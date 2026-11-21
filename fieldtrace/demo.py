"""Seed a labelled fictional woodland survey through normal workflows."""

from datetime import datetime, timedelta, timezone
from threading import Lock

from .catalog import register_photo
from .config import TAXA
from .demo_art import demo_image, woodland
from .events import frame_sort, save_suggested
from .geometry import fit_model
from .images import decode, fetch_work, image_bytes, marked_preview

INITIAL_DATE = datetime(2026, 9, 17, 6, 2, 0, tzinfo=timezone.utc)
LOCK = Lock()
STATIONS = (
    ("Alder Run", "AR-07", "wetland-edge", "Alder crossing along the lower survey trail.", "brook", 131),
    ("Ridge Gate", "RG-02", "woodland", "Old timber post and wider upland trail.", "ridge", 257),
)


def seeded_catalog():
    alders = [
        (0, "day-color", None, False, False, 0.0),
        (9, "day-color", (356, 388, 184, False), False, False, 0.0),
        (14, "day-color", (421, 392, 187, False), False, False, 0.0),
        (20, "day-color", (495, 386, 182, False), False, False, 0.0),
        (251, "day-color", None, True, False, 0.0),
        (256, "day-color", None, False, False, 0.0),
        (48612, "night-ir", None, False, False, 0.0),
        (48617, "night-ir", (456, 379, 186, True), False, False, 0.0),
        (48622, "night-ir", (390, 380, 178, True), False, True, 0.0),
        # Wind-blown empty scenes: the left grass tussock sways, nothing crosses the trail.
        (7200, "day-color", None, False, False, 0.15),
        (7230, "day-color", None, False, False, 0.62),
        (7265, "day-color", None, False, False, 0.38),
        (7300, "day-color", None, False, False, 0.85),
        # A small fox enters from the left tussock, so half its outline overlaps moving grass.
        (7342, "day-color", (158, 402, 196, False), False, False, 0.47),
    ]
    ridges = [
        (1320, "day-color", None, False, False, 0.0),
        (1325, "day-color", (382, 370, 140, False), False, False, 0.0),
        (1330, "day-color", (439, 373, 142, False), False, False, 0.0),
        (1351, "day-color", (505, 375, 146, False), False, False, 0.0),
        (51081, "night-ir", None, False, False, 0.0),
        (51104, "night-ir", (506, 376, 174, False), False, False, 0.0),
        (51110, "night-ir", None, True, False, 0.0),
        (8400, "day-color", None, False, False, 0.22),
        (8440, "day-color", None, False, False, 0.71),
        (8480, "day-color", None, False, False, 0.44),
    ]
    return [alders, ridges]


def station_fields(name, code, habitat, note):
    return {"name": name, "code": code, "habitat": habitat, "note": "Synthetic station · " + note,
            "timezone": "UTC", "private": False, "synthetic": True}


def deployment_fields(station, code):
    return {
        "station_id": station["id"], "name": station["name"] + " camera",
        "camera": code + " / SYN-1", "timezone": "UTC",
        "started_at": "2026-09-16T00:00:00+00:00", "ended_at": "2026-09-21T00:00:00+00:00", "state": "active",
        "clock_fast_s": 0, "clock_uncertainty_s": 1, "note": "Synthetic deployment, no field location.",
    }


def observations(db, deployment, background, data, seed, code):
    frames, metadata = [], []
    for i, (seconds, lighting, position, flag, missing, wind) in enumerate(data, 1):
        stamp = INITIAL_DATE + timedelta(seconds=seconds)
        intake = demo_image(background, seed=seed * 100 + i, light=lighting, at=stamp,
                            position=position, flagged=flag, missing=missing, wind=wind)
        frame, _ = register_photo(
            db, deployment, f"{code}_{i:03}." + ("png" if missing else "jpg"), decode(intake),
            {"illumination": lighting, "override": stamp.isoformat(timespec="seconds"),
             "note": "Original procedural teaching photo", "fold": None}, origin="synthetic",
        )
        evidence_review = {
            "review": "seen" if position is not None else "unusable" if flag else "empty",
            "taxa": ["fox"] if position is not None else [],
            "review_note": ("Synthetic annotated reading." if not flag
                            else "Veiling flare obscures the left side."),
            "reviewed_at": stamp.isoformat(timespec="seconds"),
        }
        if i not in (3, 7):
            frame = db.replace("frames", frame["id"], frame["version"],
                               lambda old: old | evidence_review, action="synthetic ground truth entered")
        if position is not None and i not in (3, 7):
            x, y, width, _ = position
            poly = photo_box(x + width * 0.12, y + width * 0.12, width * .83, width * .35, frame["extent"])
            db.append("marks", {
                "name": TAXA["fox"] + " position", "kind": "animal", "frame_id": frame["id"],
                "polygon": poly, "taxon": "fox", "certainty": "observed",
                "note": "Synthetic trace; the animal partly overlaps wind-blown grass." if wind
                        else "Synthetic trace.",
            }, action="synthetic outline entered")
        frames.append(frame)
        metadata.append({"position": position, "time": stamp.isoformat(timespec="seconds"),
                         "flags": flag, "wind": wind})
    return frames, metadata


