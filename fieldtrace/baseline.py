"""Multi-reference empty-scene baseline for static cameras.

Several clear empty frames from the same camera position are combined into a
pixel-wise reference model.  Regions that change repeatedly across the empty
frames (wind-blown grass, light flicker) are remembered as *recurrent change*
zones instead of being erased wholesale: new contours that extend beyond those
zones survive as review candidates.  Pixels that no suitable reference can
cover, or whose lighting is not comparable, are reported as unmeasured rather
than guessed at.  A single-reference request is still accepted and is labelled
as a simple one-photo difference.
"""

from dataclasses import dataclass
import math
import warnings

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .config import MAX_WITNESSES
from .contracts import (
    SurveyError, flag, identity_list, integer, list_value, number, word,
)
from .images import (
    fetch_work, image_bytes, intensities, label, mark_union, normalized_crop,
    show_support, support_geometry,
)
from .inspection import masked_blur, prepend_key_line, robust_brightness_shift

MAX_BASELINE_REFERENCES = 12
MIN_BASELINE_COVER = 0.25
CLIP_LOW, CLIP_HIGH = 0.016, 0.988
MAX_RECURRENT_ZONES = 40

BASELINE_PALETTE = [
    (51, 58, 62),      # 0 stable scene
    (195, 115, 180),   # 1 unmeasured / not comparable
    (96, 132, 204),    # 2 recurrent change (wind grass, flicker)
    (232, 176, 92),    # 3 change inside a recurrent zone (suppressed)
    (84, 208, 176),    # 4 new content outside recurrent zones (for review)
]
BASELINE_LEGEND = [
    ("Unmeasured", BASELINE_PALETTE[1]),
    ("Recurrent change", BASELINE_PALETTE[2]),
    ("Change in recurrent zone", BASELINE_PALETTE[3]),
    ("New content for review", BASELINE_PALETTE[4]),
]


@dataclass(frozen=True)
class BaselineSettings:
    threshold: float
    recurrent_threshold: float
    min_area: int
    radius: int
    max_contrast_shift: float
    min_coverage: float
    permit_ir: bool

    @classmethod
    def parse(cls, raw):
        return cls(
            number(raw.get("threshold"), "threshold", 0.01, 0.4, default=0.085),
            number(raw.get("recurrent_threshold"), "recurrent_threshold", 0.008, 0.3, default=0.045),
            integer(raw.get("min_area"), "min_area", 8, 4000, default=60),
            integer(raw.get("radius"), "radius", 0, 6, default=1),
            number(raw.get("max_contrast_shift"), "max_contrast_shift", 0.015, 0.5, default=0.18),
            number(raw.get("min_coverage"), "min_coverage", 0.2, 1.0, default=MIN_BASELINE_COVER),
            flag(raw.get("permit_ir"), "permit_ir", default=True),
        )

    def json(self):
        return {
            "threshold": self.threshold, "recurrent_threshold": self.recurrent_threshold,
            "min_area": self.min_area, "radius": self.radius,
            "max_contrast_shift": self.max_contrast_shift, "min_coverage": self.min_coverage,
            "permit_ir": self.permit_ir,
        }


def reference_inputs(db, frame_id, reference_ids, settings):
    """Validate that every reference shares the camera position and lighting."""
    target = db.one("frames", frame_id)
    references = db.documents("frames", reference_ids)
    for ref in references:
        if ref["deployment_id"] != target["deployment_id"] or ref["extent"] != target["extent"]:
            raise SurveyError("Choose empty photos from the same camera position and framing.",
                              field="references")
        if ref["illumination"] != target["illumination"]:
            raise SurveyError("Use references with the same illumination as the current photo.",
                              field="references")
        if not settings.permit_ir and target["illumination"] == "night-ir":
            raise SurveyError("Enable infrared comparisons for night-ir photos.", field="references")
    return target, references


def reference_arrays(images, support_domain, settings):
    """Blur each reference and keep a per-pixel mask of where it is comparable."""
    rows = []
    for image in images:
        luma, visible = intensities(image)
        clipping = (luma >= CLIP_HIGH) | (luma <= CLIP_LOW)
        candidate = visible & support_domain & ~clipping
        smoothed, valid = masked_blur(luma, candidate, settings.radius)
        rows.append({"luma": smoothed, "mask": valid,
                     "support": int(valid.sum()) / max(1, valid.size)})
    return rows


