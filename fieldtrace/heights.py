"""Audit standing heights against known reference poles on a fitted ground plane.

Each pole supplies a ground contact, a top and its real height.  In the
top-down ground coordinates of an existing plane, every vertical line meets at
the camera nadir, so the poles jointly fix that point and the camera height
without comparing raw pixel lengths.  A pole that misses the common vertical
direction or disagrees on camera height is reported and left out.  An animal is
measured only when both its ground contact and shoulder top are visible and the
line between them follows the calibrated vertical direction; otherwise the
entry is retained with no number and the reason it is blank.
"""

from math import atan, degrees, sqrt

import numpy as np
from PIL import Image, ImageDraw

from .config import CERTAINTIES, MAX_HEIGHT_POLES, MAX_HEIGHTS, MAX_HEIGHT_SUBJECTS, TAXA
from .contracts import (
    Limit, SurveyError, flag, identifier, list_value, need_object, number, option,
    unit_point, word,
)
from .geometry import contains, transform_points
from .images import fetch_work, label, pixel_units, show_support

NADIR_STD_LIMIT_M = 2.0
MIN_POLE_PIXELS = 0.004
MIN_SUBJECT_PIXELS = 0.004
PLAUSIBLE_HEIGHT_M = (0.15, 3.0)
MIN_CAMERA_M = (0.5, 8.0)


def pole_fields(raw, index):
    source = need_object(raw, "pole")
    return {
        "name": word(source.get("name") or f"Pole {index}", "pole name", max_length=48),
        "base": unit_point(source.get("base"), "pole base"),
        "top": unit_point(source.get("top"), "pole top"),
        "height_m": number(source.get("height_m"), "pole height", 0.2, 5.0),
    }


def subject_fields(raw, index):
    source = need_object(raw, "subject")
    contact = unit_point(source["contact"], "ground contact") if source.get("contact") else None
    shoulder = unit_point(source["shoulder"], "shoulder top") if source.get("shoulder") else None
    contact_visible = flag(source.get("contact_visible"), "contact_visible",
                           default=contact is not None)
    shoulder_visible = flag(source.get("shoulder_visible"), "shoulder_visible",
                            default=shoulder is not None)
    if contact_visible and contact is None:
        raise SurveyError("Mark the visible ground contact, or record it as not visible.", field="contact")
    if shoulder_visible and shoulder is None:
        raise SurveyError("Mark the visible shoulder top, or record the shoulder as not visible.", field="shoulder")
    return {
        "name": word(source.get("name") or f"Animal {index}", "animal name", max_length=48),
        "taxon": option(source.get("taxon"), "taxon", TAXA, default="deer"),
        "certainty": option(source.get("certainty"), "certainty", CERTAINTIES, default="observed"),
        "contact_visible": contact_visible,
        "shoulder_visible": shoulder_visible,
        "contact": contact,
        "shoulder": shoulder,
        "note": word(source.get("note"), "subject note", max_length=600, default="", multiline=True),
    }


def parse_heights(raw):
    poles = [pole_fields(item, i) for i, item in enumerate(
        list_value(raw.get("poles"), "poles", min_items=2, max_items=MAX_HEIGHT_POLES), 1)]
    if len({row["name"] for row in poles}) != len(poles):
        raise SurveyError("Give each reference pole a distinct name.", field="poles")
    for row in poles:
        if np.linalg.norm(np.subtract(row["top"], row["base"])) < MIN_POLE_PIXELS:
            raise SurveyError("A pole needs distinct ground and top points.", field="poles")
    subjects = [subject_fields(item, i) for i, item in enumerate(
        list_value(raw.get("subjects"), "subjects", min_items=1, max_items=MAX_HEIGHT_SUBJECTS), 1)]
    if len({row["name"] for row in subjects}) != len(subjects):
        raise SurveyError("Give each animal a distinct name.", field="subjects")
    for row in subjects:
        if row["contact"] and row["shoulder"] and np.linalg.norm(
                np.subtract(row["shoulder"], row["contact"])) < MIN_SUBJECT_PIXELS:
            raise SurveyError("Keep the shoulder top apart from the ground contact.", field="shoulder")
    return {
        "poles": poles, "subjects": subjects,
        "max_offset_m": number(raw.get("max_offset_m"), "max_offset_m", 0.01, 1.0, default=0.15),
        "max_camera_spread": number(raw.get("max_camera_spread"), "max_camera_spread", 0.02, 0.6,
                                    default=0.12),
    }


def to_ground(model, point):
    return transform_points(np.asarray(model["matrix"]), [point])[0]


