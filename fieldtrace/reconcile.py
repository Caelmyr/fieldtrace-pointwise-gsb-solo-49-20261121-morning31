"""Check camera log sidecars against imported photos before committing timestamp changes."""

from collections import Counter, defaultdict
import csv
from io import StringIO
import unicodedata

from .catalog import time_window
from .clock import date_time, local_display, operator_stamp
from .contracts import Conflict, Limit, SurveyError, identifier, integer, list_value, word
from .images import safe_filename

MAX_LOG_LINES = 550
MAX_BYTES_TEXT = 256 * 1024
HEADER = {"filename", "captured_at"}
OPTIONAL = {"time_uncertainty_s", "source_sha256", "comment"}


def path_key(value):
    return unicodedata.normalize("NFC", safe_filename(value)).casefold()


def normalize_source(raw, line):
    filename = word(raw.get("filename"), "filename", max_length=140)
    if filename != safe_filename(filename):
        raise SurveyError(f"Line {line} has no simple camera filename.", field="csv")
    stamp_text = word(raw.get("captured_at"), "camera timestamp", max_length=80)
    stamp = date_time(stamp_text, "captured_at")
    digest = raw.get("source_sha256", "").strip()
    if digest and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
        raise SurveyError(f"Line {line} requires a lowercase SHA-256 value.", field="csv")
    accuracy = integer(raw.get("time_uncertainty_s"), "time_uncertainty_s", 0, 86400, default=0)
    return {
        "filename": filename, "name_key": path_key(filename), "captured_at": stamp.isoformat(timespec="seconds"),
        "evidence_mode": "utc" if stamp.utcoffset() is not None else "clock",
        "time_uncertainty_s": accuracy, "source_sha256": digest or None,
        "comment": word(raw.get("comment"), "comment", max_length=120, default=""),
        "line": line,
    }


