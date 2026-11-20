"""Piecewise camera-clock calibration from watch-vs-camera anchor photos.

The deployment form keeps a single "clock fast by N seconds" value, but a real
camera can drift progressively and jump when its battery is swapped.  This
module keeps the two problems separate:

* anchors are photos showing both the camera clock and a trusted watch, so the
  raw camera instant and the observed true instant are both known;
* operator-marked battery-swap windows split the deployment into clock segments,
  each fitted independently with an affine drift model;
* frames whose camera reading can belong to either side of a swap are never
  guessed.  They are held for manual review and kept out of automatic trigger
  groups, together with anchors that do not fit their segment.

Nothing in here rewrites an event-linked frame, so confirmed trigger groups are
never silently reordered.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .catalog import time_window
from .clock import local_display
from .contracts import (
    Conflict, Limit, SurveyError, date_time, identifier, identity_list, integer,
    list_value, need_object, option, utc_text, word,
)

MAX_ANCHORS = 60
MAX_SWAP_WINDOWS = 12
MAX_CALIBRATION_FRAMES = 2500
MAX_WINDOW_HOURS = 720
# A fitted clock segment must keep camera time moving forward at a plausible rate.
SLOPE_LOW, SLOPE_HIGH = 0.5, 2.0
MAX_RESIDUAL_S = 120
MAX_REJECT_ROUNDS = 12
CALIBRATION_BASES = {"camera offset", "deployment time zone", "operator fold"}
ANCHOR_SIDES = {"before", "after", "unknown"}
UTC = timezone.utc


def parse_instant(value, name):
    return date_time(value, name, needs_offset=True).astimezone(UTC)


def epoch_seconds(stamp):
    return stamp.timestamp()


def iso(stamp):
    return utc_text(stamp)


@dataclass
class Segment:
    index: int
    start: datetime
    end: datetime
    label: str
    anchors: list = field(default_factory=list)
    model: dict | None = None

    @property
    def start_utc(self):
        return iso(self.start)

    @property
    def end_utc(self):
        return iso(self.end)


def parse_windows(form):
    rows = list_value(form.get("swap_windows"), "swap_windows", min_items=0, max_items=MAX_SWAP_WINDOWS)
    windows = []
    for pos, raw in enumerate(rows, 1):
        item = need_object(raw, "swap window")
        start = parse_instant(item.get("start"), "swap window start")
        end = parse_instant(item.get("end"), "swap window end")
        if end <= start:
            raise SurveyError("Each battery-swap window must end after it starts.", field="swap_windows")
        if (end - start) > timedelta(hours=MAX_WINDOW_HOURS):
            raise Limit("A battery-swap window cannot span more than 30 days.")
        note = word(item.get("note"), "swap window note", max_length=200, default="")
        windows.append({"index": pos, "start": start, "end": end, "note": note,
                        "start_utc": iso(start), "end_utc": iso(end)})
    windows.sort(key=lambda row: row["start"])
    for earlier, later in zip(windows, windows[1:]):
        if later["start"] < earlier["end"]:
            raise SurveyError("Battery-swap windows cannot overlap. Order the service visits first.",
                              field="swap_windows")
    return windows


def anchor_side(watch_time, windows):
    """Locate a trusted watch reading relative to the marked swap windows.

    Returns the side label and the clock-segment index the reading belongs to:
    segment 0 precedes the first window, segment k follows window k.
    """
    for window in windows:
        if watch_time < window["start"]:
            return "before", window["index"] - 1
        if watch_time < window["end"]:
            return "unknown", None
    return "after", len(windows)


def build_segments(deployment, windows):
    start = parse_instant(deployment["started_at"], "deployment start")
    end = parse_instant(deployment["ended_at"], "deployment end")
    segments = []
    cursor = start
    for index, window in enumerate(windows):
        window_start, window_end = window["start"], window["end"]
        if window_start > cursor:
            label = "Before first battery swap" if index == 0 else f"Between swap {index} and {index + 1}"
            segments.append(Segment(index=len(segments), start=cursor, end=window_start, label=label))
        cursor = window_end
    if end > cursor:
        label = "Whole deployment" if not windows else "After final battery swap"
        segments.append(Segment(index=len(segments), start=cursor, end=end, label=label))
    return segments


def fit_affine(points):
    """Least squares fit true_time = beta * camera_time + alpha (seconds)."""
    n = len(points)
    xs = [epoch_seconds(camera) for camera, _ in points]
    ys = [epoch_seconds(watch) for _, watch in points]
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    vary = sum((x - mean_x) ** 2 for x in xs)
    if vary < 1e-9:
        beta = 1.0
    else:
        beta = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / vary
    alpha = mean_y - beta * mean_x
    residuals = [y - (beta * x + alpha) for x, y in zip(xs, ys)]
    rms = (sum(r * r for r in residuals) / n) ** 0.5
    return beta, alpha, rms, residuals


def fit_segment(segment, tolerance):
    """Fit one segment, moving the worst-fitting anchor to held one round at a time."""
    accepted = list(segment.anchors)
    rejected = []
    for _ in range(MAX_REJECT_ROUNDS):
        if not accepted:
            return None, {item["frame_id"]: item for item in rejected}
        points = [(item["camera_instant"], item["watch_time"]) for item in accepted]
        beta, alpha, rms, residuals = fit_affine(points)
        if not (SLOPE_LOW <= beta <= SLOPE_HIGH):
            # A clock cannot plausibly run at this rate: refuse to extrapolate
            # and return every anchor for manual review.
            for item in accepted:
                item["status"] = "held"
                item["reason"] = "segment drift rate is outside a plausible camera clock range"
                rejected.append(item)
            return None, {item["frame_id"]: item for item in rejected}
        worst_abs = max(abs(value) for value in residuals)
        if len(accepted) == 1 or worst_abs <= tolerance:
            model = {
                "beta": beta, "alpha_s": alpha, "rms_s": round(rms, 3),
                "anchor_count": len(accepted),
                "anchor_ids": [item["frame_id"] for item in accepted],
                "status": "fitted" if len(accepted) > 1 else "single anchor offset",
            }
            for item, residual in zip(accepted, residuals):
                item["residual_s"] = round(residual, 3)
            return model, {item["frame_id"]: item for item in rejected}
        item = next(anchor for anchor, residual in zip(accepted, residuals) if abs(residual) == worst_abs)
        item["status"] = "held"
        item["reason"] = f"anchor residual {round(worst_abs, 1)} s exceeds the {tolerance:g} s tolerance"
        item["residual_s"] = round(worst_abs, 3)
        rejected.append(item)
        accepted.remove(item)
    # Exhausted the rejection budget: hold the remaining anchors rather than guess.
    for item in accepted:
        item["status"] = "held"
        item["reason"] = "too many anchors failed the segment fit"
        rejected.append(item)
    return None, {item["frame_id"]: item for item in rejected}


def raw_camera_instant(frame, deployment):
    """Reverse the single deployment offset to recover the camera's own reading."""
    basis = frame["time"].get("basis")
    if basis not in CALIBRATION_BASES or not frame.get("stamp_utc"):
        return None
    stored = parse_instant(frame["stamp_utc"], "stamp_utc")
    skew = deployment["clock_fast_s"]
    return stored + timedelta(seconds=skew)


