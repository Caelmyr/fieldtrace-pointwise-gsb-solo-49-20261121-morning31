"""Read and picture directional marking evidence from a selected wildlife image patch."""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .contracts import SurveyError, number, rectangle, word
from .images import (
    fetch_work, image_bytes, intensities, label, mark_union, normalized_crop, quality_edges, show_support,
)
from .inspection import percentiles


@dataclass(frozen=True)
class PatternSettings:
    box: tuple
    smooth_sigma: float
    minimum_strength: float
    max_shift: int

    @classmethod
    def parse(cls, raw):
        return cls(
            rectangle(raw.get("box")),
            number(raw.get("smooth_sigma"), "smooth_sigma", .35, 8, default=1.2),
            number(raw.get("minimum_strength"), "minimum_strength", .002, .3, default=.018),
            int(number(raw.get("max_shift"), "max_shift", 4, 110, default=28)),
        )

    def json(self):
        return {"box": self.box, "smooth_sigma": self.smooth_sigma,
                "minimum_strength": self.minimum_strength, "max_shift": self.max_shift}


def supported_gaussian(values, active, sigma):
    inside = ndimage.gaussian_filter(active.astype(np.float32), sigma, mode="nearest")
    accumulated = ndimage.gaussian_filter(np.where(active, values, 0), sigma, mode="nearest")
    guide = np.divide(accumulated, inside, out=np.zeros_like(accumulated), where=inside > 0.68)
    return guide, (inside >= .68) & active


def gradient_response(signal, active, radius):
    smoothed, local = supported_gaussian(signal, active, radius)
    core = ndimage.binary_erosion(local)
    gx = ndimage.sobel(smoothed, axis=1) / 8
    gy = ndimage.sobel(smoothed, axis=0) / 8
    dx = np.where(core, gx, 0)
    dy = np.where(core, gy, 0)
    return dx, dy, core


def orientation_params(dx, dy, core, minimum):
    strength = np.hypot(dx, dy)
    reliable = core & (strength >= minimum)
    if int(reliable.sum()) < 16:
        return {"orientation_deg": None, "coherence": None, "active_edges": int(reliable.sum()),
                "orientation_bins": [0.0] * 18, "magnitude": percentiles(strength[core])}, reliable
    r = strength[reliable]
    angles = np.arctan2(dy[reliable], dx[reliable])
    x = float(np.sum(r * np.cos(2 * angles)))
    y = float(np.sum(r * np.sin(2 * angles)))
    normal = float(np.arctan2(y, x) / 2)
    direction = float((normal + np.pi / 2) % np.pi)
    coherence = np.hypot(x, y) / float(r.sum())
    histogram = np.histogram((angles + np.pi / 2) % np.pi, bins=18, range=(0, np.pi), weights=r)[0]
    bins = histogram / max(.000001, float(histogram.sum()))
    return {
        "orientation_deg": float(np.degrees(direction)), "coherence": float(coherence),
        "active_edges": int(reliable.sum()), "orientation_bins": bins.tolist(),
        "magnitude": percentiles(strength[core]),
    }, reliable


def overlap_slices(count, delta):
    if abs(delta) >= count:
        return slice(0, 0), slice(0, 0)
    if delta < 0:
        return slice(-delta, count), slice(0, count + delta)
    return slice(0, count - delta), slice(delta, count)


def correlation_at(image, valid, dx, dy):
    ax, bx = overlap_slices(image.shape[1], dx)
    ay, by = overlap_slices(image.shape[0], dy)
    paired = valid[ay, ax] & valid[by, bx]
    amount = int(paired.sum())
    fraction = amount / max(1, int(valid.sum()))
    if amount < 20 or fraction < 0.18:
        return {"pairs": amount, "fraction": fraction, "correlation": None}
    a = image[ay, ax][paired].astype(np.float64)
    b = image[by, bx][paired].astype(np.float64)
    a -= a.mean()
    b -= b.mean()
    scale = float(np.linalg.norm(a) * np.linalg.norm(b))
    correlation = float(np.dot(a, b) / scale) if scale > 1e-7 else None
    return {"pairs": amount, "fraction": fraction, "correlation": correlation}


def profile_lags(values, support, degrees, max_shift):
    if degrees is None:
        return []
    theta = np.radians(degrees - 90)
    lag_rows = []
    locations = set()
    for shift in range(1, max_shift + 1):
        dx = int(round(shift * np.cos(theta)))
        dy = int(round(shift * np.sin(theta)))
        if (dx, dy) in locations:
            continue
        locations.add((dx, dy))
        lag_rows.append(correlation_at(values, support, dx, dy) | {
            "lag_px": float(np.hypot(dx, dy)), "shift_px": [dx, dy],
        })
    return lag_rows


def strongest_cycles(lags, max_items=4):
    plausible = []
    for prev, now, following in zip(lags, lags[1:], lags[2:]):
        score = now["correlation"]
        other = (prev["correlation"], following["correlation"])
        if score is None or any(x is None for x in other):
            continue
        if score >= 0.15 and score > max(other):
            plausible.append(now)
    return sorted(plausible, key=lambda row: row["correlation"], reverse=True)[:max_items]