def qualify_references(rows, target_smoothed, settings):
    """A reference with a global brightness jump is lighting, not local change."""
    eligible = []
    entries = []
    for index, row in enumerate(rows):
        common = row["mask"]
        offset, mad = robust_brightness_shift(row["luma"], target_smoothed, common)
        usable = offset is not None and abs(offset) <= settings.max_contrast_shift
        reason = None
        if offset is None:
            reason = "too little shared visible area"
            usable = False
        elif abs(offset) > settings.max_contrast_shift:
            reason = "global lighting shift exceeds the contrast limit"
        row["offset"] = offset
        row["mad"] = mad
        row["usable"] = usable
        entries.append({
            "index": index, "support_fraction": round(row["support"], 5),
            "brightness_offset": None if offset is None else round(float(offset), 5),
            "robust_spread": None if mad is None else round(float(mad), 5),
            "used": bool(usable), "excluded_reason": reason,
        })
        if usable:
            eligible.append(row)
    return eligible, entries


def baseline_stack(eligible):
    """Median empty-scene level and its spread per pixel, ignoring NaN gaps."""
    stacked = np.stack([row["luma"] for row in eligible], axis=0)
    masks = np.stack([row["mask"] for row in eligible], axis=0)
    coverage = masks.sum(axis=0).astype(np.float32)
    masked = np.where(masks, stacked, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        median = np.nanmedian(masked, axis=0).astype(np.float32)
        spread = np.nanmedian(np.abs(masked - median[None, ...]), axis=0).astype(np.float32)
    median = np.where(np.isfinite(median), median, 0.0)
    spread = np.where(np.isfinite(spread), spread, 0.0)
    return median, spread, coverage


def zone_summary(zones, extent):
    pieces, count = ndimage.label(zones, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return {"zone_count": 0, "zones": []}, pieces
    area = np.bincount(pieces.ravel())
    kept = [i for i in range(1, count + 1) if area[i] >= 24][:MAX_RECURRENT_ZONES]
    width, height = extent
    outlines = ndimage.find_objects(pieces)
    zones_out = []
    for i in kept:
        rows, cols = outlines[i - 1]
        zones_out.append({
            "area_px": int(area[i]),
            "box": [cols.start / width, rows.start / height,
                    (cols.stop - cols.start) / width, (rows.stop - rows.start) / height],
        })
    return {"zone_count": len(kept), "zones": zones_out}, pieces


def baseline_components(binary, response, residual, settings, recurrent):
    """Connected new-content components, each split into inside/outside grass zones.

    A light opening removes single-pixel camera noise along grass edges before
    labelling, so the candidate list keeps coherent new contours rather than
    scattered speckle.  Recurrent-zone pixels are never part of the binary, so
    a blob that merely sits behind moving grass is retained by its visible rim.
    """
    cleaned = ndimage.binary_opening(binary, structure=np.ones((3, 3), bool), iterations=1)
    connected, _ = ndimage.label(cleaned, structure=np.ones((3, 3), dtype=bool))
    area_by_index = np.bincount(connected.ravel())
    available = [int(i) for i in np.flatnonzero(area_by_index >= settings.min_area) if i]
    available.sort(key=lambda i: (-int(area_by_index[i]), i))
    kept = available[:MAX_WITNESSES]
    extent = binary.shape[::-1]
    objects = ndimage.find_objects(connected)
    candidates, suppressed = [], []
    selected = np.zeros(binary.shape, dtype=bool)
    for order, i in enumerate(kept, 1):
        rows, cols = objects[i - 1]
        here = connected[rows, cols] == i
        measure = response[rows, cols][here]
        in_grass = recurrent[rows, cols][here]
        total = int(here.sum())
        inside = int(in_grass.sum())
        outside = total - inside
        pos_y, pos_x = np.nonzero(here)
        y0, y1, x0, x1 = rows.start, rows.stop, cols.start, cols.stop
        width, height = extent
        item = {
            "index": order, "area_px": total,
            "box": [x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height],
            "centroid": [(x0 + float(pos_x.mean())) / width, (y0 + float(pos_y.mean())) / height],
            "median_change": float(np.median(measure)),
            "maximum_change": float(np.max(measure)),
            "direction": "brighter" if float(np.median(residual[rows, cols][here])) >= 0 else "darker",
            "pixels_in_recurrent_zone": inside,
            "pixels_outside_recurrent_zone": outside,
            "outside_fraction": round(outside / max(1, total), 4),
            "touches_recurrent_zone": bool(ndimage.binary_dilation(
                recurrent[rows, cols], structure=np.ones((3, 3), bool), iterations=1)[here].any()),
        }
        # Small, weak fragments at the grass boundary are vegetation edge flicker,
        # not a coherent new outline; stronger or larger blobs stay for review.
        weak_edge = (total < 4 * settings.min_area
                     and item["median_change"] < 1.6 * settings.threshold
                     and item["touches_recurrent_zone"])
        if weak_edge:
            suppressed.append(item | {"suppression": "weak fragment on a recurrent change boundary"})
            continue
        # A blob fully buried in a known wind/flicker zone is vegetation, not an animal.
        if outside == 0:
            suppressed.append(item | {"suppression": "entirely inside a recurrent change zone"})
            continue
        item["note"] = ("Touches a recurrent change zone; the contour outside it is retained for review."
                        if item["touches_recurrent_zone"] else "Clear of recurrent change zones.")
        candidates.append(item)
        selected |= connected == i
    return candidates, suppressed, selected, len(available) > len(kept)


def baseline_delta(target, reference_rows, exclusions, settings):
    luma_t, support_t = intensities(target)
    clipping_t = (luma_t >= CLIP_HIGH) | (luma_t <= CLIP_LOW)
    domain = support_t & ~exclusions & ~clipping_t
    target_smooth, target_valid = masked_blur(luma_t, domain, settings.radius)

    eligible, reference_report = qualify_references(reference_rows, target_smooth, settings)
    if not eligible:
        raise SurveyError("No reference photo is comparable here; check lighting and visible area.",
                          field="references")
    median, spread, coverage = baseline_stack(eligible)
    enough = coverage >= math.ceil(settings.min_coverage * len(eligible))
    measured = target_valid & enough
    unmeasured = support_t & ~exclusions & ~measured

    # Recurrent change: pixels that genuinely move between the empty references.
    recurrent_raw = measured & (spread >= settings.recurrent_threshold)
    recurrent = ndimage.binary_closing(recurrent_raw, structure=np.ones((3, 3), bool), iterations=1)
    # A one-pixel fringe absorbs blade-edge flicker without blanketing the scene;
    # new contours beyond that fringe remain visible and are kept for review.
    recurrent = ndimage.binary_dilation(recurrent, structure=np.ones((3, 3), bool), iterations=1)
    recurrent &= measured

    residual = target_smooth - median
    response = np.abs(residual)
    changed = measured & (response >= settings.threshold)
    in_recurrent = changed & recurrent
    fresh = changed & ~recurrent

    area_fraction = int(changed.sum()) / max(1, int(measured.sum()))
    lighting_change = area_fraction > 0.60
    if lighting_change:
        candidates, suppressed, selected, truncated = [], [], np.zeros(changed.shape, bool), False
        comparison_state = "whole-frame lighting change"
    else:
        candidates, suppressed, selected, truncated = baseline_components(
            fresh, response, residual, settings, recurrent)
        comparison_state = "multi-reference baseline" if len(eligible) > 1 else "single reference difference"

    recurrent_info, _ = zone_summary(recurrent, target.size)
    classes = np.zeros(changed.shape, dtype=np.uint8)
    classes[unmeasured] = 1
    classes[recurrent] = 2
    classes[in_recurrent] = 3
    classes[selected] = 4

    warnings = []
    if len(eligible) == 1:
        warnings.append("Only one usable reference: this is a simple one-photo difference; "
                        "recurrent change cannot be separated from a new subject.")
    rejected = [row for row in reference_report if not row["used"]]
    if rejected:
        warnings.append(f"{len(rejected)} reference photo(s) excluded for lighting or support; see references.")
    if int(unmeasured.sum()) > 0:
        warnings.append("Unmeasured areas were not covered by enough suitable references and are marked for manual review.")
    if lighting_change:
        warnings.append("Most of the frame changed at once, so local candidates were withheld as a lighting event.")
    if truncated:
        warnings.append("More candidates than the retained limit exist; raise the area threshold or narrow the view.")

    report = {
        "comparison_state": comparison_state,
        "reference_count": len(reference_rows),
        "usable_reference_count": len(eligible),
        "references": reference_report,
        "threshold": settings.threshold,
        "recurrent_threshold": settings.recurrent_threshold,
        "min_coverage": settings.min_coverage,
        "support": support_geometry(measured),
        "unmeasured_pixels": int(unmeasured.sum()),
        "excluded_pixels": int(exclusions.sum()),
        "changed_fraction": round(area_fraction, 6),
        "recurrent_change": recurrent_info | {
            "pixels": int(recurrent.sum()),
            "fraction": round(float(recurrent.sum()) / max(1, recurrent.size), 6),
        },
        "review_candidates": candidates,
        "suppressed_candidates": suppressed,
        "truncated": truncated,
        "warnings": warnings,
    }
    return report, classes, recurrent_info


def baseline_map(target, classes, report):
    base = np.asarray(show_support(target), dtype=np.float32)
    overlay = np.asarray(BASELINE_PALETTE, dtype=np.float32)[classes]
    coverage = np.where(classes == 0, 0.0, 0.5)[..., None]
    rgb = np.rint(base * (1 - coverage) + overlay * coverage).astype(np.uint8)
    output = Image.fromarray(rgb)
    draw = ImageDraw.Draw(output)
    for candidate in report["review_candidates"]:
        x0, y0, x1, y1 = normalized_crop(candidate["box"], output.size)
        draw.rectangle((x0, y0, x1, y1), outline=(158, 244, 179), width=max(2, output.width // 390))
        label(draw, (x0 + 4, y0 + 4), str(candidate["index"]), fill=(180, 252, 192))
    title = f"Empty-scene baseline · {report['comparison_state']} · {report['usable_reference_count']} reference(s)"
    return prepend_key_line(output, BASELINE_LEGEND, title=title)


def load_baseline_inputs(store, frame_id, reference_ids, settings):
    with store.use() as db:
        target, references = reference_inputs(db, frame_id, reference_ids, settings)
        images = [fetch_work(db, key) for key in reference_ids]
        target_image = fetch_work(db, frame_id)
        target_marks = db.matching("marks", where=(("frame_id", "=", frame_id),))
        all_marks = sum((db.matching("marks", where=(("frame_id", "=", key),)) for key in reference_ids), [])
        all_marks.extend(target_marks)
    exclusions = mark_union(target_image.size, all_marks, {"ignore", "occlusion"})
    rows = reference_arrays(images, np.ones(target_image.size[::-1], dtype=bool), settings)
    return target, references, target_image, rows, exclusions


def analyze_baseline(store, frame_id, form):
    raw_references = list_value(form.get("references"), "references",
                                min_items=1, max_items=MAX_BASELINE_REFERENCES)
    reference_ids = identity_list(raw_references, "references", limit=MAX_BASELINE_REFERENCES)
    if frame_id in reference_ids:
        raise SurveyError("The current photo cannot be its own empty-scene reference.", field="references")
    settings = BaselineSettings.parse(form)
    target, references, target_image, rows, exclusions = load_baseline_inputs(
        store, frame_id, reference_ids, settings)
    report, classes, _ = baseline_delta(target_image, rows, exclusions, settings)
    return target, references, target_image, settings, report, classes


def preview_baseline(store, frame_id, form):
    _, _, target_image, _, report, classes = analyze_baseline(store, frame_id, form)
    return report, baseline_map(target_image, classes, report)


def save_baseline(store, frame_id, form):
    target, references, target_image, settings, report, classes = analyze_baseline(store, frame_id, form)
    view = baseline_map(target_image, classes, report)
    with store.use(write=True) as db:
        latest = db.one("frames", frame_id)
        if latest["digest"] != target["digest"]:
            raise SurveyError("The image changed during inspection.", status=409)
        saved = db.append("glances", {
            "name": word(form.get("name"), "baseline title", default="Empty-scene baseline difference"),
            "frame_id": frame_id, "kind": "baseline",
            "report": report, "settings": settings.json(),
            "reference_ids": [ref["id"] for ref in references],
            "sources": [{"id": item["id"], "digest": item["digest"]}
                        for item in [*references, target]],
        }, action="empty-scene baseline saved")
        db.add_asset(saved["id"], "baseline", image_bytes(view), "image/png")
    return saved