def parse_csv(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES_TEXT:
        raise Limit("Use a CSV log smaller than 256 KiB.")
    reader = csv.DictReader(StringIO(raw.removeprefix("\ufeff")), strict=True)
    try:
        fields = reader.fieldnames or []
        if set(fields) - HEADER - OPTIONAL or not HEADER.issubset(fields) or len(set(fields)) != len(fields):
            raise SurveyError("Use filename and captured_at headers, with optional time_uncertainty_s, source_sha256 and comment.",
                              field="csv")
        parsed = []
        for record in reader:
            line = reader.line_num
            if None in record or any(value is None for value in record.values()):
                raise SurveyError(f"Line {line} does not match the CSV headers.", field="csv")
            if len(parsed) >= MAX_LOG_LINES:
                raise Limit("This sidecar contains more than 550 entries.")
            parsed.append(normalize_source(record, line))
    except csv.Error as exc:
        raise SurveyError("The camera log does not contain valid delimited records.", field="csv") from exc
    if not len(parsed):
        raise SurveyError("Add one or more logged photo times.", field="csv")
    return parsed


def index_by_name(frames):
    names, hashes = defaultdict(list), {}
    for frame in frames:
        names[path_key(frame["name"])].append(frame)
        hashes[frame["digest"]] = frame
    return names, hashes


def collect_candidates(log, names, digests):
    expected = log["source_sha256"]
    if expected:
        match = digests.get(expected)
        if not match:
            return [], "digest does not match a selected camera image"
        if path_key(match["name"]) != log["name_key"]:
            return [match], "filename and known digest identify different names"
        return [match], None
    candidates = names.get(log["name_key"], [])
    if not candidates:
        return [], "not imported in this deployment"
    if len(candidates) > 1:
        return candidates, "multiple photos share this name"
    return candidates, None


def proposal_for(frame, log, deployment, linked):
    raw = {"mode": log["evidence_mode"], "stamp": log["captured_at"],
           "uncertainty_s": log["time_uncertainty_s"]}
    timing = operator_stamp(raw, deployment, frame["time"])
    data = dict(timing)
    if log["evidence_mode"] == "clock" and data["utc"]:
        data["uncertainty_s"] = max(data["uncertainty_s"], log["time_uncertainty_s"])
    status, note = "ready", None
    if data["utc"] is None:
        status, note = "held", data["basis"]
    elif linked and frame["stamp_utc"] != data["utc"]:
        status, note = "held", "photo belongs to a trigger event whose order would change"
    elif frame["stamp_utc"] == data["utc"]:
        status, note = "unchanged", "record already uses this UTC time"
    elif time_window(data["utc"], deployment) != "inside":
        status, note = "held", "proposed time falls outside this deployment"
    return {
        "frame_id": frame["id"], "version": frame["version"], "source_sha256": frame["digest"],
        "previous_utc": frame["stamp_utc"], "proposed_utc": data["utc"], "timing": data,
        "status": status, "reason": note,
    }


def look_at_row(source, names, hashes, deployment, assignments):
    candidate, warning = collect_candidates(source, names, hashes)
    if warning:
        return source | {"status": "held", "reason": warning, "candidates": [x["id"] for x in candidate]}
    frame = candidate[0]
    return source | proposal_for(frame, source, deployment, assignments.get(frame["id"]))


def build_preview(db, key, rows):
    deployment = db.one("deployments", key)
    frames = db.matching("frames", where=(("deployment_id", "=", key),), limit=2501)
    if len(frames) > 2500:
        raise Limit("The selected deployment has too many photos for a single sidecar check.")
    names, digests = index_by_name(frames)
    existing = db.membership([frame["id"] for frame in frames])
    checked = [look_at_row(row, names, digests, deployment, existing) for row in rows]
    repeated = Counter(item["frame_id"] for item in checked if item.get("frame_id"))
    for row in checked:
        if row.get("frame_id") and repeated[row["frame_id"]] > 1:
            row["status"] = "held"
            row["reason"] = "several lines refer to the same imported photo"
    return {"deployment": key, "deployment_version": deployment["version"],
            "rows": checked, "totals": dict(Counter(row["status"] for row in checked))}


def preview_reconcile(store, form):
    group = identifier(form.get("deployment_id"), "deployment")
    records = parse_csv(form.get("csv"))
    with store.use() as db:
        return build_preview(db, group, records)


def inputs_for_commit(form):
    selected = list_value(form.get("selected"), "selected", min_items=1, max_items=MAX_LOG_LINES)
    versions = form.get("versions")
    if not isinstance(versions, dict):
        raise SurveyError("Refresh the camera log preview before committing.")
    if any(type(value) != int or value < 2 for value in selected):
        raise SurveyError("The selected lines must be listed by their line numbers.")
    wanted = set(selected)
    if len(wanted) != len(selected):
        raise SurveyError("Select each camera log line at most once.")
    return wanted, versions


def apply_reconcile(store, form):
    group = identifier(form.get("deployment_id"), "deployment")
    records = parse_csv(form.get("csv"))
    wanted, versions = inputs_for_commit(form)
    with store.use(write=True) as db:
        preview = build_preview(db, group, records)
        if preview["deployment_version"] != form.get("deployment_version"):
            raise Conflict("The deployment changed. Refresh the camera log preview.")
        entries = [row for row in preview["rows"] if row["line"] in wanted]
        if len(entries) != len(wanted) or any(row["status"] != "ready" for row in entries):
            raise Conflict("A selected log line is missing its current, unambiguous camera photo. Refresh the preview.")
        deployment = db.one("deployments", group)
        changed = []
        for row in entries:
            stamp = row["proposed_utc"]
            patch = {
                "time": row["timing"], "stamp_utc": stamp, "time_window": time_window(stamp, deployment),
                "local_stamp": local_display(stamp, deployment["timezone"]),
                "time_log": {"comment": row["comment"], "line": row["line"], "value": row["captured_at"]},
            }
            frame = db.one("frames", row["frame_id"], optional=True)
            if frame is not None and frame.get("clock_hold"):
                patch["clock_hold"] = None
            expected = versions.get(row["frame_id"])
            if type(expected) != int:
                raise Conflict("The original version of a selected photo is missing.")
            changed.append(db.replace("frames", row["frame_id"], expected, lambda old: old | patch,
                                      action="camera log time verified"))
    return {"changed": changed, "deployment": group}