def pattern_reading(photo, excluded, settings):
    brightness, active = intensities(photo)
    use = active & ~excluded & (brightness > .025) & (brightness < .985)
    if int(use.sum()) < 48:
        raise SurveyError("Use a marking region with at least 48 illuminated visible pixels.", field="box")
    dx, dy, core = gradient_response(brightness, use, settings.smooth_sigma)
    oriented, reliable = orientation_params(dx, dy, core, settings.minimum_strength)
    low, valid = supported_gaussian(brightness, use, max(2 * settings.smooth_sigma, 3.0))
    details = np.where(valid, brightness - low, 0)
    direction = oriented["orientation_deg"] if (oriented.get("coherence") or 0) >= .2 else None
    lags = profile_lags(details, valid, direction, settings.max_shift)
    candidates = strongest_cycles(lags)
    reading = oriented | {
        "active_pixels": int(use.sum()), "excluded_pixels": int(excluded.sum()),
        "support_fraction": float(use.mean()), "spacing_lags": lags, "repeat_candidates": candidates,
        "dimensions": photo.size,
        "state": "single direction evidence" if direction is not None else "insufficient or mixed direction",
        "note": "Marking orientation and correlation are local photo evidence, without an individual match decision.",
    }
    return reading, reliable


def texture_overlay(photo, selected):
    image = np.asarray(show_support(photo), dtype=np.float32)
    light = np.array([101, 228, 154], dtype=np.float32)
    image[selected] = image[selected] * .64 + light * .36
    return Image.fromarray(np.rint(image).astype(np.uint8))


def direction_plot(data, graphic, point, radius):
    draw = graphic
    hue = (115, 222, 173)
    draw.ellipse((point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius),
                 outline=(102, 143, 132), width=1)
    max_ = max(data or [0])
    if not max_:
        return
    for i, fraction in enumerate(data):
        length = radius * fraction / max_
        angle = np.pi * (i + .5) / len(data)
        vector = np.array([np.cos(angle), np.sin(angle)]) * length
        left, right = tuple(np.asarray(point) - vector), tuple(np.asarray(point) + vector)
        draw.line((*left, *right), fill=hue, width=3)


def lag_plot(lags, draw, bounds):
    left, top, right, bottom = bounds
    zero = (top + bottom) / 2
    draw.line((left, zero, right, zero), fill=(99, 141, 133), width=1)
    values = [row for row in lags if row["correlation"] is not None]
    if not values:
        return
    span = max(1, lags[-1]["lag_px"])
    last = None
    for row in lags:
        score = row["correlation"]
        if score is None:
            last = None
            continue
        x = left + (right - left) * row["lag_px"] / span
        y = zero - score * (bottom - top) / 2
        if last:
            draw.line((*last, x, y), fill=(224, 181, 107), width=3)
        last = (x, y)


def mark_board(photo, stats, selected):
    width, height = 870, 535
    board = Image.new("RGB", (width, height), (23, 34, 35))
    pen = ImageDraw.Draw(board)
    label(pen, (24, 17), "Marking direction and repeat evidence", size=22)
    left = texture_overlay(photo, selected)
    left.thumbnail((475, 370), Image.Resampling.LANCZOS)
    board.paste(left, (25 + (475 - left.width) // 2, 78 + (370 - left.height) // 2))
    label(pen, (538, 80), "Direction in the image", size=14)
    direction_plot(stats["orientation_bins"], pen, (663, 208), 95)
    label(pen, (538, 322), "Correlation over normal displacement", size=13)
    lag_plot(stats["spacing_lags"], pen, (540, 355, 826, 439))
    supported = f"{stats['support_fraction']:.0%} measured support · {stats['state']}"
    label(pen, (27, 477), supported, size=15)
    cycles = stats["repeat_candidates"]
    text = ", ".join(f"{sample['lag_px']:.1f} px" for sample in cycles) if cycles else "No clear lag candidates"
    label(pen, (27, 501), text, size=11)
    return board


def save_pattern(store, key, raw):
    settings = PatternSettings.parse(raw)
    with store.use() as db:
        frame = db.one("frames", key)
        image = fetch_work(db, key)
        marks = db.matching("marks", where=(("frame_id", "=", key),))
    bounds = normalized_crop(settings.box, image.size)
    if min(bounds[2] - bounds[0], bounds[3] - bounds[1]) < 12:
        raise SurveyError("Use a patch at least 12 pixels wide and high.", field="box")
    photo = image.crop(bounds)
    x0, y0, x1, y1 = bounds
    omitted = mark_union(image.size, marks, {"ignore", "occlusion"})[y0:y1, x0:x1]
    report, response = pattern_reading(photo, omitted, settings)
    bitmap = mark_board(photo, report, response)
    record = {
        "name": word(raw.get("name"), "marking report name", default="Marking texture"),
        "frame_id": key, "kind": "marking-pattern", "settings": settings.json(),
        "source_digest": frame["digest"], "frame_version": frame["version"], "report": report,
        "bounds": bounds, "mask_versions": {row["id"]:row["version"] for row in marks},
    }
    with store.use(write=True) as db:
        found = db.append("glances", record, action="marking pattern measured")
        db.add_asset(found["id"], "guide", image_bytes(bitmap), "image/png")
    return found


def pattern_probe(store, key, raw):
    settings = PatternSettings.parse(raw)
    with store.use() as db:
        photo = fetch_work(db, key)
        masks = db.matching("marks", where=(("frame_id", "=", key),))
    crop = normalized_crop(settings.box, photo.size)
    x0, y0, x1, y1 = crop
    if min(x1 - x0, y1 - y0) < 12:
        raise SurveyError("Use a patch with enough signal for the orientation probe.")
    excluded = mark_union(photo.size, masks, {"occlusion", "ignore"})
    result, _ = pattern_reading(photo.crop(crop), excluded[y0:y1, x0:x1], settings)
    return result | {"bounds": crop}