def fit_nadir(lines):
    normals = np.array([row[0] for row in lines])
    offsets = np.array([row[1] for row in lines])
    nadir, *_ = np.linalg.lstsq(normals, offsets, rcond=None)
    offset_errors = np.abs(normals @ nadir - offsets)
    singular = np.linalg.svd(normals, compute_uv=False)
    spread = float(singular[-1]) if len(singular) else 0.0
    uncertainty = None
    if len(lines) > 2:
        variance = float(np.sum(offset_errors ** 2)) / (len(lines) - 2)
        covariance = variance * np.linalg.inv(normals.T @ normals)
        uncertainty = float(sqrt(max(0.0, np.linalg.eigvalsh(covariance)[-1])))
    return nadir, offset_errors, spread, uncertainty


def pole_camera_height(base, top, nadir, height_m):
    base_radius = float(np.linalg.norm(base - nadir))
    top_radius = float(np.linalg.norm(top - nadir))
    if top_radius <= base_radius + 1e-9:
        return None
    return height_m * top_radius / (top_radius - base_radius)


def plain(row):
    returned = {}
    for key, value in row.items():
        if key in {"line", "ground_base", "ground_top"}:
            continue
        if isinstance(value, np.ndarray):
            value = value.tolist()
        returned[key] = value
    return returned


def prepare_pole(model, row):
    try:
        base = to_ground(model, row["base"])
        top = to_ground(model, row["top"])
    except SurveyError:
        return row | {"usable": False, "reason": "Pole crosses the ground-plane horizon."}
    if not contains(model["domain"], row["base"]):
        return row | {"usable": False, "reason": "Pole base lies outside the calibrated ground area."}
    direction = top - base
    length = float(np.linalg.norm(direction))
    if length < 1e-6:
        return row | {"usable": False, "reason": "Pole has no ground-plane extent."}
    normal = np.array([-direction[1], direction[0]]) / length
    return row | {"usable": True, "ground_base": base, "ground_top": top,
                  "line": (normal, float(normal @ base))}


def calibrate(model, poles, settings):
    prepared = [prepare_pole(model, row) for row in poles]
    active = [row for row in prepared if row["usable"]]
    nadir = None
    uncertainty = None
    spread = 0.0
    while len(active) >= 2:
        nadir, errors, spread, uncertainty = fit_nadir([row["line"] for row in active])
        if spread < 1e-7:
            break
        measured = []
        invalid = None
        for row, error in zip(active, errors):
            camera = pole_camera_height(row["ground_base"], row["ground_top"], nadir, row["height_m"])
            row["vertical_offset_m"] = float(error)
            row["camera_height_m"] = camera
            measured.append(camera)
            if camera is None or not MIN_CAMERA_M[0] <= camera <= MIN_CAMERA_M[1]:
                invalid = row
        if invalid is not None:
            prepared[prepared.index(invalid)] = invalid | {
                "usable": False, "reason": "Pole does not imply a plausible camera height."}
            active = [row for row in active if row is not invalid]
            continue
        median = float(np.median(measured))
        worst, worst_score = None, 0.0
        for row in active:
            score = max(
                row["vertical_offset_m"] / settings["max_offset_m"],
                abs(row["camera_height_m"] / median - 1) / settings["max_camera_spread"],
            )
            if score > worst_score:
                worst, worst_score = row, score
        if worst_score <= 1.0:
            break
        reason = ("Pole line misses the common vertical direction (leaning or mispointed)."
                  if worst["vertical_offset_m"] / settings["max_offset_m"] >=
                  abs(worst["camera_height_m"] / median - 1) / settings["max_camera_spread"]
                  else "Pole height disagrees with the camera height from the other poles.")
        prepared[prepared.index(worst)] = worst | {"usable": False, "reason": reason}
        active = [row for row in active if row is not worst]
    if len(active) < 2:
        for row in active:
            prepared[prepared.index(row)] = row | {
                "usable": False, "reason": "No spare pole is left to check this pole against."}
        return {
            "status": "held",
            "reason": "Fewer than two reference poles agree on a vertical direction.",
            "poles": [plain(row | {"retained": False, "reason": row.get("reason", "Not usable.")})
                      for row in prepared],
            "retained_poles": 0, "excluded_poles": len(prepared),
            "max_offset_m": settings["max_offset_m"], "lean_sensitivity_deg": None,
            "lean_note": None,
        }
    nadir, errors, spread, uncertainty = fit_nadir([row["line"] for row in active])
    for row, error in zip(active, errors):
        row["vertical_offset_m"] = float(error)
        row["camera_height_m"] = pole_camera_height(row["ground_base"], row["ground_top"], nadir,
                                                    row["height_m"])
    cameras = [row["camera_height_m"] for row in active]
    median = float(np.median(cameras))
    reasons = []
    if spread < 1e-7:
        reasons.append("Reference poles do not bracket the vertical direction.")
    if uncertainty is not None and uncertainty > NADIR_STD_LIMIT_M:
        reasons.append(f"The vertical reference is uncertain by about {uncertainty:.1f} m.")
    if max(abs(value / median - 1) for value in cameras) > settings["max_camera_spread"]:
        reasons.append("Reference poles disagree on the camera height.")
    if reasons:
        status, reason = "held", " ".join(reasons)
    else:
        status, reason = "pass", None
    if len(active) == 2:
        status = "held"
        reason = ("Only two poles were retained. Two lines always meet somewhere, so a shared "
                  "mispointing or a matching lean cannot be ruled out; add a third pole.")
    base_radii = [float(np.linalg.norm(row["ground_base"] - nadir)) for row in active]
    representative = float(np.median(base_radii)) if base_radii else 0.0
    detected_deg = None
    if representative > 1e-6:
        detected_deg = degrees(atan(settings["max_offset_m"] / representative))
    entries = [plain(row | ({"retained": True, "reason": None} if row in active else {
        "retained": False, "reason": row.get("reason", "Excluded as inconsistent.")}))
        for row in prepared]
    return {
        "status": status, "reason": reason, "nadir": nadir.tolist(),
        "nadir_std_m": uncertainty, "camera_height_m": median,
        "camera_height_range_m": [float(min(cameras)), float(max(cameras))],
        "camera_spread_fraction": float(max(abs(value / median - 1) for value in cameras)),
        "redundancy": len(active) - 2, "poles": entries,
        "lean_sensitivity_deg": detected_deg,
        "lean_note": ("Catches a pole leaning towards or away from the camera. A pole leaning "
                      "across the view moves almost along the same ground line, so one photo "
                      "cannot rule that out."),
        "retained_poles": len(active), "excluded_poles": len(prepared) - len(active),
        "max_offset_m": settings["max_offset_m"],
    }


