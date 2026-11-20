"""Regression checks: existing demo flows keep working after clock work."""
import sys
import tempfile

sys.path[:0] = ["/workspace", "/opt/pointwise-env/site-packages"]

from fieldtrace.web import create_app  # noqa: E402
from fieldtrace.demo import create_demo  # noqa: E402


def main():
    app = create_app(tempfile.mkdtemp(prefix="fieldtrace-reg-"))
    client = app.test_client()
    store = app.extensions["fieldtrace"]
    failures = []

    def check(label, condition, detail=""):
        print(f"[{'ok  ' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    demo = create_demo(store)
    check("demo survey created", len(demo["deployments"]) == 2)

    # Stations rollup still works and now reports clock_hold_count.
    stations = client.get("/api/stations").get_json()
    check("station rollup includes clock hold count",
          all("clock_hold_count" in dep for dep in stations["deployments"]))
    check("demo starts with no clock holds",
          all(dep["clock_hold_count"] == 0 for dep in stations["deployments"]))

    dep_id = demo["deployments"][0]
    # Existing event preview flow still returns the demo groups.
    event_prev = client.post("/api/events/preview",
                             json={"deployment_id": dep_id, "gap_s": 45}).get_json()
    check("demo event preview still runs", "groups" in event_prev)

    # Frames still serialize fully and carry an absent clock_hold key.
    frames = client.get(f"/api/frames?deployment_id={dep_id}&limit=5").get_json()["frames"]
    check("frames list returns data", len(frames) > 0)
    check("fresh frames have no clock hold", all(not f.get("clock_hold") for f in frames))

    # A calibration preview with no swap windows still fits a single segment.
    frame_id = frames[0]["id"]
    frame = client.get(f"/api/frames/{frame_id}").get_json()
    watch = frame["stamp_utc"]
    preview = client.post("/api/clock-calibration/preview", json={
        "deployment_id": dep_id, "swap_windows": [],
        "anchors": [{"frame": frame_id, "watch_time": watch, "uncertainty_s": 1}],
        "residual_tolerance_s": 120}).get_json()
    check("single-segment preview works", len(preview["segments"]) == 1)
    check("single anchor model flagged", preview["segments"][0]["model"]["status"] == "single anchor offset")

    # Calibration listing endpoint works even before any apply.
    listing = client.get("/api/clock-calibration").get_json()
    check("empty calibration listing", listing == {"calibrations": []})

    # Health endpoint still reports ready.
    health = client.get("/api/health").get_json()
    check("health ready", health["status"] == "ready")

    print()
    if failures:
        print(f"{len(failures)} regression check(s) failed")
        sys.exit(1)
    print("all regression checks passed")


if __name__ == "__main__":
    main()
