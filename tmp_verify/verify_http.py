"""HTTP-level checks for the clock calibration API via the Flask test client."""
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path[:0] = ["/workspace", "/opt/pointwise-env/site-packages"]

from fieldtrace.web import create_app  # noqa: E402

UTC = timezone.utc


def iso(stamp):
    return stamp.astimezone(UTC).isoformat(timespec="seconds")


def dt(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def main():
    app = create_app(tempfile.mkdtemp(prefix="fieldtrace-http-"))
    client = app.test_client()
    failures = []

    def check(label, condition, detail=""):
        print(f"[{'ok  ' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    def post(route, payload):
        resp = client.post(route, json=payload)
        return resp.status_code, resp.get_json()

    station_code, station = post("/api/stations", {
        "name": "HTTP station", "habitat": "woodland", "timezone": "UTC"})
    check("station created", station_code == 201, str(station))
    station_id = station["id"]
    dep_code, dep = post("/api/deployments", {
        "station_id": station_id, "name": "HTTP camera", "timezone": "UTC",
        "started_at": "2026-10-01T00:00:00+00:00", "ended_at": "2026-10-10T00:00:00+00:00",
        "clock_fast_s": 0, "clock_uncertainty_s": 0})
    check("deployment created", dep_code == 201, str(dep))
    dep_id = dep["id"]

    # Insert frames directly through the store with raw camera timestamps.
    store = app.extensions["fieldtrace"]
    seq = 0

    def add_frame(name, stamp):
        nonlocal seq
        seq += 1
        with store.use(write=True) as db:
            return db.append("frames", {
                "deployment_id": dep_id, "station_id": station_id, "name": name,
                "digest": f"{seq:064x}", "illumination": "day-color", "review": "new",
                "origin": "upload",
                "time": {"camera_time": iso(stamp), "utc": iso(stamp), "time_zone": "UTC",
                         "basis": "camera offset", "uncertainty_s": 0,
                         "camera_clock_fast_s": 0, "utc_candidates": []},
                "stamp_utc": iso(stamp), "time_window": "inside",
                "local_stamp": {"wall_time": iso(stamp)}, "extent": [640, 480],
                "source_extent": [640, 480], "source_media": "image/jpeg", "depth": 3,
                "colour": "RGB", "supported_fraction": 1.0, "camera": {}, "note": "",
                "taxa": [], "review_note": "", "reviewed_at": None})

    def skew_before(t):
        days = (t - dt("2026-10-01T00:00:00+00:00")).total_seconds() / 86400
        return 30 + 10 * days

    anchor_true = [dt("2026-10-01T12:00:00+00:00"), dt("2026-10-02T08:00:00+00:00"),
                   dt("2026-10-03T08:00:00+00:00"), dt("2026-10-05T08:00:00+00:00"),
                   dt("2026-10-06T20:00:00+00:00")]
    anchor_ids = []
    for i, true_t in enumerate(anchor_true):
        skew = skew_before(true_t) if i < 3 else skew_before(true_t) + 120
        anchor_ids.append(add_frame(f"ANC_{i}.jpg", true_t + timedelta(seconds=skew))["id"])
    regular = add_frame("REG.jpg",
                        dt("2026-10-03T20:00:00+00:00")
                        + timedelta(seconds=skew_before(dt("2026-10-03T20:00:00+00:00"))))
    held_frame = add_frame("SWAP.jpg", dt("2026-10-04T10:46:00+00:00"))

    payload = {
        "deployment_id": dep_id,
        "swap_windows": [{"start": "2026-10-04T10:00:00+00:00",
                          "end": "2026-10-04T12:00:00+00:00"}],
        "anchors": [{"frame": anchor_ids[i], "watch_time": iso(anchor_true[i]), "uncertainty_s": 2}
                    for i in range(5)],
        "residual_tolerance_s": 10, "name": "HTTP calibration",
    }
    code, preview = post("/api/clock-calibration/preview", payload)
    check("preview endpoint 200", code == 200, str(preview)[:200])
    check("preview exposes two segments", len(preview["segments"]) == 2)
    by_name = {r["frame_name"]: r for r in preview["frames"]}
    check("regular frame ready over http", by_name["REG.jpg"]["status"] == "ready")
    check("swap frame held over http", by_name["SWAP.jpg"]["status"] == "held")
    check("held frame lists two candidates", len(by_name["SWAP.jpg"]["candidates"]) == 2)

    # Validation errors come back as structured 400s.
    bad_code, bad = post("/api/clock-calibration/preview", payload | {"anchors": []})
    check("empty anchors rejected", bad_code == 400 and bad["error"]["field"] == "anchors", str(bad))
    overlap_code, overlap = post("/api/clock-calibration/preview", payload | {"swap_windows": [
        {"start": "2026-10-04T10:00:00+00:00", "end": "2026-10-04T12:00:00+00:00"},
        {"start": "2026-10-04T11:00:00+00:00", "end": "2026-10-04T13:00:00+00:00"}]})
    check("overlapping windows rejected over http", overlap_code == 400, str(overlap)[:120])

    ready_ids = [r["frame_id"] for r in preview["frames"] if r["status"] == "ready"]
    versions = {r["frame_id"]: r["version"] for r in preview["frames"]}
    apply_code, applied = post("/api/clock-calibration/apply", payload | {
        "deployment_version": preview["deployment_version"],
        "selected": ready_ids, "versions": versions})
    check("apply endpoint 201", apply_code == 201, str(applied)[:200])
    check("http apply reports held frame", held_frame["id"] in applied["held"])

    frame_resp = client.get(f"/api/frames/{held_frame['id']}")
    body = frame_resp.get_json()
    check("held frame persisted clock_hold", bool(body["clock_hold"]))
    check("held frame timestamp untouched", body["stamp_utc"] == by_name["SWAP.jpg"]["previous_utc"])

    regular_resp = client.get(f"/api/frames/{regular['id']}").get_json()
    check("corrected frame basis over http",
          regular_resp["time"]["basis"] == "piecewise clock calibration")

    # The held frame must be excluded from automatic event grouping.
    event_code, event_preview = post("/api/events/preview", {"deployment_id": dep_id, "gap_s": 3600})
    check("event preview 200", event_code == 200)
    check("held frame absent from proposed groups",
          all(held_frame["id"] not in group["ids"] for group in event_preview["groups"]))

    # Calibration directory and detail endpoints.
    listing = client.get(f"/api/clock-calibration?deployment={dep_id}").get_json()
    check("calibration listed", len(listing["calibrations"]) == 1)
    cal_id = listing["calibrations"][0]["id"]
    detail = client.get(f"/api/clock-calibration/{cal_id}").get_json()
    check("calibration detail keeps snapshot", detail["snapshot"]["deployment"] == dep_id)
    record = client.get(f"/api/records/calibrations/{cal_id}").get_json()
    check("generic record endpoint serves calibration", record["id"] == cal_id)

    # Manual resolution clears the hold through the existing time endpoint.
    clear_resp = client.put(f"/api/frames/{held_frame['id']}/time", json={
        "mode": "utc", "stamp": "2026-10-04T10:31:00+00:00", "uncertainty_s": 5,
        "version": body["version"]})
    clear_code, cleared = clear_resp.status_code, clear_resp.get_json()
    check("manual time edit 200", clear_code == 200, str(cleared)[:160])
    check("manual edit clears hold over http", cleared["clock_hold"] is None)

    print()
    if failures:
        print(f"{len(failures)} http check(s) failed")
        sys.exit(1)
    print("all http calibration checks passed")


if __name__ == "__main__":
    main()
