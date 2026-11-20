"""Spatial labels, checked planar camera calibration and path geometry."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import ImageDraw

from .catalog import revision
from .config import CERTAINTIES, MARK_KINDS, MAX_MARKS_PER_FRAME, MAX_MEASURES, MAX_PLANAR_PAIRS, TAXA
from .contracts import (
    Conflict, Limit, SurveyError, identifier, list_value, need_object, number,
    option, parse_float_pair, polygon, unit_point, word,
)
from .images import fetch_work, label, marked_preview, pixel_units, show_support

COLLINEAR = 1e-9


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def between(a, p, b):
    return abs(cross(a, b, p)) < COLLINEAR and all(
        min(a[i], b[i]) - COLLINEAR <= p[i] <= max(a[i], b[i]) + COLLINEAR for i in range(2)
    )


def intersect(a, b, c, d):
    if any(between(x, y, z) for x, y, z in ((a, c, b), (a, d, b), (c, a, d), (c, b, d))):
        return True
    return (cross(a, b, c) > 0) != (cross(a, b, d) > 0) and (cross(c, d, a) > 0) != (cross(c, d, b) > 0)


def simple_polygon(points):
    count = len(points)
    edges = list(zip(points, points[1:] + points[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            c, d = edges[j]
            if intersect(a, b, c, d):
                raise SurveyError("The outline crosses itself.", field="polygon")
    return points


def hull(points):
    pts = sorted(set(map(tuple, points)))
    if len(pts) < 3:
        return []
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def contains(points, position):
    x, y = position
    inside = False
    for a, b in zip(points, points[1:] + points[:1]):
        if between(a, position, b):
            return True
        if (a[1] > y) != (b[1] > y):
            edge_x = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < edge_x:
                inside = not inside
    return inside


def signed_area(points):
    return 0.5 * sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(points, points[1:] + points[:1]))


def mark_fields(form):
    kind = option(form.get("kind"), "kind", MARK_KINDS, default="animal")
    return {
        "name": word(form.get("name"), "mark name", max_length=65),
        "kind": kind,
        "polygon": simple_polygon(polygon(form.get("polygon"))),
        "taxon": option(form.get("taxon"), "taxon", TAXA, default="unknown") if kind == "animal" else None,
        "certainty": option(form.get("certainty"), "certainty", CERTAINTIES, default="observed"),
        "note": word(form.get("note"), "notes", max_length=1000, default="", multiline=True),
    }


def save_mark(store, frame, form, key=None):
    fields = mark_fields(form)
    with store.use(write=True) as db:
        db.one("frames", frame)
        if key is None:
            if db.count("marks", equal={"frame_id": frame}) >= MAX_MARKS_PER_FRAME:
                raise Limit("This frame has reached its annotation limit.")
            return db.append("marks", fields | {"frame_id": frame}, action="source polygon drawn")
        old = db.one("marks", key)
        if old["frame_id"] != frame:
            raise SurveyError("This mark belongs to another frame.", status=404)
        return db.replace("marks", key, revision(form), lambda original: original | fields, action="source polygon updated")


def remove_mark(store, frame, key, form):
    with store.use(write=True) as db:
        if db.one("marks", key)["frame_id"] != frame:
            raise SurveyError("This mark belongs to another frame.", status=404)
        db.remove("marks", key, revision(form))
    return {"removed": key}


def marks_preview(store, key):
    with store.use() as db:
        db.one("frames", key)
        image = fetch_work(db, key)
        marks = db.matching("marks", where=(("frame_id", "=", key),), order=("created_at", "ASC"))
    return marked_preview(image, marks, edge=1350)


def normalized_system(points):
    matrix = np.asarray(points, dtype=float)
    center = matrix.mean(axis=0)
    radius = float(np.sqrt(np.mean(np.sum((matrix - center) ** 2, axis=1))))
    if radius <= 1e-10:
        raise SurveyError("The calibration coordinates have no usable spread.", field="anchors")
    scale = np.sqrt(2) / radius
    affine = np.array([[scale, 0, -scale * center[0]], [0, scale, -scale * center[1]], [0, 0, 1]])
    return (matrix - center) * scale, affine


def transform_points(matrix, points):
    xy = np.asarray(points, dtype=float).reshape(-1, 2)
    coordinates = (matrix @ np.column_stack((xy, np.ones(len(xy)))).T).T
    if np.any(np.abs(coordinates[:, 2]) <= 1e-9):
        raise SurveyError("The selected points cross the plane horizon.", field="points")
    return coordinates[:, :2] / coordinates[:, 2, None]


def projective_fit(source, target):
    src, first = normalized_system(source)
    dst, second = normalized_system(target)
    design = []
    for (x, y), (xw, yw) in zip(src, dst):
        design.extend(([-x, -y, -1, 0, 0, 0, xw * x, xw * y, xw],
                       [0, 0, 0, -x, -y, -1, yw * x, yw * y, yw]))
    _, spectrum, right = np.linalg.svd(np.asarray(design))
    rank_indicator = spectrum[-1] if len(spectrum) == 8 else spectrum[-2]
    if rank_indicator < 0.025:
        raise SurveyError("Landmarks are too close to a single line for a dependable plane.", field="anchors")
    m = np.linalg.inv(second) @ right[-1].reshape(3, 3) @ first
    if abs(np.linalg.det(m)) < 1e-11:
        raise SurveyError("The landmark transform collapsed.", field="anchors")
    return m / np.linalg.norm(m)


def parse_anchors(raw):
    records = list_value(raw, "anchors", min_items=4, max_items=MAX_PLANAR_PAIRS)
    names = set()
    anchors = []
    for i, record in enumerate(records, 1):
        source = need_object(record, "anchor")
        name = word(source.get("name") or f"Point {i}", "landmark", max_length=40)
        if name in names:
            raise SurveyError("Give each landmark a distinct label.", field="anchors")
        names.add(name)
        position = unit_point(source.get("image"), "image")
        reference = parse_float_pair(source.get("ground_m"), "ground_m", (-500, -500), (500, 500))
        anchors.append({"name": name, "image": position, "ground_m": reference})
    if len(set(tuple(row["image"]) for row in anchors)) != len(anchors):
        raise SurveyError("Each image landmark must have a distinct coordinate.", field="anchors")
    return anchors


def fit_model(anchors, maximum_error):
    src, dst = zip(*[(row["image"], row["ground_m"]) for row in anchors])
    support = hull(src)
    if len(support) < 3 or abs(signed_area(support)) < 0.004:
        raise SurveyError("Landmarks must enclose a useful area of the image.", field="anchors")
    matrix = projective_fit(src, dst)
    predicted = transform_points(matrix, src)
    errors = np.linalg.norm(predicted - np.asarray(dst), axis=1)
    return {
        "matrix": matrix.tolist(), "domain": support, "rms_m": float(np.sqrt(np.mean(errors ** 2))),
        "fit_pass": bool(np.max(errors) <= maximum_error), "max_error_m": float(np.max(errors)),
        "landmarks": [row | {"residual_m": float(error)}
                      for row, error in zip(anchors, errors)],
    }


def save_model(store, form):
    frame_key = identifier(form.get("frame"), "frame")
    maximum = number(form.get("max_error_m"), "max_error_m", 0.005, 3.0, default=0.1)
    anchors = parse_anchors(form.get("anchors"))
    solved = fit_model(anchors, maximum)
    name = word(form.get("name"), "ground plane name")
    with store.use(write=True) as db:
        frame = db.one("frames", frame_key)
        return db.append("models", solved | {
            "name": name, "deployment_id": frame["deployment_id"], "frame_id": frame_key,
            "source_digest": frame["digest"], "source_extent": frame["extent"], "maximum_error_m": maximum,
            "note": word(form.get("note"), "survey note", max_length=1200, default="", multiline=True),
        }, action="ground plane fitted")


def guide_image(store, model_key):
    with store.use() as db:
        model = db.one("models", model_key)
        frame = fetch_work(db, model["frame_id"])
    output = show_support(frame)
    output.thumbnail((1200, 1200))
    canvas = ImageDraw.Draw(output)
    domain = pixel_units(model["domain"], output.size)
    canvas.line(domain + [domain[0]], fill=(113, 219, 171), width=3)
    for sample in model["landmarks"]:
        x, y = pixel_units([sample["image"]], output.size)[0]
        canvas.ellipse((x - 6, y - 6, x + 6, y + 6), outline=(248, 223, 151), width=2)
        label(canvas, (min(x + 8, output.width - 160), min(y + 5, output.height - 25)), sample["name"], size=13)
    return output


def plane_measurement(model, points):
    if not model["fit_pass"]:
        raise Conflict("This ground plane did not pass its landmark error limit.")
    matrix = np.asarray(model["matrix"])
    metric = transform_points(matrix, points)
    result = []
    for index, (a, b) in enumerate(zip(points, points[1:])):
        supported = contains(model["domain"], a) and contains(model["domain"], b)
        ea, eb = metric[index:index + 2]
        result.append({
            "from": a, "to": b, "supported": supported,
            "ground_start": ea.tolist() if supported else None,
            "ground_end": eb.tolist() if supported else None,
            "metres": float(np.linalg.norm(eb - ea)) if supported else None,
        })
    values = [row["metres"] for row in result if row["supported"]]
    return {
        "segments": result, "inside_plane": len(values) == len(result),
        "total_supported_m": sum(values), "support_segments": len(values),
        "segments_requested": len(result),
    }


def save_measure(store, form):
    model_id = identifier(form.get("model"), "model")
    target = identifier(form.get("frame"), "frame")
    raw_points = list_value(form.get("points"), "points", min_items=2, max_items=32)
    points = [unit_point(item) for item in raw_points]
    if any(math.dist(a, b) < 0.0005 for a, b in zip(points, points[1:])):
        raise SurveyError("Adjacent points of a measured path must be distinct.", field="points")
    with store.use(write=True) as db:
        model = db.one("models", model_id)
        frame = db.one("frames", target)
        if model["deployment_id"] != frame["deployment_id"] or model["source_extent"] != frame["extent"]:
            raise SurveyError("Use the same deployment and framing as the ground-plane landmarks.", field="model")
        if db.count("measures", equal={"frame_id": target}) >= MAX_MEASURES:
            raise Limit("This frame has reached its measurement limit.")
        result = plane_measurement(model, points)
        return db.append("measures", result | {
            "name": word(form.get("name"), "measurement name"),
            "frame_id": target, "model_id": model_id, "frame_version": frame["version"],
            "points": points, "fit_error_m": model["rms_m"], "source_digest": frame["digest"],
        }, action="ground plane distance measured")
