"""Review photo support and static-camera change without altering original pixels."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .config import MAX_WITNESSES
from .contracts import (
    Limit, SurveyError, flag, identifier, integer, need_object, number, rectangle, word,
)
from .images import (
    background, display_tone, fetch_work, font, image_bytes, intensities, label, mark_union,
    normalized_crop, quality_edges, readonly_array, show_support, support_geometry,
)

AUDIT_PALETTE = [
    (46, 54, 60), (136, 109, 172), (81, 136, 215), (238, 202, 111),
    (198, 102, 52), (37, 170, 129),
]
AUDIT_LABELS = ["not visible", "excluded", "deep shadow", "clipped highlight", "weak detail", "usable detail"]
MOTION_PALETTE = [(51, 58, 62), (195, 115, 180), (69, 85, 91), (242, 164, 75), (84, 208, 176)]


@dataclass(frozen=True)
class QualitySettings:
    shadows: float
    highlights: float
    minimum_detail: float
    rows: int
    columns: int
    box: tuple | None

    @classmethod
    def parse(cls, raw):
        shadow = number(raw.get("shadows"), "shadows", 0.005, 0.35, default=0.08)
        light = number(raw.get("highlights"), "highlights", 0.65, 1, default=0.975)
        return cls(
            shadow, light,
            number(raw.get("minimum_detail"), "minimum_detail", 0.001, 0.3, default=0.018),
            integer(raw.get("rows"), "rows", 2, 16, default=5),
            integer(raw.get("columns"), "columns", 2, 20, default=7),
            rectangle(raw["box"]) if raw.get("box") else None,
        )

    def json(self):
        return {
            "shadows": self.shadows, "highlights": self.highlights,
            "minimum_detail": self.minimum_detail, "rows": self.rows, "columns": self.columns,
            "box": self.box,
        }


@dataclass(frozen=True)
class MotionSettings:
    threshold: float
    min_area: int
    max_area_fraction: float
    radius: int
    max_contrast_shift: float
    permit_ir: bool

    @classmethod
    def parse(cls, raw):
        return cls(
            number(raw.get("threshold"), "threshold", 0.01, 0.4, default=0.085),
            integer(raw.get("min_area"), "min_area", 8, 4000, default=60),
            number(raw.get("max_area_fraction"), "max_area_fraction", 0.03, 0.9, default=0.35),
            integer(raw.get("radius"), "radius", 0, 6, default=1),
            number(raw.get("max_contrast_shift"), "max_contrast_shift", 0.015, 0.5, default=0.18),
            flag(raw.get("permit_ir"), "permit_ir", default=True),
        )

    def json(self):
        return {"threshold": self.threshold, "min_area": self.min_area, "radius": self.radius,
                "max_area_fraction": self.max_area_fraction, "max_contrast_shift": self.max_contrast_shift,
                "permit_ir": self.permit_ir}


def as_window(extent, box):
    if box is None:
        return (0, 0, *extent)
    return normalized_crop(box, extent)


def region_active(extent, window):
    support = np.zeros(extent[::-1], dtype=bool)
    x0, y0, x1, y1 = window
    support[y0:y1, x0:x1] = True
    return support


def percentiles(sample):
    keys = ("p05", "p25", "median", "p75", "p95")
    if not len(sample):
        return dict.fromkeys(keys)
    return {key: float(value) for key, value in zip(keys, np.quantile(sample, (0.05, 0.25, 0.5, 0.75, 0.95)))}


def quality_cell(index, bounds, classes, detail, luma):
    x0, y0, x1, y1 = bounds
    q = classes[y0:y1, x0:x1]
    counts = np.bincount(q.ravel(), minlength=len(AUDIT_PALETTE))
    included = q >= 2
    responses = detail[y0:y1, x0:x1][included]
    values = luma[y0:y1, x0:x1][included]
    return {
        "index": index, "box": [x0, y0, x1 - x0, y1 - y0],
        "support": int(included.sum()), "detail": percentiles(responses),
        "luminance": percentiles(values),
        "categories": {name: int(counts[i]) for i, name in enumerate(AUDIT_LABELS)},
    }


def grid_windows(box, rows, columns):
    x0, y0, x1, y1 = box
    xs = np.unique(np.rint(np.linspace(x0, x1, columns + 1)).astype(int))
    ys = np.unique(np.rint(np.linspace(y0, y1, rows + 1)).astype(int))
    return [(int(a), int(c), int(b), int(d)) for c, d in zip(ys, ys[1:]) for a, b in zip(xs, xs[1:])]


def image_quality(image, excluded, settings):
    luma, valid = intensities(image)
    window = as_window(image.size, settings.box)
    domain = region_active(image.size, window)
    valid &= domain
    usable = valid & ~excluded
    if np.count_nonzero(usable) < 36:
        raise SurveyError("Choose an area with at least 36 visible, non-excluded pixels.", field="box")
    edges, edge_support = quality_edges(luma, usable)
    local = ndimage.gaussian_filter(np.where(edge_support, edges ** 2, 0), 2.0)
    support = ndimage.gaussian_filter(edge_support.astype(np.float32), 2.0)
    detail = np.sqrt(np.divide(local, support, out=np.zeros_like(local), where=support > 0.55))
    dark = usable & (luma <= settings.shadows)
    bright = usable & (luma >= settings.highlights)
    ordinary = usable & ~(dark | bright)
    classes = np.zeros(luma.shape, dtype=np.uint8)
    classes[valid & excluded] = 1
    classes[dark] = 2
    classes[bright] = 3
    classes[ordinary] = 4
    classes[ordinary & (detail >= settings.minimum_detail)] = 5
    selected = classes[domain]
    totals = np.bincount(selected, minlength=len(AUDIT_LABELS))
    summary = {
        "window": list(window), "support": support_geometry(usable),
        "requested_pixels": int(domain.sum()),
        "categories": {title: int(totals[i]) for i, title in enumerate(AUDIT_LABELS)},
        "usable_detail_rate": float(totals[5] / totals[2:].sum()),
        "luminance": percentiles(luma[usable]),
        "detail": percentiles(detail[ordinary & edge_support]),
        "cells": [quality_cell(i, box, classes, detail, luma)
                  for i, box in enumerate(grid_windows(window, settings.rows, settings.columns), 1)],
    }
    return summary, classes, domain


def class_image(photo, tags, palette, *, support=None, opacity=0.48):
    base = np.asarray(show_support(photo), dtype=np.float32)
    overlay = np.asarray(palette, dtype=np.float32)[tags]
    coverage = np.ones(tags.shape, dtype=np.float32) if support is None else support.astype(np.float32)
    mix = (coverage * opacity)[..., None]
    rgb = np.rint(base * (1 - mix) + overlay * mix).astype(np.uint8)
    return Image.fromarray(rgb)


def prepend_key_line(output, entries, *, title):
    draw = ImageDraw.Draw(output)
    title_size = max(12, output.width // 78)
    label(draw, (13, 13), title, size=title_size)
    font_size = max(9, output.width // 132)
    typography = font(font_size)
    available = output.width - 22

    def measure(text):
        return draw.textbbox((0, 0), text, font=typography)[2] + 30

    candidates = [(str(text), shade) for text, shade in entries]
    # Shorten labels only when the full key would not fit the image width.
    while sum(measure(text) for text, _ in candidates) > available:
        longest = max(range(len(candidates)), key=lambda i: len(candidates[i][0]))
        text, shade = candidates[longest]
        if len(text) <= 4:
            break
        candidates[longest] = (text[:max(3, len(text) - 4)].rstrip() + "…", shade)
    row = output.height - 24
    x = 11
    for text, shade in candidates:
        draw.rectangle((x, row, x + 9, row + 9), fill=shade)
        label(draw, (x + 14, row - 4), text, size=font_size)
        x += measure(text)
    return output


def quality_map(photo, classes, domain):
    output = class_image(photo, classes, AUDIT_PALETTE, support=domain)
    return prepend_key_line(output, list(zip(AUDIT_LABELS, AUDIT_PALETTE)),
                            title="Image quality · diagnostic view")


def frame_marks(db, keys):
    return [db.matching("marks", where=(("frame_id", "=", key),)) for key in keys]


def save_audit(store, key, form):
    settings = QualitySettings.parse(form)
    with store.use() as db:
        frame = db.one("frames", key)
        image = fetch_work(db, key)
        marks, = frame_marks(db, [key])
    excluded = mark_union(image.size, marks, {"ignore", "occlusion"})
    report, tags, domain = image_quality(image, excluded, settings)
    graphic = quality_map(image, tags, domain)
    with store.use(write=True) as db:
        latest = db.one("frames", key)
        if latest["digest"] != frame["digest"]:
            raise SurveyError("The image changed during inspection.", status=409)
        report = db.append("glances", {
            "name": word(form.get("name"), "audit title", default="Image quality map"),
            "frame_id": key, "kind": "quality", "settings": settings.json(),
            "report": report, "source_digest": frame["digest"],
            "mask_versions": {mark["id"]: mark["version"] for mark in marks},
        }, action="visibility audit saved")
        db.add_asset(report["id"], "quality", image_bytes(graphic), "image/png")
    return report


def robust_brightness_shift(earlier, later, support):
    if int(support.sum()) < 64:
        return None, None
    raw = (later[support] - earlier[support]).astype(np.float32)
    center = float(np.median(raw))
    return center, 1.4826 * float(np.median(np.abs(raw - center)))


def masked_blur(values, support, radius):
    if radius == 0:
        return values, support
    weight = ndimage.gaussian_filter(support.astype(np.float32), radius, mode="nearest")
    filt = ndimage.gaussian_filter(np.where(support, values, 0), radius, mode="nearest")
    smooth = np.divide(filt, weight, out=np.zeros_like(filt), where=weight > 0.62)
    return smooth, support & (weight >= 0.62)


def stats_for_component(index, slices, indices, response, sign, extent):
    rows, cols = slices
    here = indices[rows, cols] == index
    sz = int(here.sum())
    if sz == 0:
        return None
    measure = response[rows, cols][here]
    pos_y, pos_x = np.nonzero(here)
    y0, y1, x0, x1 = rows.start, rows.stop, cols.start, cols.stop
    width, height = extent
    return {
        "area_px": sz, "box": [x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height],
        "centroid": [(x0 + float(pos_x.mean())) / width, (y0 + float(pos_y.mean())) / height],
        "median_change": float(np.median(measure)),
        "maximum_change": float(np.max(measure)),
        "direction": "brighter" if float(np.median(sign[rows, cols][here])) >= 0 else "darker",
    }


def candidate_components(binary, response, residual, settings):
    connected, _ = ndimage.label(binary, structure=np.ones((3, 3), dtype=bool))
    area_by_index = np.bincount(connected.ravel())
    available = [int(i) for i in np.flatnonzero(area_by_index >= settings.min_area) if i]
    available.sort(key=lambda i: (-int(area_by_index[i]), i))
    kept = available[:MAX_WITNESSES]
    extent = binary.shape[::-1]
    objects = ndimage.find_objects(connected)
    proposals = []
    for i in kept:
        found = stats_for_component(i, objects[i - 1], connected, response, residual, extent)
        proposals.append(found)
    return proposals, np.isin(connected, kept), len(available) > len(kept)


def scenes_delta(first, second, exclusions, settings):
    a, support_a = intensities(first)
    b, support_b = intensities(second)
    jointly_visible = support_a & support_b & ~exclusions
    clipping = (a >= 0.988) | (b >= 0.988) | (a <= 0.016) | (b <= 0.016)
    support = jointly_visible & ~clipping
    smooth_a, valid_a = masked_blur(a, support, settings.radius)
    smooth_b, valid_b = masked_blur(b, support, settings.radius)
    common = valid_a & valid_b
    offset, mad = robust_brightness_shift(smooth_a, smooth_b, common)
    if offset is None:
        raise SurveyError("The two frames share too little measured camera area.", field="frames")
    residual = (smooth_b - smooth_a) - offset
    response = np.abs(residual)
    changed = common & (response >= settings.threshold)
    area_fraction = int(changed.sum()) / int(common.sum())
    background_shift = abs(offset) > settings.max_contrast_shift or area_fraction > settings.max_area_fraction
    if background_shift:
        candidates, selected, truncated = [], np.zeros(changed.shape, dtype=bool), False
    else:
        candidates, selected, truncated = candidate_components(changed, response, residual, settings)
    classes = np.zeros(changed.shape, dtype=np.uint8)
    classes[jointly_visible & ~common] = 1
    classes[common] = 2
    classes[changed] = 3
    classes[selected] = 4
    return {
        "comparison_state": "lighting change" if background_shift else "locally comparable",
        "offset": offset, "robust_spread": mad, "support": support_geometry(common),
        "changed_fraction": area_fraction, "threshold": settings.threshold,
        "excluded_pixels": int(exclusions.sum()),
        "unmeasured_pixels": int((jointly_visible & ~common).sum()),
        "review_candidates": candidates, "truncated": truncated,
    }, classes


def motion_map(second, tags, report):
    output = class_image(second, tags, MOTION_PALETTE)
    draw = ImageDraw.Draw(output)
    for i, c in enumerate(report["review_candidates"], 1):
        x0, y0, x1, y1 = normalized_crop(c["box"], output.size)
        draw.rectangle((x0, y0, x1, y1), outline=(158, 244, 179), width=max(2, output.width // 390))
        label(draw, (x0 + 4, y0 + 4), str(i), fill=(180, 252, 192))
    return prepend_key_line(output, [("Unmeasured", MOTION_PALETTE[1]), ("Changed", MOTION_PALETTE[3]),
                                    ("For review", MOTION_PALETTE[4])],
                            title="Change candidates · " + report["comparison_state"])


def pair_inputs(db, frame, reference, settings):
    first, last = db.documents("frames", [reference, frame])
    if frame == reference:
        raise SurveyError("Choose two different photos.", field="reference")
    if first["deployment_id"] != last["deployment_id"] or first["extent"] != last["extent"]:
        raise SurveyError("Choose a same-deployment pair with matching framing.", field="reference")
    if first["illumination"] != last["illumination"] or not settings.permit_ir and last["illumination"] == "night-ir":
        raise SurveyError("Use comparable illumination for an ordinary difference view.", field="reference")
    return first, last


def save_motion(store, frame, form):
    reference = identifier(form.get("reference"), "reference")
    settings = MotionSettings.parse(form)
    with store.use() as db:
        first, last = pair_inputs(db, frame, reference, settings)
        images = [fetch_work(db, key) for key in (reference, frame)]
        marks = sum(frame_marks(db, (reference, frame)), [])
    exclusions = mark_union(images[0].size, marks, {"ignore", "occlusion"})
    report, classes = scenes_delta(*images, exclusions, settings)
    view = motion_map(images[-1], classes, report)
    with store.use(write=True) as db:
        saved = db.append("glances", {
            "name": word(form.get("name"), "comparison title", default="Static camera difference"),
            "frame_id": frame, "kind": "motion", "reference_id": reference,
            "report": report, "settings": settings.json(),
            "sources": [{"id": item["id"], "digest": item["digest"]} for item in (first, last)],
            "mask_versions": {mark["id"]: mark["version"] for mark in marks},
        }, action="change candidates saved")
        db.add_asset(saved["id"], "motion", image_bytes(view), "image/png")
    return saved


def zoom_image(store, frame, query):
    with store.use() as db:
        photo = fetch_work(db, frame)
        marks, = frame_marks(db, [frame])
    pattern = query.get("mode", "plain")
    options = {"plain", "shadow", "window", "edge"}
    if pattern not in options:
        raise SurveyError("Choose a supported inspection display.")
    photo.thumbnail((1300, 1300), Image.Resampling.LANCZOS)
    luma, valid = intensities(photo)
    gamma = number(query.get("gamma"), "gamma", 0.3, 4, default=1)
    if query.get("low") or query.get("high"):
        low = number(query.get("low"), "low", 0, 0.95, default=0.02)
        high = number(query.get("high"), "high", 0.05, 1, default=0.98)
        if high <= low:
            raise SurveyError("The upper display level must exceed the lower level.", field="high")
    elif np.count_nonzero(valid) >= 4:
        low, high = np.quantile(luma[valid], (0.02, 0.98))
    else:
        low, high = 0, 1
    if pattern == "plain":
        return show_support(photo)
    if pattern == "shadow":
        grey = display_tone(luma, valid, 0, max(0.2, float(high)), max(1.6, gamma))
    elif pattern == "window":
        grey = display_tone(luma, valid, low, high, gamma)
    else:
        exclusions = mark_union(photo.size, marks, {"ignore", "occlusion"})
        edge, core = quality_edges(luma, valid & ~exclusions)
        high = max(0.025, float(np.quantile(edge[core], 0.97))) if np.any(core) else 1
        grey = display_tone(edge, core, 0, high, gamma)
        valid = core
    gray = Image.fromarray(grey).convert("RGB")
    return show_support(gray, valid)


def glances_view(store, frame):
    with store.use() as db:
        db.one("frames", frame)
        glances = db.matching("glances", where=(("frame_id", "=", frame),))
    return {"inspections": glances}
