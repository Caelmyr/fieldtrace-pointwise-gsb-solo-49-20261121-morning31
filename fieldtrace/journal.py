"""Downloadable survey registers, audit history and attachment integrity checks."""

from collections import Counter
import csv
import hashlib
from io import StringIO
from pathlib import Path

from .config import MAX_SEARCH_FRAMES, REVIEW_STATES
from .contracts import Limit, SurveyError, flag, identifier, integer, option
from .evidence import safe_cell

REGISTER_FIELDS = [
    ("name", "camera_file"), ("site", "station"), ("camera", "deployment"),
    ("capture", "utc"), ("local", "local_time"), ("basis", "time_evidence"),
    ("window", "deployment_window"), ("clock_hold", "clock_hold"),
    ("review", "review"), ("taxa", "animals"),
    ("classification", "lighting"), ("extent", "image_dimensions"), ("digest", "source_sha256"),
    ("version", "version"), ("origin", "source_kind"),
]


def csv_bytes(columns, records):
    f = StringIO()
    w = csv.writer(f, lineterminator="\n")
    w.writerow([title for _, title in columns])
    for record in records:
        w.writerow([safe_cell(record.get(key)) for key, _ in columns])
    return f.getvalue().encode("utf-8")


def protected_site(station, hide):
    if hide and station["private"]:
        return station["code"] or "Withheld"
    return station["name"]


def frame_row(record, stations, cameras, audit, share):
    station = stations[record["station_id"]]
    deployment = cameras[record["deployment_id"]]
    placed = protected_site(station, share)
    image_name = "Restricted camera image" if station["private"] and share else record["name"]
    local_label = record["local_stamp"]["wall_time"] if record["local_stamp"] else None
    return {
        "name": image_name, "site": placed,
        "camera": deployment["camera"] if not (station["private"] and share) else None,
        "capture": record["stamp_utc"], "local": local_label, "basis": record["time"]["basis"],
        "window": record["time_window"], "review": record["review"],
        "clock_hold": "held" if record.get("clock_hold") else "",
        "taxa": ", ".join(record["taxa"]), "classification": record["illumination"],
        "extent": "x".join(map(str, record["extent"])), "digest": record["digest"],
        "origin": record["origin"], "version": record["version"],
        "history_length": len(audit),
    }


def frame_register(store, query):
    requested = integer(query.get("limit"), "limit", 1, MAX_SEARCH_FRAMES, default=600)
    deployment = identifier(query.get("deployment"), "deployment", optional=True)
    status = query.get("review")
    share = flag(query.get("share"), "share")
    include_history = flag(query.get("history"), "history")
    filters = []
    if deployment:
        filters.append(("deployment_id", "=", deployment))
    if status:
        filters.append(("review", "=", option(status, "review", REVIEW_STATES)))
    with store.use() as db:
        frames = db.matching("frames", where=filters, order=("stamp_utc", "ASC"), limit=requested + 1)
        if len(frames) > requested:
            raise Limit("Register scope exceeds its row limit. Choose a single deployment or more rows.")
        stations = {row["id"]: row for row in db.matching("stations")}
        cameras = {row["id"]: row for row in db.matching("deployments")}
        rows = [frame_row(rec, stations, cameras, db.history(rec["id"]) if include_history else [], share)
                for rec in frames]
    columns = REGISTER_FIELDS + ([("history_length", "saved_revisions")] if include_history else [])
    return csv_bytes(columns, rows)


def report_rows(report, kind):
    notes = []
    if kind == "effort":
        for station in report["stations"]:
            for day in station["days"]:
                notes.append({"station": station["station_name"], "time_zone": station["timezone"]} | day)
        columns = [(k, k) for k in ("station", "time_zone", "date", "active_hours", "calendar_hours",
                                    "seen", "not_seen", "pending", "rate_per_24h")]
    elif kind == "activity":
        for station in report["stations"]:
            for window in station["entries"]:
                notes.append({"station": station["name"], "time_zone": station["timezone"]} | window)
        columns = [(k, k) for k in ("station", "time_zone", "start", "end", "hours",
                                    "seen", "not_seen", "pending", "rate_per_24h")]
    elif kind == "sequence":
        notes = [
            {"name": row["name"], "frame": row["frame_id"], "order": row["sequence_index"],
             "time": row["time"]["utc"], "review": row["review"], "bounds": " ".join(map(str, row["bounds"]))}
            for row in report["metadata"]
        ]
        columns = [(k, k) for k in ("order", "name", "frame", "time", "review", "bounds")]
    else:
        raise SurveyError("No journal layout exists for this report.")
    return columns, notes


def report_csv(store, key):
    with store.use() as db:
        record = db.one("reports", key)
    columns, rows = report_rows(record["report"], record["kind"])
    return csv_bytes(columns, rows)


def safe_stat(path):
    try:
        return path.stat()
    except FileNotFoundError:
        return None


def blob_digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while data := f.read(1024 * 512):
            hasher.update(data)
    return hasher.hexdigest()


def attachment_result(asset, filepath, verify_digest):
    metadata = safe_stat(filepath)
    state = "present"
    if metadata is None:
        state = "missing"
    elif metadata.st_size != asset["bytes"]:
        state = "length changed"
    elif verify_digest and blob_digest(filepath) != asset["digest"]:
        state = "digest changed"
    return {"label": asset["label"], "media_type": asset["media_type"],
            "state": state, "bytes": asset["bytes"], "sha256": asset["digest"]}


def record_integrity(store, key, query):
    check_hash = flag(query.get("sha256"), "sha256")
    with store.use() as db:
        facts = db.assets(key)
        versions = db.history(key)
        if not facts and not versions:
            raise SurveyError("This identifier has no recorded media or history.", status=404)
    if sum(row["bytes"] for row in facts) > 140 * 1024 ** 2 and check_hash:
        raise Limit("The files for this record exceed the verification budget.")
    verified = [attachment_result(asset, store.blobs.path(asset["digest"]), check_hash) for asset in facts]
    counts = Counter(row["state"] for row in verified)
    return {
        "record": key, "assets": verified, "statuses": dict(counts),
        "hashes_recomputed": check_hash, "revisions": len(versions),
        "created_at": versions[-1]["at"] if versions else None,
        "latest_revision": versions[0]["version"] if versions else None,
    }
