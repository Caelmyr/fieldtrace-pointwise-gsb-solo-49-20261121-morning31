"""Derive auditable trigger events and compare individually traced ground points."""

from collections import Counter
from datetime import datetime, timedelta
import math

from .catalog import revision
from .clock import time_pair
from .config import DEFAULT_EVENT_GAP, MAX_EVENT_FRAMES, MAX_SEARCH_FRAMES, MAX_WALK_STEPS, MOTION_DIRECTIONS
from .contracts import (
    Conflict, Limit, SurveyError, identifier, identity_list, integer, list_value, need_object,
    option, unit_point, word,
)
from .geometry import plane_measurement


def event_time(ordered):
    known = [f["stamp_utc"] for f in ordered if f["stamp_utc"]]
    if not known:
        return {"start_utc": None, "end_utc": None, "duration_s": None}
    elapsed = datetime.fromisoformat(known[-1]) - datetime.fromisoformat(known[0])
    return {"start_utc": known[0], "end_utc": known[-1], "duration_s": elapsed.total_seconds()}


def frame_sort(frame):
    return (frame["stamp_utc"] is None, frame["stamp_utc"] or "", frame["name"].casefold(), frame["id"])


def span_seconds(prev, following):
    a = datetime.fromisoformat(prev["stamp_utc"])
    b = datetime.fromisoformat(following["stamp_utc"])
    return (b - a).total_seconds()


def gap_stats(previous, frame, threshold):
    a, b = time_pair(previous), time_pair(frame)
    if a is None or b is None:
        return {"status": "unknown time", "seconds": None, "range_s": None}
    nominal = span_seconds(previous, frame)
    low = max(0, (b[0] - a[1]).total_seconds())
    high = max(0, (b[1] - a[0]).total_seconds())
    status = "split" if nominal > threshold else "keep"
    if low <= threshold < high:
        status = "clock overlap"
    return {"status": status, "seconds": nominal, "range_s": [low, high]}


def propose(ordered, threshold):
    groups, held, decisions = [], [], []
    current = []
    for frame in ordered:
        key = frame["id"]
        if frame.get("clock_hold"):
            held.append({"frame_id": key, "reason": "clock calibration held for manual review"})
            continue
        if frame["stamp_utc"] is None or frame["time_window"] != "inside":
            held.append({"frame_id": key, "reason": frame["time_window"]})
            continue
        if current:
            last = current[-1]
            gap = gap_stats(last, frame, threshold)
            decisions.append(gap | {"left": last["id"], "right": key})
            if gap["status"] in {"split", "clock overlap"} or len(current) >= MAX_EVENT_FRAMES:
                groups.append(current)
                current = []
        current.append(frame)
    if current:
        groups.append(current)
    return groups, held, decisions


def describe_group(index, frames):
    return {
        "ordinal": index, "ids": [row["id"] for row in frames], "count": len(frames),
        "thumbnail_id": frames[0]["id"], "lighting": sorted({row["illumination"] for row in frames}),
        "first": frames[0]["name"], "last": frames[-1]["name"],
        **event_time(frames),
    }


def preview_events(store, form):
    deployment = identifier(form.get("deployment_id"), "deployment")
    threshold = integer(form.get("gap_s"), "gap_s", 2, 3600, default=DEFAULT_EVENT_GAP)
    cap = integer(form.get("limit"), "limit", 1, MAX_SEARCH_FRAMES, default=400)
    with store.use() as db:
        db.one("deployments", deployment)
        all_rows = db.matching("frames", where=(("deployment_id", "=", deployment),),
                               order=("stamp_utc", "ASC"), limit=cap + 1)
        assigned = db.membership([row["id"] for row in all_rows])
    newest = sorted([row for row in all_rows[:cap] if row["id"] not in assigned], key=frame_sort)
    groups, held, decisions = propose(newest, threshold)
    return {
        "groups": [describe_group(i, members) for i, members in enumerate(groups, 1)],
        "held": held, "gaps": decisions, "gap_s": threshold, "already_assigned": len(assigned),
        "truncated": len(all_rows) > cap,
    }


