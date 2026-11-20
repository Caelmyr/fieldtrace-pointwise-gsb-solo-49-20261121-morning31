"""Station deployment, frame intake, edits and review expiry."""

from datetime import datetime, timezone

from .clock import interval, interpret_camera, local_display, operator_stamp, time_relation
from .config import (
    DEPLOYMENT_STATES, HABITATS, ILLUMINATIONS, MAX_BATCH_BYTES, MAX_BATCH_FILES, MAX_BATCH_PIXELS,
    MAX_FRAME_COUNT, MAX_SITE_COUNT, MAX_VISIT_DAYS, ORIGINS, REVIEW_STATES, SORT_KEYS, TAXA,
)
from .contracts import (
    Conflict, Limit, SurveyError, date_time, flag, identifier, identity_list, integer,
    option, QueryPage, timezone_id, utc_text, word,
)
from .images import decode, frame_assets, read_upload


def revision(form):
    return integer(form.get("version"), "version", 1, 2**31 - 1)


def station_data(form):
    return {
        "name": word(form.get("name"), "station name"),
        "habitat": option(form.get("habitat"), "habitat", HABITATS, default="other"),
        "timezone": timezone_id(form.get("timezone", "UTC")),
        "code": word(form.get("code"), "survey code", max_length=26, default=""),
        "note": word(form.get("note"), "notes", max_length=1800, default="", multiline=True),
        "private": flag(form.get("private"), "private"),
    }


def save_station(store, form, key=None):
    fields = station_data(form)
    with store.use(write=True) as db:
        if key is None:
            if db.count("stations") >= MAX_SITE_COUNT:
                raise Limit("The survey already has the maximum number of stations.")
            return db.append("stations", fields)
        return db.replace("stations", key, revision(form), lambda old: old | fields)


def deployment_data(form, station):
    start, end = interval(form, "started_at", "ended_at")
    if (end - start).total_seconds() > MAX_VISIT_DAYS * 86400:
        raise Limit("Choose a deployment span of at most 730 days.")
    return {
        "station_id": station["id"],
        "name": word(form.get("name"), "deployment name"),
        "camera": word(form.get("camera"), "camera identifier", max_length=65, default=""),
        "timezone": timezone_id(form.get("timezone") or station["timezone"]),
        "started_at": utc_text(start),
        "ended_at": utc_text(end),
        "state": option(form.get("state"), "state", DEPLOYMENT_STATES, default="active"),
        "clock_fast_s": integer(form.get("clock_fast_s"), "clock_fast_s", -86400, 86400, default=0),
        "clock_uncertainty_s": integer(form.get("clock_uncertainty_s"), "clock_uncertainty_s", 0, 7200, default=0),
        "note": word(form.get("note"), "notes", max_length=1800, default="", multiline=True),
    }


def save_deployment(store, form, key=None):
    with store.use(write=True) as db:
        old = db.one("deployments", key) if key else None
        station_key = identifier(form.get("station_id"), "station") if old is None else old["station_id"]
        station = db.one("stations", station_key)
        new = deployment_data(form, station)
        if old is None:
            return db.append("deployments", new)
        return db.replace("deployments", key, revision(form), lambda current: current | new)


def deployment_rollup(db, record):
    return record | {
        "frame_count": db.count("frames", equal={"deployment_id": record["id"]}),
        "event_count": db.count("events", equal={"deployment_id": record["id"]}),
        "outage_count": db.count("outages", equal={"deployment_id": record["id"]}),
    }


def stations_view(store):
    with store.use() as db:
        stations = db.matching("stations", order=("name", "ASC"))
        deploys = [deployment_rollup(db, i) for i in db.matching("deployments")]
    return {"stations": stations, "deployments": deploys}