def measure_subject(row, calibration, model, settings):
    blank = lambda reason: row | {"status": "held", "reason": reason, "height_m": None,
                                  "height_range_m": None, "vertical_offset_m": None, "caution": None}
    if not row["contact_visible"] or row["contact"] is None:
        return blank("Ground contact is not visible.")
    if not row["shoulder_visible"] or row["shoulder"] is None:
        return blank("Shoulder top is not visible.")
    if calibration["status"] != "pass":
        return blank("The reference poles did not yield a usable camera calibration.")
    if not contains(model["domain"], row["contact"]):
        return blank("Ground contact lies outside the calibrated ground area.")
    try:
        base = to_ground(model, row["contact"])
        top = to_ground(model, row["shoulder"])
    except SurveyError:
        return blank("The shoulder line crosses the ground-plane horizon.")
    nadir = np.asarray(calibration["nadir"])
    distance = top - base
    length = float(np.linalg.norm(distance))
    if length < 1e-6:
        return blank("Shoulder top and ground contact collapse to the same ground position.")
    normal = np.array([-distance[1], distance[0]]) / length
    offset = abs(float(normal @ (nadir - base)))
    base_radius = float(np.linalg.norm(base - nadir))
    top_radius = float(np.linalg.norm(top - nadir))
    if top_radius <= base_radius + 1e-9:
        return blank("Shoulder top is not above the ground contact in the calibrated vertical direction.")
    if offset > settings["max_offset_m"]:
        return blank(f"Shoulder top is {offset:.2f} m off the vertical through the ground contact.")
    ratio = 1.0 - base_radius / top_radius
    height = calibration["camera_height_m"] * ratio
    low, high = calibration["camera_height_range_m"]
    if not PLAUSIBLE_HEIGHT_M[0] <= height <= PLAUSIBLE_HEIGHT_M[1]:
        return blank(f"The estimate of {height:.2f} m is outside the plausible standing-height range.")
    return row | {
        "status": "measured", "reason": None, "height_m": float(height),
        "height_range_m": [float(low * ratio), float(high * ratio)],
        "camera_height_m": calibration["camera_height_m"],
        "vertical_offset_m": offset, "ground_separation_m": length,
        "caution": None if contains(model["domain"], row["shoulder"]) else
        "Shoulder top is above the edge of the calibrated ground area, so the vertical is extrapolated.",
    }


def analyze(model, values):
    calibration = calibrate(model, values["poles"], values)
    subjects = [plain(measure_subject(row, calibration, model, values)) for row in values["subjects"]]
    return {"calibration": calibration, "subjects": subjects,
            "measured_count": sum(row["status"] == "measured" for row in subjects),
            "held_count": sum(row["status"] != "measured" for row in subjects)}