def parse_anchors(form, frames_by_id, deployment, windows):
    rows = list_value(form.get("anchors"), "anchors", min_items=1, max_items=MAX_ANCHORS)
    anchors = []
    seen = set()
    for pos, raw in enumerate(rows, 1):
        item = need_object(raw, "clock anchor")
        frame_id = identifier(item.get("frame"), "anchor frame")
        if frame_id in seen:
            raise SurveyError("Each anchor photo can be entered only once.", field="anchors")
        seen.add(frame_id)
        frame = frames_by_id.get(frame_id)
        entry = {
            "line": pos, "frame_id": frame_id,
            "frame_name": frame["name"] if frame else None,
            "watch_text": word(item.get("watch_time"), "watch time", max_length=40),
            "uncertainty_s": integer(item.get("uncertainty_s"), "anchor uncertainty", 0, 7200, default=0),
            "side_override": option(item.get("side"), "anchor side", ANCHOR_SIDES, default="unknown"),
            "note": word(item.get("note"), "anchor note", max_length=200, default=""),
            "camera_instant": None, "watch_time": None, "segment": None,
            "status": "ready", "reason": None, "residual_s": None,
        }
        if frame is None:
            entry.update(status="held", reason="anchor photo is not imported in this deployment")
            anchors.append(entry)
            continue
        if frame["deployment_id"] != deployment["id"]:
            entry.update(status="held", reason="anchor photo belongs to a different deployment")
            anchors.append(entry)
            continue
        watch_time = parse_instant(entry["watch_text"], "watch time")
        camera_instant = raw_camera_instant(frame, deployment)
        entry["watch_time"] = watch_time
        entry["watch_utc"] = iso(watch_time)
        entry["camera_instant"] = camera_instant
        entry["camera_utc"] = iso(camera_instant) if camera_instant else None
        entry["observed_offset_s"] = round(
            (camera_instant - watch_time).total_seconds(), 3) if camera_instant else None
        if camera_instant is None:
            reason = ("photo was already corrected by a clock calibration, so it has no raw camera reading"
                      if frame["time"].get("basis") == "piecewise clock calibration"
                      else "photo has no camera-clock reading (direct operator time or unresolved)")
            entry.update(status="held", reason=reason)
            anchors.append(entry)
            continue
        side, segment_index = anchor_side(watch_time, windows)
        if entry["side_override"] != "unknown":
            side = entry["side_override"]
            if side == "before":
                segment_index = max(0, (segment_index or 0))
            else:
                segment_index = min(len(windows), (segment_index or 0) + 1)
        entry["declared_side"] = side
        entry["segment"] = segment_index if side != "unknown" else None
        if side == "unknown":
            entry.update(status="held",
                         reason="watch reading is inside a battery-swap window and the side is not confirmed")
        anchors.append(entry)
    return anchors