def import_form(form):
    illumination = option(form.get("illumination"), "illumination", ILLUMINATIONS, default="other")
    entered = form.get("camera_time")
    if entered:
        date_time(entered, "camera_time")
    fold = form.get("fold")
    return {
        "illumination": illumination,
        "override": entered or None,
        "fold": integer(fold, "fold", 0, 1) if fold not in (None, "") else None,
        "note": word(form.get("note"), "source note", max_length=1200, default="", multiline=True),
    }


def time_window(stamp, deployment):
    if not stamp:
        return "unresolved"
    return "inside" if deployment["started_at"] <= stamp < deployment["ended_at"] else "outside deployment"


def register_photo(db, deployment, filename, photo, parameters, *, origin="upload"):
    existing = db.matching(
        "frames",
        where=(("deployment_id", "=", deployment["id"]), ("digest", "=", photo.digest)),
        limit=1,
    )
    if existing:
        return existing[0], True
    if db.count("frames") >= MAX_FRAME_COUNT:
        raise Limit("This survey has reached its frame limit.")
    clock = interpret_camera(photo.metadata, deployment, parameters.get("override"), parameters.get("fold"))
    frame = db.append("frames", {
        "deployment_id": deployment["id"],
        "station_id": deployment["station_id"],
        "name": filename,
        "digest": photo.digest,
        "illumination": parameters["illumination"],
        "review": "new",
        "origin": option(origin, "origin", ORIGINS),
        "time": clock,
        "stamp_utc": clock["utc"],
        "time_window": time_window(clock["utc"], deployment),
        "local_stamp": local_display(clock["utc"], deployment["timezone"]),
        "extent": list(photo.extent),
        "source_extent": list(photo.original_extent),
        "source_media": photo.mime,
        "depth": photo.depth,
        "colour": photo.kind,
        "supported_fraction": float(photo.valid.mean()),
        "camera": photo.metadata,
        "note": parameters["note"],
        "taxa": [],
        "review_note": "",
        "reviewed_at": None,
    }, action="camera photo imported")
    frame_assets(db, frame, photo)
    return frame, False


def receive_frames(store, deployment_id, uploads, raw):
    if not 1 <= len(uploads) <= MAX_BATCH_FILES:
        raise Limit(f"Choose between 1 and {MAX_BATCH_FILES} camera photos.")
    parameters = import_form(raw)
    if parameters["override"] and len(uploads) != 1:
        raise SurveyError("Enter one explicit timestamp only when importing a single photo.", field="camera_time")
    images = []
    incoming, pixels = 0, 0
    for upload in uploads:
        filename, binary = read_upload(upload)
        incoming += len(binary)
        if incoming > MAX_BATCH_BYTES:
            raise Limit("This intake exceeds the 64 MiB batch limit.")
        photo = decode(binary)
        pixels += photo.original_extent[0] * photo.original_extent[1]
        if pixels > MAX_BATCH_PIXELS:
            raise Limit("This intake exceeds the 70-million-pixel batch limit.")
        images.append((filename, photo))
    with store.use(write=True) as db:
        deployment = db.one("deployments", deployment_id)
        results = [
            {"frame": frame, "duplicate": duplicate}
            for filename, photo in images
            for frame, duplicate in [register_photo(db, deployment, filename, photo, parameters)]
        ]
    return {"results": results, "new_frames": sum(not row["duplicate"] for row in results),
            "duplicate_frames": sum(row["duplicate"] for row in results)}


def frame_listing(store, query):
    page = QueryPage.parse(query, SORT_KEYS)
    where = []
    for param in ("deployment_id", "review", "illumination"):
        if not query.get(param):
            continue
        if param == "deployment_id":
            value = identifier(query[param], param)
        else:
            value = option(query[param], param, REVIEW_STATES if param == "review" else ILLUMINATIONS)
        where.append((param, "=", value))
    if query.get("after"):
        where.append(("stamp_utc", ">=", utc_text(date_time(query["after"], "after", needs_offset=True))))
    if query.get("before"):
        where.append(("stamp_utc", "<", utc_text(date_time(query["before"], "before", needs_offset=True))))
    with store.use() as db:
        frames = db.matching("frames", where=where, order=SORT_KEYS[page.sort], limit=page.limit + 1, offset=page.offset)
        more = len(frames) > page.limit
        listed = frames[:page.limit]
        membership = db.membership([x["id"] for x in listed])
    return {"frames": [x | {"event_id": membership.get(x["id"])} for x in listed],
            "offset": page.offset, "more": more}


