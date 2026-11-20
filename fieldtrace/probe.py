"""Spatially locate original image evidence with explicit neighborhood support."""

import numpy as np
from PIL import ImageDraw

from .contracts import SurveyError, integer, unit_point
from .geometry import contains
from .images import fetch_work, image_bytes, label, mark_union, show_support
from .inspection import percentiles


def probe_window(extent, centre, radius):
    x, y = centre
    left, right = max(0, x - radius), min(extent[0], x + radius + 1)
    top, bottom = max(0, y - radius), min(extent[1], y + radius + 1)
    metric_x, metric_y = np.meshgrid(np.arange(left, right) - x, np.arange(top, bottom) - y)
    return (left, top, right, bottom), (metric_x ** 2 + metric_y ** 2 <= radius ** 2)


def source_position(frame, point):
    source_w, source_h = frame["source_extent"]
    work_w, work_h = frame["extent"]
    source = [round(float(point[0]) * (source_w - 1), 4), round(float(point[1]) * (source_h - 1), 4)]
    work = [int(round(float(point[0]) * (work_w - 1))), int(round(float(point[1]) * (work_h - 1)))]
    return {"normalized": point, "working_px": work, "oriented_source_px": source,
            "source_orientation": frame["camera"].get("orientation", 1)}


def channel_summary(samples):
    if samples.size == 0:
        return {"medians": None, "minima": None, "maxima": None, "luminance_quantiles": percentiles([])}
    channel_coeffs = np.array([.2126, .7152, .0722])
    luma = (samples.astype(np.float32) @ channel_coeffs) / 255
    return {
        "medians": np.median(samples, axis=0).tolist(), "minima": np.min(samples, axis=0).tolist(),
        "maxima": np.max(samples, axis=0).tolist(), "luminance_quantiles": percentiles(luma),
    }


def calculate_probe(frame, image, marks, point, radius):
    mapped = source_position(frame, point)
    x, y = mapped["working_px"]
    bounds, circle = probe_window(image.size, (x, y), radius)
    left, top, right, bottom = bounds
    pixels = np.asarray(image.crop(bounds).convert("RGBA"))
    opacity = pixels[..., 3] >= 128
    unavailable = mark_union(image.size, marks, {"ignore", "occlusion"})[top:bottom, left:right]
    selected = opacity & circle & ~unavailable
    inside_mark = [{"id": mark["id"], "name": mark["name"], "kind": mark["kind"], "version": mark["version"]}
                   for mark in marks if contains(mark["polygon"], point)]
    count = int(selected.sum())
    total = int(circle.sum())
    centre = pixels[y - top, x - left, :3].tolist() if selected[y - top, x - left] else None
    return {
        "coordinate": mapped, "radius_px": radius, "count": count, "requested": total,
        "missing": int((circle & ~opacity).sum()), "excluded": int((circle & unavailable & opacity).sum()),
        "support_fraction": count / max(1, total), "bounds": bounds,
        "at_point_rgb": centre, "summary": channel_summary(pixels[..., :3][selected]),
        "marks": inside_mark, "frame": frame["id"], "source_digest": frame["digest"],
        "note": "Readings use the oriented 8-bit working display of the camera photo.",
    }


def probe_image(image, result, *, context_px):
    point = result["coordinate"]["working_px"]
    x, y = point
    left, top = max(0, x - context_px), max(0, y - context_px)
    right, bottom = min(image.width, x + context_px + 1), min(image.height, y + context_px + 1)
    view = show_support(image.crop((left, top, right, bottom)))
    ratio = min(6, 380 / max(view.size))
    canvas = view.resize((max(1, round(view.width * ratio)), max(1, round(view.height * ratio))))
    scale_x, scale_y = canvas.width / view.width, canvas.height / view.height
    p, q = (x - left + 0.5) * scale_x, (y - top + 0.5) * scale_y
    draw = ImageDraw.Draw(canvas)
    rng = max(2, result["radius_px"] * min(scale_x, scale_y))
    shade = (135, 242, 177) if result["at_point_rgb"] is not None else (247, 165, 128)
    draw.ellipse((p - rng, q - rng, p + rng, q + rng), outline=shade, width=2)
    draw.line((p - 10, q, p + 10, q), fill=shade, width=1)
    draw.line((p, q - 10, p, q + 10), fill=shade, width=1)
    label(draw, (7, 7), f"{x},{y} · {result['count']}/{result['requested']}", size=10)
    return canvas


def pixel_probe(store, key, raw, *, preview=False):
    with store.use() as db:
        frame = db.one("frames", key)
        image = fetch_work(db, key)
        marks = db.matching("marks", where=(("frame_id", "=", key),))
    computed = classification_at_cursor(frame, image, marks, raw)
    if not preview:
        return computed
    context = integer(raw.get("context"), "context", 8, 180, default=52)
    return probe_image(image, computed, context_px=context)


def classification_at_cursor(frame, image, marks, raw):
    point = unit_point(raw.get("point"))
    radius = integer(raw.get("radius"), "radius", 0, 45, default=4)
    data = calculate_probe(frame, image, marks, point, radius)
    reading = data["at_point_rgb"]
    if reading is None:
        state = "no trustworthy centre reading"
    elif max(reading) - min(reading) >= 35:
        state = "noticeable channel separation"
    elif sum(reading) / len(reading) < 30:
        state = "very dark"
    elif max(reading) > 247:
        state = "near clipping"
    else:
        state = "ordinary display range"
    if frame["illumination"] == "night-ir":
        state += " · infrared capture"
    return data | {"display_state": state}