def aggregate_reviews(frames):
    counts = Counter(row["review"] for row in frames)
    common = sorted({taxon for row in frames if row["review"] == "seen" for taxon in row["taxa"]})
    unavailable = sum(counts[state] for state in ("unusable", "held", "new"))
    if counts["seen"]:
        conclusion = "positive"
    elif counts["empty"] and not unavailable:
        conclusion = "negative"
    else:
        conclusion = "pending"
    return {"counts": dict(counts), "taxa": common, "conclusion": conclusion, "pending": unavailable}


def public_event(db, event, *, include_frames=False):
    frames = db.event_members(event["id"])
    result = event | {
        "frame_count": len(frames),
        "thumbnail_id": frames[0]["id"] if frames else None,
        "review_coverage": aggregate_reviews(frames),
        "deployment": db.one("deployments", event["deployment_id"]),
    }
    if include_frames:
        result["frames"] = frames
        result["paths"] = db.matching("paths", where=(("event_id", "=", event["id"]),))
    return result


def list_events(store, deployment=None):
    where = (("deployment_id", "=", deployment),) if deployment else ()
    with store.use() as db:
        return {"events": [public_event(db, event) for event in db.matching(
            "events", where=where, order=("stamp_utc", "DESC"), limit=150)]}


def read_event(store, key):
    with store.use() as db:
        return public_event(db, db.one("events", key), include_frames=True)


def event_payload(form, deployment, frames):
    start = event_time(frames)
    return {
        "name": word(form.get("name"), "event name"),
        "deployment_id": deployment,
        "stamp_utc": start["start_utc"] or None,
        "state": option(form.get("state"), "state", {"draft", "confirmed", "deferred"}, default="draft"),
        "caption": word(form.get("caption"), "caption", max_length=1200, default="", multiline=True),
        "gap_s": integer(form.get("gap_s"), "gap_s", 2, 3600, default=DEFAULT_EVENT_GAP),
        "time_span": start,
    }


def register_event(store, form, *, key=None):
    ids = identity_list(form.get("frames"), "frames", limit=MAX_EVENT_FRAMES)
    deployment = identifier(form.get("deployment_id"), "deployment")
    with store.use(write=True) as db:
        db.one("deployments", deployment)
        frames = sorted(db.documents("frames", ids), key=frame_sort)
        if any(row["deployment_id"] != deployment for row in frames):
            raise SurveyError("An ordinary trigger event covers just one camera deployment.", field="frames")
        if any(row.get("clock_hold") for row in frames):
            raise SurveyError("Resolve the held camera clock readings before adding these photos to a trigger event.",
                              field="frames")
        payload = event_payload(form, deployment, frames)
        if payload["state"] == "confirmed" and any(row["stamp_utc"] is None for row in frames):
            raise SurveyError("A confirmed event needs resolved timestamps for all its frames.", field="frames")
        if key is None:
            event = db.append("events", payload, action="camera event saved")
        else:
            previous = db.one("events", key)
            if previous["deployment_id"] != deployment:
                raise SurveyError("An event cannot move to a different deployment.", field="deployment")
            if db.count("paths", equal={"event_id": key}):
                old_ids = {row["id"] for row in db.event_members(key)}
                if old_ids != set(ids):
                    raise Conflict("A saved ground path refers to this event's original frame set.")
            event = db.replace("events", key, revision(form), lambda old: old | payload, action="camera event updated")
        db.set_event_members(event["id"], [item["id"] for item in frames])
        return public_event(db, event, include_frames=True)


def resolve_preset_groups(store, form):
    deployment = identifier(form.get("deployment_id"), "deployment")
    threshold = integer(form.get("gap_s"), "gap_s", 2, 3600, default=DEFAULT_EVENT_GAP)
    raw_groups = list_value(form.get("groups"), "groups", min_items=1, max_items=30)
    chosen = [identity_list(group, "frames", limit=MAX_EVENT_FRAMES) for group in raw_groups]
    flat = [key for group in chosen for key in group]
    if len(set(flat)) != len(flat):
        raise SurveyError("A camera frame can belong to one trigger event.", field="groups")
    if len(flat) > 400:
        raise Limit("Save no more than 400 frame memberships at once.")
    with store.use(write=True) as db:
        location = db.one("deployments", deployment)
        if db.membership(flat):
            raise Conflict("Some suggested frames were assigned after the last preview. Refresh the suggestions.")
        catalog = {row["id"]: row for row in db.documents("frames", flat)}
        if any(row["deployment_id"] != deployment or row["time_window"] != "inside" for row in catalog.values()):
            raise Conflict("Frame deployment or time changed after preview. Refresh the suggestions.")
        if any(row.get("clock_hold") for row in catalog.values()):
            raise Conflict("Resolve the held clock readings before accepting automatic trigger groups.")
        events = []
        for group in chosen:
            frames = sorted([catalog[key] for key in group], key=frame_sort)
            local = frames[0]["local_stamp"]
            title = f"{location['name']} · {local['date']} {local['clock']}"
            events.append(save_suggested(db, deployment, title, frames, threshold))
    return {"events": events}