def inspect_frame(store, key):
    with store.use() as db:
        frame = db.one("frames", key)
        deployment = db.one("deployments", frame["deployment_id"])
        criterion = (("frame_id", "=", key),)
        event_link = db.membership([key]).get(key)
        tracks = []
        if event_link:
            tracks = [
                {"id": row["id"], "name": row["name"]}
                for row in db.matching("tracks", where=(("event_id", "=", event_link),))
                if any(point["frame_id"] == key for point in row["points"])
            ]
        return frame | {
            "deployment": deployment,
            "station": db.one("stations", frame["station_id"]),
            "marks": db.matching("marks", where=criterion, order=("created_at", "ASC")),
            "glances": db.matching("glances", where=criterion),
            "measures": db.matching("measures", where=criterion),
            "event_id": event_link,
            "tracks": tracks,
            "assets": db.assets(key),
        }


def review_values(form):
    review = option(form.get("review"), "review", REVIEW_STATES)
    choices = form.get("taxa") or []
    if not isinstance(choices, list) or len(choices) > 8:
        raise SurveyError("Choose up to eight taxa.", field="taxa")
    taxa = list(dict.fromkeys(option(value, "taxon", TAXA) for value in choices))
    if review == "seen" and not taxa:
        raise SurveyError("Choose at least one animal label, or use Uncertain animal.", field="taxa")
    if review in ("empty", "unusable") and taxa:
        raise SurveyError("Clear taxa from an empty or unusable observation.", field="taxa")
    return {
        "review": review,
        "taxa": taxa,
        "review_note": word(form.get("review_note"), "review note", max_length=2000, default="", multiline=True),
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds") if review != "new" else None,
    }


def review_frames(store, form):
    values = review_values(form)
    keys = identity_list(form.get("frames"), "frames", limit=100)
    versions = form.get("versions")
    if not isinstance(versions, dict):
        raise SurveyError("Load the latest frame versions before reviewing.", field="frames")
    with store.use(write=True) as db:
        frames = db.documents("frames", keys)
        changed = []
        for frame in frames:
            key = frame["id"]
            expected = integer(versions.get(key), "version", 1, 2**31 - 1)
            changed.append(db.replace("frames", key, expected, lambda old: old | values, action="review decision"))
    return {"frames": changed}


def edit_stamp(store, key, form):
    with store.use(write=True) as db:
        initial = db.one("frames", key)
        deployment = db.one("deployments", initial["deployment_id"])
        event_link = db.membership([key]).get(key)
        clock = operator_stamp(form, deployment, initial["time"])
        next_values = {
            "time": clock,
            "stamp_utc": clock["utc"],
            "local_stamp": local_display(clock["utc"], deployment["timezone"]),
            "time_window": time_window(clock["utc"], deployment),
        }
        if event_link and initial["stamp_utc"] != clock["utc"]:
            raise Conflict("Detach the frame from its event, or rebuild the event before changing its time.",
                           facts={"event_id": event_link})
        return db.replace("frames", key, revision(form), lambda old: old | next_values, action="camera time reviewed")


def frame_compare(store, left, right):
    with store.use() as db:
        a, b = db.documents("frames", [left, right])
    return {"first": a, "second": b, "order": time_relation(a, b),
            "same_station": a["station_id"] == b["station_id"],
            "same_deployment": a["deployment_id"] == b["deployment_id"],
            "same_extent": a["extent"] == b["extent"]}