def save_heights(store, form):
    frame_key = identifier(form.get("frame"), "frame")
    model_key = identifier(form.get("model"), "ground plane")
    values = parse_heights(form)
    with store.use(write=True) as db:
        frame = db.one("frames", frame_key)
        model = db.one("models", model_key)
        if model["deployment_id"] != frame["deployment_id"] or model["source_extent"] != frame["extent"]:
            raise SurveyError("Use the same deployment and framing as the ground-plane landmarks.", field="model")
        if db.count("heights", equal={"frame_id": frame_key}) >= MAX_HEIGHTS:
            raise Limit("This frame has reached its standing-height limit.")
        result = analyze(model, values)
        return db.append("heights", result | {
            "name": word(form.get("name"), "height measurement name"),
            "frame_id": frame_key, "model_id": model_key, "frame_version": frame["version"],
            "source_digest": frame["digest"], "model_rms_m": model["rms_m"],
            "model_matrix": model["matrix"], "model_domain": model["domain"],
            "poles": [row | {"base": row["base"], "top": row["top"]} for row in result["calibration"]["poles"]],
            "max_offset_m": values["max_offset_m"], "max_camera_spread": values["max_camera_spread"],
            "note": word(form.get("note"), "survey note", max_length=1500, default="", multiline=True),
        }, action="standing height measured")


def preview_heights(store, form):
    frame_key = identifier(form.get("frame"), "frame")
    model_key = identifier(form.get("model"), "ground plane")
    values = parse_heights(form)
    with store.use() as db:
        frame = db.one("frames", frame_key)
        model = db.one("models", model_key)
    if model["deployment_id"] != frame["deployment_id"] or model["source_extent"] != frame["extent"]:
        raise SurveyError("Use the same deployment and framing as the ground-plane landmarks.", field="model")
    return analyze(model, values)


def list_heights(store, query):
    frame = identifier(query.get("frame"), "frame", optional=True)
    where = (("frame_id", "=", frame),) if frame else ()
    with store.use() as db:
        return {"heights": db.matching("heights", where=where, limit=80)}


def height_guide(store, key):
    with store.use() as db:
        record = db.one("heights", key)
        image = fetch_work(db, record["frame_id"])
    output = show_support(image)
    output.thumbnail((1300, 1300), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(output)
    width, height = output.size
    scale = max(2, width // 520)
    for pole in record["poles"]:
        points = pixel_units([pole["base"], pole["top"]], output.size)
        retained = pole["retained"]
        colour = (98, 219, 232) if retained else (247, 118, 109)
        draw.line(points, fill=colour, width=scale)
        for x, y in points:
            draw.ellipse((x - scale - 2, y - scale - 2, x + scale + 2, y + scale + 2),
                         outline=colour, width=2)
        x, y = points[1]
        text = (f"{pole['name']} · {pole['height_m']:.2f} m" if retained
                else f"{pole['name']} · excluded")
        label(draw, (min(max(4, x + 7), width - 190), max(4, y + 5)), text,
              fill=colour, size=12)
    for subject in record["subjects"]:
        pieces = [subject["contact"]] + ([subject["shoulder"]] if subject["shoulder"] else [])
        points = pixel_units([point for point in pieces if point], output.size)
        if subject["status"] == "measured":
            colour = (133, 239, 158)
            text = f"{subject['name']} · {subject['height_m']:.2f} m"
        else:
            colour = (242, 198, 111)
            text = f"{subject['name']} · held"
        if len(points) == 2:
            draw.line(points, fill=colour, width=scale)
        for x, y in points:
            draw.ellipse((x - scale - 2, y - scale - 2, x + scale + 2, y + scale + 2),
                         outline=colour, width=2)
        if points:
            x, y = points[-1]
            label(draw, (min(max(4, x + 7), width - 210), max(4, y + 5)), text,
                  fill=colour, size=12)
    calibration = record["calibration"]
    if calibration["status"] == "pass":
        low, high = calibration["camera_height_range_m"]
        summary = (f"{calibration['retained_poles']}/{len(record['poles'])} poles · camera "
                   f"{calibration['camera_height_m']:.2f} m ({low:.2f}–{high:.2f}) · "
                   f"{record['measured_count']}/{len(record['subjects'])} animals measured")
        if calibration.get("lean_sensitivity_deg") is not None:
            summary += f" · catches lean >{calibration['lean_sensitivity_deg']:.1f}°"
    else:
        summary = f"Calibration held · {calibration['reason']}"
    label(draw, (7, 7), summary, size=13, fill=(240, 242, 234))
    return output