def candidate_segments(camera_instant, windows, segments):
    """Segments whose model could explain a raw camera reading.

    A reading inside a battery-swap window is reachable from both neighbouring
    segments, because the operator cannot tell whether it was shot before or
    after the clock jump.  Every other reading matches at most one segment.
    """
    camera_text = iso(camera_instant)
    fitted = [seg for seg in segments if seg.model]
    matches = [seg for seg in fitted if seg.start_utc <= camera_text < seg.end_utc]
    if matches:
        return matches
    for index, window in enumerate(windows):
        if window["start_utc"] <= camera_text < window["end_utc"]:
            left = next((seg for seg in fitted if seg.end_utc == window["start_utc"]), None)
            right = next((seg for seg in fitted if seg.start_utc == window["end_utc"]), None)
            return [seg for seg in (left, right) if seg is not None]
    return []


def apply_model(stamp, model):
    seconds = epoch_seconds(stamp)
    corrected = model["beta"] * seconds + model["alpha_s"]
    return datetime.fromtimestamp(corrected, tz=UTC)


def corrected_uncertainty(frame, tolerance, model):
    base = frame["time"].get("uncertainty_s") or 0
    fit = max(tolerance, model.get("rms_s") or 0)
    return max(int(base), int(round(fit)))


def shared_window(segment_index, candidate_segs, windows):
    """Window separating a candidate segment from its neighbour in this match."""
    neighbour_indexes = [seg.index for seg in candidate_segs if seg.index != segment_index]
    if not neighbour_indexes:
        return None
    boundary = max(segment_index, *neighbour_indexes)
    return boundary if 1 <= boundary <= len(windows) else None


def order_flips(ambiguous):
    """Pairs of held frames whose true order reverses between the two sides.

    Only photos sharing the same pair of candidate segments (the same swap
    window) are compared.  A flip means the before/after ordering cannot be
    decided without knowing which side of the jump each photo belongs to.
    """
    flips = []
    for i, left in enumerate(ambiguous):
        left_map = {candidate["segment"]: candidate["proposed_utc"]
                    for candidate in left["candidates"]}
        if len(left_map) < 2:
            continue
        for right in ambiguous[i + 1:]:
            right_map = {candidate["segment"]: candidate["proposed_utc"]
                         for candidate in right["candidates"]}
            shared = sorted(set(left_map) & set(right_map))
            if len(shared) < 2:
                continue
            order_first = left_map[shared[0]] < right_map[shared[0]]
            order_second = left_map[shared[-1]] < right_map[shared[-1]]
            if order_first != order_second:
                window_id = next(
                    (candidate["window"] for candidate in left["candidates"]
                     if candidate["segment"] == shared[0]), None)
                flips.append({"window": window_id,
                              "frames": [left["frame_id"], right["frame_id"]],
                              "names": [left["frame_name"], right["frame_name"]]})
    return flips


