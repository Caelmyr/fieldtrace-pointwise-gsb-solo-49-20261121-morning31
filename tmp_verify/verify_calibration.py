"""End-to-end checks for piecewise clock calibration.

Run with: python3 -I tmp_verify/verify_calibration.py
The script lives outside the package tree and adds every path explicitly, so
isolated mode (-I) still finds the app and its third-party dependencies.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path[:0] = ["/workspace", "/opt/pointwise-env/site-packages"]

from fieldtrace.calibration import preview_calibration, apply_calibration, order_flips  # noqa: E402
from fieldtrace.events import preview_events, resolve_preset_groups, register_event  # noqa: E402
from fieldtrace.catalog import edit_stamp  # noqa: E402
from fieldtrace.store import Store  # noqa: E402

UTC = timezone.utc


def dt(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso(stamp):
    return stamp.astimezone(UTC).isoformat(timespec="seconds")


def time_record(stamp_utc, basis="camera offset", uncertainty=0, skew=0):
    return {
        "camera_time": iso(stamp_utc + timedelta(seconds=skew)),
        "utc": iso(stamp_utc), "time_zone": "UTC", "basis": basis,
        "uncertainty_s": uncertainty, "camera_clock_fast_s": skew, "utc_candidates": [],
    }


FRAME_SEQ = 0


def make_frame(db, deployment, name, stamp_utc, skew):
    global FRAME_SEQ
    FRAME_SEQ += 1
    digest = f"{FRAME_SEQ:064x}"
    return db.append("frames", {
        "deployment_id": deployment["id"], "station_id": deployment["station_id"],
        "name": name, "digest": digest,
        "illumination": "day-color", "review": "new", "origin": "upload",
        "time": time_record(stamp_utc, skew=skew), "stamp_utc": iso(stamp_utc),
        "time_window": "inside", "local_stamp": {"wall_time": iso(stamp_utc)},
        "extent": [640, 480], "source_extent": [640, 480], "source_media": "image/jpeg",
        "depth": 3, "colour": "RGB", "supported_fraction": 1.0, "camera": {},
        "note": "", "taxa": [], "review_note": "", "reviewed_at": None,
    }, action="test frame")


def main():
    tmp = tempfile.mkdtemp(prefix="fieldtrace-verify-")
    store = Store(tmp)
    failures = []

    def check(label, condition, detail=""):
        status = "ok  " if condition else "FAIL"
        print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    with store.use(write=True) as db:
        station = db.append("stations", {
            "name": "Verify station", "habitat": "woodland", "timezone": "UTC",
            "code": "V1", "note": "", "private": False})
        deployment = db.append("deployments", {
            "station_id": station["id"], "name": "Verify camera", "camera": "CAM-1",
            "timezone": "UTC", "started_at": "2026-10-01T00:00:00+00:00",
            "ended_at": "2026-10-10T00:00:00+00:00", "state": "active",
            "clock_fast_s": 0, "clock_uncertainty_s": 0, "note": ""})

        # True timeline. Camera runs fast: +30 s at Oct 2, drifting +10 s/day.
        # Battery swap window Oct 4 10:00-12:00 UTC; the clock jumps +120 s.
        def skew_before(true_t):
            days = (true_t - dt("2026-10-01T00:00:00+00:00")).total_seconds() / 86400
            return 30 + 10 * days

        def skew_after(true_t):
            return skew_before(true_t) + 120

        anchor_true = [
            dt("2026-10-01T12:00:00+00:00"),
            dt("2026-10-02T08:00:00+00:00"),
            dt("2026-10-03T08:00:00+00:00"),
            dt("2026-10-05T08:00:00+00:00"),
            dt("2026-10-06T20:00:00+00:00"),
        ]
        frames = {}
        for i, true_t in enumerate(anchor_true):
            skew = skew_before(true_t) if i < 3 else skew_after(true_t)
            frames[f"anchor{i}"] = make_frame(
                db, deployment, f"ANCHOR_{i}.jpg", true_t + timedelta(seconds=skew), 0)

        # Regular photos on both sides of the swap.
        regular_before = make_frame(db, deployment, "BEFORE_1.jpg",
                                    dt("2026-10-03T20:00:00+00:00") + timedelta(seconds=skew_before(dt("2026-10-03T20:00:00+00:00"))), 0)
        regular_after = make_frame(db, deployment, "AFTER_1.jpg",
                                   dt("2026-10-05T20:00:00+00:00") + timedelta(seconds=skew_after(dt("2026-10-05T20:00:00+00:00"))), 0)

        # Ambiguous frames: raw camera readings inside the swap window and close
        # enough together that a 120 s jump reverses their true order under the
        # two side interpretations.
        ambiguous_a = make_frame(db, deployment, "SWAP_A.jpg",
                                 dt("2026-10-04T10:46:00+00:00"), 0)
        ambiguous_b = make_frame(db, deployment, "SWAP_B.jpg",
                                 dt("2026-10-04T10:45:00+00:00"), 0)

        # Bad anchor: claims a watch time wildly inconsistent with its segment.
        bad = make_frame(db, deployment, "BAD_ANCHOR.jpg",
                         dt("2026-10-02T12:00:00+00:00") + timedelta(seconds=42), 0)

    swap_window = {"start": "2026-10-04T10:00:00+00:00", "end": "2026-10-04T12:00:00+00:00",
                   "note": "battery swap"}
    anchors_form = [
        {"frame": frames["anchor0"]["id"], "watch_time": iso(anchor_true[0]), "uncertainty_s": 2},
        {"frame": frames["anchor1"]["id"], "watch_time": iso(anchor_true[1]), "uncertainty_s": 2},
        {"frame": frames["anchor2"]["id"], "watch_time": iso(anchor_true[2]), "uncertainty_s": 2},
        {"frame": frames["anchor3"]["id"], "watch_time": iso(anchor_true[3]), "uncertainty_s": 2},
        {"frame": frames["anchor4"]["id"], "watch_time": iso(anchor_true[4]), "uncertainty_s": 2},
        {"frame": bad["id"], "watch_time": "2026-10-02T12:30:00+00:00", "uncertainty_s": 2},
    ]
    form = {
        "deployment_id": deployment["id"],
        "swap_windows": [swap_window],
        "anchors": anchors_form,
        "residual_tolerance_s": 10,
        "name": "Verify calibration",
    }
    preview = preview_calibration(store, form)

    check("two clock segments built", len(preview["segments"]) == 2,
          str([(s["label"], s["anchor_count"], s["model"] and s["model"]["status"]) for s in preview["segments"]]))
    fitted = [s for s in preview["segments"] if s["model"]]
    check("both segments fitted", len(fitted) == 2)
    for seg in fitted:
        check(f"segment {seg['index']} slope near unity",
              abs(seg["model"]["beta"] - 1.0) < 0.001, str(seg["model"]["beta"]))
        check(f"segment {seg['index']} fit rms under tolerance",
              seg["model"]["rms_s"] <= 10, str(seg["model"]["rms_s"]))

    anchor_status = {a["frame_name"]: a for a in preview["anchors"]}
    check("good anchors accepted", all(
        anchor_status[name]["status"] == "ready" for name in
        ("ANCHOR_0.jpg", "ANCHOR_1.jpg", "ANCHOR_2.jpg", "ANCHOR_3.jpg", "ANCHOR_4.jpg")))
    check("inconsistent anchor held", anchor_status["BAD_ANCHOR.jpg"]["status"] == "held",
          anchor_status["BAD_ANCHOR.jpg"].get("reason"))

    rows = {r["frame_name"]: r for r in preview["frames"]}
    check("regular pre-swap photo ready", rows["BEFORE_1.jpg"]["status"] == "ready",
          rows["BEFORE_1.jpg"].get("reason"))
    check("regular post-swap photo ready", rows["AFTER_1.jpg"]["status"] == "ready",
          rows["AFTER_1.jpg"].get("reason"))
    check("swap window photos held", rows["SWAP_A.jpg"]["status"] == "held"
          and rows["SWAP_B.jpg"]["status"] == "held")
    check("held photos carry both segment candidates",
          {c["segment"] for c in rows["SWAP_A.jpg"]["candidates"]} == {0, 1})

    true_before = dt("2026-10-03T20:00:00+00:00")
    proposed = dt(rows["BEFORE_1.jpg"]["proposed_utc"])
    check("calibrated time recovers true time within 5 s",
          abs((proposed - true_before).total_seconds()) <= 5,
          f"proposed={proposed} true={true_before}")

    check("swap interpretations give distinct corrected times",
          len({c["proposed_utc"] for c in rows["SWAP_A.jpg"]["candidates"]}) == 2
          and len({c["proposed_utc"] for c in rows["SWAP_B.jpg"]["candidates"]}) == 2,
          str(rows["SWAP_A.jpg"]["candidates"]))

    # Unit-level proof that genuine order reversals are surfaced.
    flip_pair = [
        {"frame_id": "a", "frame_name": "A", "candidates": [
            {"segment": 0, "window": 1, "proposed_utc": "2026-10-04T10:00:00+00:00"},
            {"segment": 1, "window": 1, "proposed_utc": "2026-10-04T10:02:00+00:00"}]},
        {"frame_id": "b", "frame_name": "B", "candidates": [
            {"segment": 0, "window": 1, "proposed_utc": "2026-10-04T10:01:00+00:00"},
            {"segment": 1, "window": 1, "proposed_utc": "2026-10-04T10:00:30+00:00"}]},
    ]
    check("order reversal detector flags genuine flips",
          len(order_flips(flip_pair)) == 1 and order_flips(flip_pair)[0]["frames"] == ["a", "b"])

    # Applying must reject attempts to include held rows.
    rejected = False
    try:
        apply_calibration(store, form | {
            "deployment_version": preview["deployment_version"],
            "selected": [rows["BEFORE_1.jpg"]["frame_id"], rows["SWAP_A.jpg"]["frame_id"]],
            "versions": {rows["BEFORE_1.jpg"]["frame_id"]: rows["BEFORE_1.jpg"]["version"],
                         rows["SWAP_A.jpg"]["frame_id"]: rows["SWAP_A.jpg"]["version"]}})
    except Exception as exc:  # noqa: BLE001
        rejected = True
        print("       rejected mixed apply:", str(exc)[:80])
    check("apply refuses held photos", rejected)

    ready_ids = [r["frame_id"] for r in preview["frames"] if r["status"] == "ready"]
    versions = {r["frame_id"]: r["version"] for r in preview["frames"]}
    result = apply_calibration(store, form | {
        "deployment_version": preview["deployment_version"],
        "selected": ready_ids, "versions": versions})
    check("ready photos corrected", len(result["changed"]) == len(ready_ids),
          f"{len(result['changed'])} vs {len(ready_ids)}")
    check("ambiguous and bad-anchor photos held",
          set(result["held"]) >= {rows["SWAP_A.jpg"]["frame_id"], rows["SWAP_B.jpg"]["frame_id"],
                                  bad["id"]}, str(result["held"]))

    with store.use() as db:
        corrected = db.one("frames", rows["BEFORE_1.jpg"]["frame_id"])
        check("corrected basis recorded", corrected["time"]["basis"] == "piecewise clock calibration")
        check("calibration lineage retained",
              corrected["time"]["calibration"]["calibration_id"] == result["calibration"]["id"])
        swap_a = db.one("frames", rows["SWAP_A.jpg"]["frame_id"])
        check("held frame keeps original timestamp", swap_a["stamp_utc"] == rows["SWAP_A.jpg"]["previous_utc"])
        check("held frame records reason and candidates",
              bool(swap_a["clock_hold"]["reason"]) and len(swap_a["clock_hold"]["candidates"]) == 2)
        bad_frame = db.one("frames", bad["id"])
        check("bad anchor flagged for review", bool(bad_frame["clock_hold"]))

    # Auto grouping after apply still excludes the held photos.
    events_preview2 = preview_events(store, {"deployment_id": deployment["id"], "gap_s": 3600})
    held_after = {item["frame_id"] for item in events_preview2["held"]}
    check("held frames stay out of new trigger groups",
          {rows["SWAP_A.jpg"]["frame_id"], rows["SWAP_B.jpg"]["frame_id"], bad["id"]} <= held_after)

    # Manual group approval also refuses held frames.
    blocked = False
    if events_preview2["groups"]:
        try:
            resolve_preset_groups(store, {"deployment_id": deployment["id"], "gap_s": 3600,
                                          "groups": [g["ids"] for g in events_preview2["groups"]]
                                          + [[rows["SWAP_A.jpg"]["frame_id"]]]})
        except Exception as exc:  # noqa: BLE001
            blocked = True
            print("       rejected held group:", str(exc)[:80])
    check("group approval blocks held frames", blocked)

    # Manual event creation with a held frame is refused.
    blocked_manual = False
    try:
        register_event(store, {"deployment_id": deployment["id"], "name": "blocked",
                               "frames": [rows["SWAP_A.jpg"]["frame_id"]]})
    except Exception as exc:  # noqa: BLE001
        blocked_manual = True
        print("       rejected manual event:", str(exc)[:80])
    check("manual event rejects held frame", blocked_manual)

    # Event protection and manual hold resolution need a pristine deployment:
    # after apply, anchor frames carry a calibration basis and can no longer be
    # re-used as anchors, so a second store keeps the scenarios independent.
    store2 = Store(tempfile.mkdtemp(prefix="fieldtrace-verify2-"))
    with store2.use(write=True) as db2:
        station2 = db2.append("stations", {
            "name": "Guard station", "habitat": "woodland", "timezone": "UTC",
            "code": "G1", "note": "", "private": False})
        dep2 = db2.append("deployments", {
            "station_id": station2["id"], "name": "Guard camera", "camera": "CAM-2",
            "timezone": "UTC", "started_at": "2026-10-01T00:00:00+00:00",
            "ended_at": "2026-10-10T00:00:00+00:00", "state": "active",
            "clock_fast_s": 0, "clock_uncertainty_s": 0, "note": ""})
        anchor2_ids, true2 = [], []
        for i, true_t in enumerate(anchor_true):
            sk = skew_before(true_t) if i < 3 else skew_after(true_t)
            true2.append(true_t)
            anchor2_ids.append(make_frame(
                db2, dep2, f"G_ANCHOR_{i}.jpg", true_t + timedelta(seconds=sk), 0)["id"])
        guarded_true = dt("2026-10-06T12:00:00+00:00")
        guarded = make_frame(db2, dep2, "GUARDED_EVENT.jpg",
                             guarded_true + timedelta(seconds=skew_after(guarded_true)), 0)
        held_guard = make_frame(db2, dep2, "HELD_GUARD.jpg",
                                dt("2026-10-04T10:46:00+00:00"), 0)
    form2 = {
        "deployment_id": dep2["id"], "swap_windows": [swap_window],
        "residual_tolerance_s": 10, "name": "Guard calibration",
        "anchors": [{"frame": anchor2_ids[i], "watch_time": iso(true2[i]), "uncertainty_s": 2}
                    for i in range(len(anchor2_ids))],
    }
    with store2.use(write=True) as db2:
        target_id = guarded["id"]
        event = db2.append("events", {
            "name": "Confirmed group", "deployment_id": dep2["id"],
            "stamp_utc": db2.one("frames", target_id)["stamp_utc"], "state": "confirmed",
            "caption": "", "gap_s": 45,
            "time_span": {"start_utc": db2.one("frames", target_id)["stamp_utc"],
                          "end_utc": db2.one("frames", target_id)["stamp_utc"], "duration_s": 0}})
        db2.set_event_members(event["id"], [target_id])
    preview2 = preview_calibration(store2, form2)
    row2 = next(r for r in preview2["frames"] if r["frame_id"] == target_id)
    check("event-linked photo blocked from calibration", row2["status"] == "blocked", row2.get("reason"))

    # Apply on the pristine store, then confirm manual review clears a hold.
    ready2 = [r["frame_id"] for r in preview2["frames"] if r["status"] == "ready"]
    versions2 = {r["frame_id"]: r["version"] for r in preview2["frames"]}
    apply_calibration(store2, form2 | {
        "deployment_version": preview2["deployment_version"],
        "selected": ready2, "versions": versions2})
    with store2.use() as db2:
        held_frame = db2.one("frames", held_guard["id"])
        held_version = held_frame["version"]
        check("pristine store flags ambiguous frame", bool(held_frame["clock_hold"]))
    try:
        edit_stamp(store2, held_guard["id"], {
            "mode": "utc", "stamp": "2026-10-04T10:31:00+00:00",
            "uncertainty_s": 5, "version": held_version})
    except Exception as exc:  # noqa: BLE001
        print("       edit_stamp error:", repr(exc))
    with store2.use() as db2:
        cleared = db2.one("frames", held_guard["id"])
    check("manual review clears clock hold", cleared["clock_hold"] is None, str(cleared["clock_hold"]))
    check("manual review records direct time", cleared["time"]["basis"] == "direct operator time",
          cleared["time"]["basis"])
    # Anchor inside a swap window with unknown side is held, explicit side accepted.
    with store.use(write=True) as db:
        inside = make_frame(db, deployment, "WINDOW_ANCHOR.jpg",
                            dt("2026-10-04T11:30:00+00:00") + timedelta(seconds=160), 0)
    form_unknown = form | {"anchors": anchors_form + [
        {"frame": inside["id"], "watch_time": "2026-10-04T11:30:00+00:00", "uncertainty_s": 2}]}
    pv = preview_calibration(store, form_unknown)
    inside_row = next(a for a in pv["anchors"] if a["frame_id"] == inside["id"])
    check("window anchor without side is held", inside_row["status"] == "held", inside_row.get("reason"))
    form_side = form | {"anchors": anchors_form + [
        {"frame": inside["id"], "watch_time": "2026-10-04T11:30:00+00:00",
         "uncertainty_s": 2, "side": "after"}]}
    pv2 = preview_calibration(store, form_side)
    inside_row2 = next(a for a in pv2["anchors"] if a["frame_id"] == inside["id"])
    check("window anchor with explicit side accepted", inside_row2["status"] == "ready",
          inside_row2.get("reason"))

    # Overlapping swap windows are rejected.
    bad_windows = False
    try:
        preview_calibration(store, form | {"swap_windows": [
            swap_window, {"start": "2026-10-04T11:00:00+00:00", "end": "2026-10-04T13:00:00+00:00"}]})
    except Exception as exc:  # noqa: BLE001
        bad_windows = True
        print("       rejected overlap:", str(exc)[:80])
    check("overlapping swap windows rejected", bad_windows)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        sys.exit(1)
    print("all calibration checks passed")


if __name__ == "__main__":
    main()