def save_suggested(db, deployment, title, frames, threshold):
    start = event_time(frames)
    boundaries = [gap_stats(a, b, threshold) for a, b in zip(frames, frames[1:])]
    if any(item["status"] != "keep" for item in boundaries):
        raise Conflict("A selected suggested event now spans an uncertain trigger gap.")
    event = db.append("events", {
        "name": title[:85], "deployment_id": deployment, "stamp_utc": start["start_utc"],
        "state": "draft", "caption": "", "gap_s": threshold, "time_span": start,
    }, action="trigger group accepted")
    db.set_event_members(event["id"], [row["id"] for row in frames])
    return public_event(db, event)


def path_stations(rows):
    positions = []
    used = set()
    for item in list_value(rows, "stations", min_items=2, max_items=MAX_WALK_STEPS):
        node = need_object(item, "station")
        frame = identifier(node.get("frame"), "frame")
        if frame in used:
            raise SurveyError("Mark one ground position per frame for this path.", field="stations")
        used.add(frame)
        point = unit_point(node.get("point"))
        positions.append({"frame_id": frame, "point": point})
    return positions


def elapsed_limits(a, b):
    first, second = time_pair(a), time_pair(b)
    if first is None or second is None:
        return None
    start = (second[0] - first[1]).total_seconds()
    stop = (second[1] - first[0]).total_seconds()
    if start <= 0:
        return None
    return start, stop


def path_report(model, known):
    geometry = plane_measurement(model, [item["point"] for item in known])
    points = geometry["segments"]
    steps = []
    for segment, a, b in zip(points, known, known[1:]):
        span = elapsed_limits(a["frame"], b["frame"])
        length = segment["metres"]
        speeds = [length / span[1], length / span[0]] if length is not None and span else None
        steps.append(segment | {
            "from_frame": a["frame_id"], "to_frame": b["frame_id"], "elapsed_s": span,
            "speed_m_s_interval": speeds,
        })
    straight = None
    if steps and all(step["supported"] for step in steps):
        straight = math.dist(steps[0]["ground_start"], steps[-1]["ground_end"])
    return geometry | {"steps": steps, "straight_displacement_m": straight, "time_resolved": sum(
        row["elapsed_s"] is not None for row in steps)}


def save_path(store, form):
    event = identifier(form.get("event"), "event")
    model_id = identifier(form.get("model"), "model")
    entries = path_stations(form.get("stations"))
    with store.use(write=True) as db:
        observed = db.one("events", event)
        model = db.one("models", model_id)
        if observed["deployment_id"] != model["deployment_id"]:
            raise SurveyError("The event and ground model must use the same deployment.")
        members = {row["id"]: row for row in db.event_members(event)}
        if not all(row["frame_id"] in members for row in entries):
            raise SurveyError("All path marks must come from the selected event.", field="stations")
        if any(members[row["frame_id"]]["extent"] != model["source_extent"] for row in entries):
            raise SurveyError("Ground measurements require unchanged frame dimensions.", field="stations")
        ordered = sorted(entries, key=lambda row: frame_sort(members[row["frame_id"]]))
        result = path_report(model, [row | {"frame": members[row["frame_id"]]} for row in ordered])
        return db.append("paths", result | {
            "event_id": event, "event_version": observed["version"], "model_id": model_id,
            "name": word(form.get("name"), "path name"),
            "stations": [row | {"frame_version": members[row["frame_id"]]["version"]} for row in ordered],
            "direction": option(form.get("direction"), "direction", MOTION_DIRECTIONS, default="unresolved"),
            "note": word(form.get("note"), "path note", max_length=1500, default="", multiline=True),
        }, action="ground positions compared")