def build_calibration(db, deployment, form):
    windows = parse_windows(form)
    tolerance = integer(form.get("residual_tolerance_s"), "residual tolerance", 1, 86400,
                        default=MAX_RESIDUAL_S)
    frames = db.matching("frames", where=(("deployment_id", "=", deployment["id"]),),
                         order=("stamp_utc", "ASC"), limit=MAX_CALIBRATION_FRAMES + 1)
    if len(frames) > MAX_CALIBRATION_FRAMES:
        raise Limit("This deployment has too many photos for one calibration run.")
    frames_by_id = {frame["id"]: frame for frame in frames}
    anchors = parse_anchors(form, frames_by_id, deployment, windows)
    segments = build_segments(deployment, windows)
    for anchor in anchors:
        if anchor["status"] == "ready":
            target = next((seg for seg in segments if seg.index == anchor["segment"]), None)
            if target is None:
                anchor.update(status="held", reason="anchor falls outside the deployment clock segments")
            else:
                target.anchors.append(anchor)

    rejected = {}
    for segment in segments:
        model, bad = fit_segment(segment, tolerance)
        segment.model = model
        rejected.update(bad)

    memberships = db.membership([frame["id"] for frame in frames])
    frame_rows, ambiguous = [], []
    ready_count = blocked_count = unchanged_count = 0
    for frame in frames:
        camera_instant = raw_camera_instant(frame, deployment)
        row = {
            "frame_id": frame["id"], "version": frame["version"], "frame_name": frame["name"],
            "previous_utc": frame["stamp_utc"], "proposed_utc": None, "status": "unresolved",
            "reason": None, "candidates": [], "event_id": memberships.get(frame["id"]),
            "camera_utc": iso(camera_instant) if camera_instant else None,
        }
        if camera_instant is None:
            row["status"] = "unresolved"
            row["reason"] = ("already corrected by a clock calibration"
                             if frame["time"].get("basis") == "piecewise clock calibration"
                             else "no camera-clock reading to calibrate")
            frame_rows.append(row)
            continue
        candidates = candidate_segments(camera_instant, windows, segments)
        if not candidates:
            row["status"] = "outside calibration"
            row["reason"] = "no fitted clock segment covers this camera reading"
            frame_rows.append(row)
            continue
        candidate_segs = candidates
        row["candidates"] = [
            {"segment": seg.index, "segment_label": seg.label,
             "window": shared_window(seg.index, candidate_segs, windows),
             "proposed_utc": iso(apply_model(camera_instant, seg.model))}
            for seg in candidate_segs
        ]
        if row["event_id"]:
            row["status"] = "blocked"
            row["reason"] = "photo belongs to a saved trigger event; detach or rebuild the event first"
            blocked_count += 1
            frame_rows.append(row)
            continue
        if len(candidates) > 1:
            row["status"] = "held"
            row["reason"] = "camera reading fits either side of a battery-swap window"
            row["proposed_utc"] = None
            ambiguous.append(row)
            frame_rows.append(row)
            continue
        proposed_text = row["candidates"][0]["proposed_utc"]
        row["proposed_utc"] = proposed_text
        window_state = time_window(proposed_text, deployment)
        if window_state != "inside":
            row["status"] = "blocked"
            row["reason"] = f"calibrated time falls {window_state}"
            blocked_count += 1
            frame_rows.append(row)
            continue
        if proposed_text == frame["stamp_utc"]:
            row["status"] = "unchanged"
            unchanged_count += 1
            frame_rows.append(row)
            continue
        row["status"] = "ready"
        ready_count += 1
        frame_rows.append(row)

    flips = order_flips(ambiguous)
    flip_frames = {frame_id for flip in flips for frame_id in flip["frames"]}
    for row in ambiguous:
        if row["frame_id"] in flip_frames:
            row["reason"] += "; photo order reverses between the two clock interpretations"

    held_frame_ids = {row["frame_id"] for row in frame_rows if row["status"] == "held"}
    anchor_holds = [anchor for anchor in anchors if anchor["status"] == "held"]
    for anchor in anchor_holds:
        held_frame_ids.add(anchor["frame_id"])

    segment_views = [
        {"index": seg.index, "label": seg.label,
         "start_utc": iso(seg.start), "end_utc": iso(seg.end),
         "anchor_count": len(seg.anchors), "model": seg.model}
        for seg in segments
    ]
    public_anchors = [
        {key: value for key, value in anchor.items() if key not in ("camera_instant", "watch_time")}
        for anchor in anchors
    ]
    return {
        "deployment": deployment["id"], "deployment_version": deployment["version"],
        "residual_tolerance_s": tolerance,
        "swap_windows": [{key: value for key, value in window.items() if not isinstance(value, datetime)}
                         for window in windows],
        "segments": segment_views,
        "anchors": public_anchors,
        "frames": frame_rows,
        "order_flips": flips,
        "totals": Counter(row["status"] for row in frame_rows) | Counter(
            anchor["status"] for anchor in public_anchors),
        "counts": {"ready": ready_count, "held": len(held_frame_ids), "blocked": blocked_count,
                   "unchanged": unchanged_count, "anchor_holds": len(anchor_holds),
                   "order_flips": len(flips)},
    }


def preview_calibration(store, form):
    group = identifier(form.get("deployment_id"), "deployment")
    with store.use() as db:
        deployment = db.one("deployments", group)
        return build_calibration(db, deployment, form)