def photo_box(x, y, w, h, extent):
    width, height = extent
    a, b, c, d = x / width, y / height, (x + w) / width, (y + h) / height
    return [(a, b), (c, b), (c, d), (a, d)]


def seed_event(db, deployment, frames, limits, use_words):
    selected = sorted(frames[limits[0]:limits[1]], key=frame_sort)
    event = save_suggested(db, deployment["id"], use_words, selected, 45)
    return db.replace("events", event["id"], 1, lambda old: old | {"state": "confirmed"},
                      action="training event confirmed")


def add_outages(db, deployment, i):
    stamp = f"2026-09-{18+i:02}"
    return db.append("outages", {
        "deployment_id": deployment["id"], "name": "Service visit", "cause": "service",
        "start_utc": stamp + "T10:20:00+00:00", "end_utc": stamp + "T13:10:00+00:00",
        "note": "Synthetic interruption used in the training effort report.",
    }, action="training camera outage")


def ground_model(db, frame, deployment):
    anchors = [
        {"name": "Far left stake", "image": (.34,.58), "ground_m": (-1.8, 5.5)},
        {"name": "Far right stake", "image": (.64,.58), "ground_m": (1.8, 5.5)},
        {"name": "Near left wand", "image": (.21,.87), "ground_m": (-1.8, 1.0)},
        {"name": "Near right wand", "image": (.78,.87), "ground_m": (1.8, 1.0)},
    ]
    result = fit_model(anchors, .10)
    return db.append("models", result | {
        "name":"Training ground plane", "deployment_id":deployment["id"], "frame_id":frame["id"],
        "source_digest":frame["digest"], "source_extent":frame["extent"], "maximum_error_m":.1,
        "note":"Synthetic landmarks for visualizing the calibration and measuring paths.",
    }, action="training ground plane fitted")


def witness(db, frame):
    subject = db.append("identities", {
        "name":"Willow", "taxon":"fox", "status":"provisional",
        "features":"Fictional reference. Look at the shaded flank and tail edge.",
    })
    photo = fetch_work(db, frame["id"])
    x, y, w, h = 350, 398, 178, 72
    sx, sy = frame["extent"]
    tile = marked_preview(photo.crop((x, y, x+w,y+h)), [])
    key = db.append("portraits", {
        "identity_id":subject["id"], "frame_id":frame["id"], "name":"Daytime left-flank witness",
        "pose":"left", "box":[x/sx,y/sy,w/sx,h/sy], "certainty":"probable",
        "note":"Synthetic face side and body marking.",
        "source_digest":frame["digest"], "source_extent":frame["extent"],
        "source_version":frame["version"], "identity_version":subject["version"],
        "illumination":frame["illumination"], "witness_time":frame["stamp_utc"],
        "quality":{"bounds_px":[x,y,x+w,y+h],"width_px":w,"height_px":h,
                   "supported_fraction":1,"clear_fraction":1,"edge_detail":None},
        "mask_versions":{},
    })
    db.add_asset(key["id"], "guide", image_bytes(tile), "image/png")
    return subject


def create_demo(store):
    with LOCK:
        with store.use() as db:
            existing = db.preference("training_survey")
            if existing:
                return existing | {"reused": True}
        landscapes = [woodland(name, seed) for _, _, _, _, name, seed in STATIONS]
        records = seeded_catalog()
        result = {"stations":[], "deployments":[], "events":[], "frames":[], "ground_models":[],
                  "synthetic":True}
        with store.use(write=True) as db:
            for i, (name, code, habitat, note, _, seed) in enumerate(STATIONS):
                station = db.append("stations", station_fields(name + " · training", code, habitat, note))
                deployment = db.append("deployments", deployment_fields(station, code))
                frames, metadata = observations(db, deployment, landscapes[i], records[i], seed, code)
                groups = [(0,4,"Trail crossing"), (4,6,"Lighting disturbance"), (6,9,"IR trail activity")]
                if i:
                    groups = [(0,4,"Ridge daytime sequence"), (4,7,"Ridge infrared sample")]
                events = [seed_event(db, deployment, frames, (a,b), label) for a,b,label in groups]
                add_outages(db, deployment, i)
                plane = ground_model(db, frames[0], deployment)
                if i == 0:
                    entry = witness(db, frames[1])
                    result["witness"] = entry["id"]
                result["stations"].append(station["id"])
                result["deployments"].append(deployment["id"])
                result["frames"].extend(frame["id"] for frame in frames)
                result["events"].extend(event["id"] for event in events)
                result["ground_models"].append(plane["id"])
            db.set_preference("training_survey", result)
    return result | {"reused":False}