def hold_payload(reason, calibration_id, candidates=None):
    return {"reason": reason, "calibration_id": calibration_id,
            "candidates": candidates or []}


def apply_calibration(store, form):
    group = identifier(form.get("deployment_id"), "deployment")
    selected = identity_list(form.get("selected"), "selected", limit=MAX_CALIBRATION_FRAMES)
    versions = form.get("versions")
    if not isinstance(versions, dict):
        raise SurveyError("Refresh the clock calibration preview before applying it.")
    with store.use(write=True) as db:
        deployment = db.one("deployments", group)
        preview = build_calibration(db, deployment, form)
        if preview["deployment_version"] != form.get("deployment_version"):
            raise Conflict("The deployment changed. Refresh the clock calibration preview.")
        rows = {row["frame_id"]: row for row in preview["frames"]}
        wanted = set(selected)
        if not wanted or any(key not in rows for key in wanted):
            raise Conflict("A selected photo is missing from the current preview. Refresh it.")
        if any(rows[key]["status"] != "ready" for key in wanted):
            raise Conflict("Only photos marked ready can be applied. Held photos need manual review.")
        record = db.append("calibrations", {
            "name": word(form.get("name"), "calibration name",
                         default=f"Clock calibration {deployment['name'][:40]}"),
            "deployment_id": group,
            "state": "applied",
            "snapshot": preview,
        }, action="piecewise clock calibration applied")
        calibration_id = record["id"]
        all_frames = db.matching("frames", where=(("deployment_id", "=", group),), limit=MAX_CALIBRATION_FRAMES + 1)
        current = {frame["id"]: frame for frame in all_frames}
        changed = []
        for frame_id in wanted:
            detail_row = rows[frame_id]
            frame = current[frame_id]
            expected = versions.get(frame_id)
            if type(expected) is not int:
                raise Conflict("The original version of a selected photo is missing.")
            proposed_text = detail_row["proposed_utc"]
            segment_index = detail_row["candidates"][0]["segment"]
            segment = next(seg for seg in preview["segments"] if seg["index"] == segment_index)
            uncertainty = corrected_uncertainty(
                frame, preview["residual_tolerance_s"], segment["model"])
            timing = dict(frame["time"])
            timing.update({
                "utc": proposed_text,
                "basis": "piecewise clock calibration",
                "uncertainty_s": uncertainty,
                "calibration": {
                    "calibration_id": calibration_id,
                    "segment": segment_index, "segment_label": segment["label"],
                    "model": segment["model"],
                    "previous_utc": frame["stamp_utc"],
                },
            })
            patch = {
                "time": timing, "stamp_utc": proposed_text,
                "time_window": time_window(proposed_text, deployment),
                "local_stamp": local_display(proposed_text, deployment["timezone"]),
                "clock_hold": None,
            }
            changed.append(db.replace("frames", frame_id, expected,
                                      lambda old, patch=patch: old | patch,
                                      action="camera time calibrated"))

        # Held photos (ambiguous swap placement or rejected anchors) are flagged
        # for review without touching their existing timestamp or event membership.
        held_details = {row["frame_id"]: row for row in preview["frames"] if row["status"] == "held"}
        for anchor in preview["anchors"]:
            if anchor["status"] == "held":
                held_details.setdefault(anchor["frame_id"], {
                    "frame_id": anchor["frame_id"], "candidates": [], "reason": anchor["reason"]})
        held = []
        for frame_id, detail_row in held_details.items():
            if db.membership([frame_id]).get(frame_id):
                continue
            frame = db.one("frames", frame_id, optional=True)
            if frame is None:
                continue
            candidates = detail_row.get("candidates") if isinstance(detail_row.get("candidates"), list) else []
            public_candidates = [dict(candidate) for candidate in candidates]
            db.replace(
                "frames", frame_id, frame["version"],
                lambda old, detail_row=detail_row, public_candidates=public_candidates: old | {
                    "clock_hold": hold_payload(detail_row.get("reason"), calibration_id, public_candidates)},
                action="camera time held for review")
            held.append(frame_id)
    return {"calibration": record, "changed": changed, "held": held,
            "counts": preview["counts"], "order_flips": preview["order_flips"]}


def list_calibrations(store, deployment=None):
    where = (("deployment_id", "=", deployment),) if deployment else ()
    with store.use() as db:
        rows = db.matching("calibrations", where=where, order=("created_at", "DESC"), limit=100)
    return {"calibrations": [{key: value for key, value in row.items() if key != "snapshot"} for row in rows]}


def read_calibration(store, key):
    with store.use() as db:
        return db.one("calibrations", key)
