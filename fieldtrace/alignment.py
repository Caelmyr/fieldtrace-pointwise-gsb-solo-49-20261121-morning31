"""Audit apparent camera displacement using operator-confirmed immobile landmarks."""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .config import MAX_PLACEMENT_CANDIDATES, MAX_PLACEMENT_MARKS
from .contracts import SurveyError, identifier, list_value, need_object, number, unit_point, word
from .geometry import hull, signed_area
from .images import fetch_work, image_bytes, intensities, label, mark_union, pixel_units, show_support

MIN_POINT_GAP = 0.008
REGULARIZER = 1e-9
MAGENTA = (243, 139, 169)
MINT = (128, 236, 188)

# Suggestion-stage limits, expressed on the working image.
MIN_TEMPLATE_EDGE = 13
MAX_TEMPLATE_EDGE = 240
DEFAULT_TEMPLATE_EDGE = 41
CONSISTENCY_TOLERANCE_PX = 2.5
AMBIGUOUS_MARGIN = 0.08
WEAK_SCORE = 0.35
MIN_USABLE_FRACTION = 0.72


@dataclass(frozen=True)
class PlacementSettings:
    anchors: list
    allowed_shift_px: float
    allowed_spread_px: float

    @classmethod
    def parse(cls, values):
        raw = list_value(values.get("anchors"), "anchors", min_items=3, max_items=MAX_PLACEMENT_MARKS)
        used = set()
        sketches = []
        for entry in raw:
            item = need_object(entry, "anchor")
            name = word(item.get("name"), "landmark name", max_length=48)
            if name in used:
                raise SurveyError("Name each landmark distinctly.", field="anchors")
            used.add(name)
            first = unit_point(item.get("before"), "before")
            second = unit_point(item.get("after"), "after")
            sketches.append({"name": name, "before": first, "after": second})
        return cls(
            sketches,
            number(values.get("allowed_shift_px"), "allowed_shift_px", 0.5, 150, default=5),
            number(values.get("allowed_spread_px"), "allowed_spread_px", 0.5, 180, default=4),
        )

    def json(self):
        return {"anchors": self.anchors, "allowed_shift_px": self.allowed_shift_px,
                "allowed_spread_px": self.allowed_spread_px}


def verify_anchors(values):
    pts = np.asarray(values)
    offsets = pts[:, None, :] - pts[None, :, :]
    distances = np.hypot(offsets[:, :, 0], offsets[:, :, 1])
    distances[np.diag_indices_from(distances)] = np.inf
    if np.any(distances < MIN_POINT_GAP):
        raise SurveyError("Use separated landmarks on both photos.", field="anchors")
    outline = hull(values)
    if len(outline) < 3 or abs(signed_area(outline)) < 0.001:
        raise SurveyError("Landmarks need a spread across the photo, rather than one line.", field="anchors")
    return outline


def direct_fit(first, last):
    center_first = np.mean(first, axis=0)
    center_last = np.mean(last, axis=0)
    a = first - center_first
    b = last - center_last
    dot = a.T @ b
    u, singular, vh = np.linalg.svd(dot)
    pivot = np.eye(2)
    if np.linalg.det(vh.T @ u.T) < 0:
        pivot[1, 1] = -1
    basis = vh.T @ pivot @ u.T
    scale = float((singular[0] + pivot[1, 1] * singular[1]) / max(REGULARIZER, np.sum(a * a)))
    translation = center_last - scale * basis @ center_first
    predicted = (scale * (basis @ first.T)).T + translation
    return {
        "rotation_deg": float(np.degrees(np.arctan2(basis[1, 0], basis[0, 0]))),
        "scale": scale, "translation": translation.tolist(),
        "errors": np.linalg.norm(last - predicted, axis=1).tolist(),
    }


def offset_stats(a, b):
    related = b - a
    median = np.median(related, axis=0)
    residue = related - median
    spreads = np.linalg.norm(residue, axis=1)
    return median.tolist(), spreads, related


def alignment_reading(extent, settings):
    left = [row["before"] for row in settings.anchors]
    right = [row["after"] for row in settings.anchors]
    before_hull = verify_anchors(left)
    after_hull = verify_anchors(right)
    pixel_before = np.array(pixel_units(left, extent))
    pixel_after = np.array(pixel_units(right, extent))
    translation, differences, vectors = offset_stats(pixel_before, pixel_after)
    shift = float(np.hypot(*translation))
    spread = float(np.percentile(differences, 90))
    hints = direct_fit(pixel_before, pixel_after)
    state = "stable view"
    if spread > settings.allowed_spread_px:
        state = "inconsistent landmarks"
    elif shift > settings.allowed_shift_px:
        state = "camera displaced"
    return {
        "conclusion": state, "shift_px": shift, "spread_p90_px": spread,
        "median_translation_px": translation,
        "similarity_hint": {key: hints[key] for key in ("rotation_deg", "scale", "translation")},
        "spans": {"before": before_hull, "after": after_hull},
        "correspondences": [
            row | {"dx_px": float(vector[0]), "dy_px": float(vector[1]),
                   "translation_residual_px": float(residual), "similarity_residual_px": float(error)}
            for row, residual, vector, error in zip(settings.anchors, differences, vectors, hints["errors"])
        ],
        "method": "Operator landmarks with median displacement and a separate similarity-fit hint",
    }


def annotated_camera(image, anchors, field, bounds, hue):
    pane = show_support(image)
    pane.thumbnail((465, 360), Image.Resampling.LANCZOS)
    if not pane.width or not pane.height:
        raise SurveyError("The camera reference is empty.")
    draw = ImageDraw.Draw(pane)
    contour = pixel_units(bounds, pane.size)
    draw.line(contour + [contour[0]], fill=hue, width=2)
    for i, item in enumerate(anchors, 1):
        x, y = pixel_units([item[field]], pane.size)[0]
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), outline=hue, width=2)
        label(draw, (min(x + 6, pane.width - 24), min(y + 2, pane.height - 19)), i, fill=hue, size=12)
    return pane


def camera_pair_board(images, report):
    out = Image.new("RGB", (990, 502), (20, 31, 32))
    draw = ImageDraw.Draw(out)
    label(draw, (22, 17), "Camera placement cross-check", size=22)
    label(draw, (22, 52), "Earlier reference", size=14)
    label(draw, (516, 52), "Current photo", size=14)
    for i, (field, hue) in enumerate((("before", MINT), ("after", MAGENTA))):
        pane = annotated_camera(images[i], report["correspondences"], field, report["spans"][field], hue)
        x = i * 490 + 19 + (465 - pane.width) // 2
        y = 88 + (350 - pane.height) // 2
        out.paste(pane, (x, y))
    title = f"{report['conclusion']} · {report['shift_px']:.2f} px median displacement"
    label(draw, (22, 453), title, size=15)
    label(draw, (22, 476), "Pairs report image movement; they do not prove that the observed subject moved.", size=11)
    return out


def create_alignment(store, target, form, *, save=True):
    prior = identifier(form.get("reference"), "reference")
    if prior == target:
        raise SurveyError("Select a different camera reference.", field="reference")
    settings = PlacementSettings.parse(form)
    with store.use() as db:
        first, last = db.documents("frames", [prior, target])
        if first["station_id"] != last["station_id"] or first["extent"] != last["extent"]:
            raise SurveyError("Use camera photos from the same station and with identical dimensions.", field="reference")
        images = [fetch_work(db, key) for key in (prior, target)]
    result = alignment_reading(last["extent"], settings)
    if not save:
        return result
    report = {
        "kind": "placement", "name": word(form.get("name"), "placement report name", default="Placement audit"),
        "frame_id": target, "reference_id": prior, "settings": settings.json(), "report": result,
        "sources": [{"id": frame["id"], "digest": frame["digest"], "version": frame["version"]}
                    for frame in (first, last)],
    }
    board = camera_pair_board(images, result)
    with store.use(write=True) as db:
        item = db.append("glances", report, action="camera placement audit saved")
        db.add_asset(item["id"], "guide", image_bytes(board), "image/png")
    return item


# ---------------------------------------------------------------------------
# Assisted landing suggestions: local template search, never an auto-match.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuggestionSettings:
    marks: list
    search_radius_px: float
    template_px: int

    @classmethod
    def parse(cls, raw, extent):
        entries = list_value(raw.get("marks"), "marks", min_items=1, max_items=MAX_PLACEMENT_MARKS)
        used = set()
        marks = []
        for index, entry in enumerate(entries, 1):
            item = need_object(entry, "mark")
            name = word(item.get("name") or f"Mark {index}", "landmark name", max_length=48)
            if name in used:
                raise SurveyError("Name each landmark distinctly.", field="marks")
            used.add(name)
            marks.append({"name": name, "before": unit_point(item.get("before"), "before")})
        radius = number(raw.get("search_radius_px"), "search_radius_px", 4, 200, default=28)
        longest = max(extent)
        template = int(number(raw.get("template_px"), "template_px",
                              MIN_TEMPLATE_EDGE, min(MAX_TEMPLATE_EDGE, longest // 2),
                              default=DEFAULT_TEMPLATE_EDGE))
        return cls(marks, radius, template)

    def json(self):
        return {"marks": self.marks, "search_radius_px": self.search_radius_px, "template_px": self.template_px}


def prepped_surface(image, excluded):
    """Luminance with gentle local contrast lift, so a brightness change does not dominate."""
    luma, support = intensities(image)
    active = support & ~excluded
    blurred = ndimage.gaussian_filter(np.where(active, luma, 0), 2.2, mode="nearest")
    weight = ndimage.gaussian_filter(active.astype(np.float32), 2.2, mode="nearest")
    local = np.divide(blurred, weight, out=np.zeros_like(blurred), where=weight > 0.6)
    detail = luma - local
    scale = np.std(detail[active]) if np.count_nonzero(active) else 0.0
    if scale < 1e-6:
        normalized = luma
    else:
        normalized = np.clip(0.5 + detail / (6.0 * scale), 0, 1)
    return normalized.astype(np.float32), active


def patch_bounds(cx, cy, half, width, height):
    x0 = max(0, int(round(cx - half)))
    y0 = max(0, int(round(cy - half)))
    x1 = min(width, int(round(cx + half + 1)))
    y1 = min(height, int(round(cy + half + 1)))
    return x0, y0, x1, y1


def local_correlation_map(template, active_template, scene, active_scene):
    """Mean-removed normalized correlation over valid pixels, via FFT cross-correlation."""
    th, tw = template.shape
    sh, sw = scene.shape
    if th > sh or tw > sw:
        return np.full((max(0, sh - th + 1), max(0, sw - tw + 1)), np.nan, dtype=np.float32)
    mask = active_template.astype(np.float32)
    region_ok = active_scene.astype(np.float32)
    scene_weighted = scene * region_ok
    template_values = template * mask
    # FFT correlation cost and memory scale with the window, not with template*window.
    sums = {
        "n": _fft_correlate(region_ok, mask),
        "sb": _fft_correlate(scene_weighted, mask),
        "sb2": _fft_correlate(scene_weighted ** 2, mask),
        "sa": _fft_correlate(region_ok, template_values),
        "sa2": _fft_correlate(region_ok, template_values ** 2),
        "sab": _fft_correlate(scene_weighted, template_values),
    }
    # FFT output indexing: entry [i, j] holds the sum with the template centred on scene [i, j].
    y0, y1 = th // 2, sh - (th - 1 - th // 2)
    x0, x1 = tw // 2, sw - (tw - 1 - tw // 2)
    n = sums["n"][y0:y1, x0:x1]
    needed = float(mask.sum()) * MIN_USABLE_FRACTION
    usable = n >= needed
    out = np.full(n.shape, np.nan, dtype=np.float32)
    if not np.any(usable):
        return out
    count = n[usable]
    cov = sums["sab"][y0:y1, x0:x1][usable] - sums["sa"][y0:y1, x0:x1][usable] * \
        sums["sb"][y0:y1, x0:x1][usable] / count
    var_a = np.maximum(
        sums["sa2"][y0:y1, x0:x1][usable] - sums["sa"][y0:y1, x0:x1][usable] ** 2 / count, 1e-12)
    var_b = np.maximum(
        sums["sb2"][y0:y1, x0:x1][usable] - sums["sb"][y0:y1, x0:x1][usable] ** 2 / count, 1e-12)
    score = cov / np.sqrt(var_a * var_b)
    out[usable] = np.clip(score, -1.0, 1.0).astype(np.float32)
    return out


def _fft_correlate(scene, kernel):
    """Cross-correlation of scene with kernel (origin at kernel centre), same shape as scene."""
    kh, kw = kernel.shape
    padded = np.zeros_like(scene, dtype=np.float32)
    padded[:kh, :kw] = kernel
    rolled = np.roll(padded, (-((kh - 1) // 2), -((kw - 1) // 2)), axis=(0, 1))
    spectrum = np.fft.rfft2(scene) * np.conj(np.fft.rfft2(rolled))
    return np.fft.irfft2(spectrum, s=scene.shape).astype(np.float32)


def distinct_peaks(score_map, limit):
    """Non-maximum suppression on a correlation map, returning spaced local maxima."""
    if not score_map.size or np.all(np.isnan(score_map)):
        return []
    peaks = []
    guarded = np.where(np.isnan(score_map), -2.0, score_map)
    work = guarded.copy()
    while len(peaks) < limit:
        flat = int(np.argmax(work))
        oy, ox = np.unravel_index(flat, work.shape)
        value = float(guarded[oy, ox])
        if value <= -1.5:
            break
        peaks.append((oy, ox, value))
        y0, y1 = max(0, oy - 6), min(work.shape[0], oy + 7)
        x0, x1 = max(0, ox - 6), min(work.shape[1], ox + 7)
        work[y0:y1, x0:x1] = -2.0
    return peaks


def suggest_one_mark(index, mark, before_surface, after_surface, before_active, after_active,
                     extent, settings):
    width, height = extent
    px, py = pixel_units([mark["before"]], extent)[0]
    half = settings.template_px // 2
    crosses_edge = px - half < 0 or py - half < 0 or px + half + 1 > width or py + half + 1 > height
    tx0, ty0, tx1, ty1 = patch_bounds(px, py, half, width, height)
    template = before_surface[ty0:ty1, tx0:tx1]
    template_active = before_active[ty0:ty1, tx0:tx1]
    record = {
        "index": index, "name": mark["name"], "before": mark["before"],
        "template_bounds_px": [tx0, ty0, tx1, ty1],
        "template_usable_fraction": float(template_active.mean()) if template.size else 0.0,
        "candidates": [], "state": "review", "note": "",
    }
    if crosses_edge or template.shape[0] < MIN_TEMPLATE_EDGE or template.shape[1] < MIN_TEMPLATE_EDGE:
        record["state"] = "edge-of-frame"
        record["note"] = "The marker sits too close to the photo edge for a full comparison patch."
        return record
    if record["template_usable_fraction"] < MIN_USABLE_FRACTION:
        record["state"] = "occluded"
        record["note"] = "Most of this patch is unsupported or excluded in the earlier photo."
        return record
    radius = int(round(settings.search_radius_px))
    sy0, sy1 = max(0, int(py) - radius), min(height, int(py) + radius + 1)
    sx0, sx1 = max(0, int(px) - radius), min(width, int(px) + radius + 1)
    scene = after_surface[sy0:sy1, sx0:sx1]
    scene_active = after_active[sy0:sy1, sx0:sx1]
    score_map = local_correlation_map(template, template_active, scene, scene_active)
    peaks = distinct_peaks(score_map, MAX_PLACEMENT_CANDIDATES)
    if not peaks:
        record["state"] = "occluded"
        record["note"] = "No usable patch inside the search window on the current photo."
        return record
    for oy, ox, score in peaks:
        cx = sx0 + ox + (tx1 - tx0) / 2
        cy = sy0 + oy + (ty1 - ty0) / 2
        record["candidates"].append({
            "after": [cx / (width - 1), cy / (height - 1)],
            "center_px": [round(cx, 2), round(cy, 2)],
            "score": round(score, 4),
        })
    best = record["candidates"][0]
    runner_up = record["candidates"][1]["score"] if len(record["candidates"]) > 1 else -1.0
    if best["score"] < WEAK_SCORE:
        record["state"] = "weak"
        record["note"] = "No patch in the search window resembles this marker; leave it for manual review."
    elif best["score"] - runner_up < AMBIGUOUS_MARGIN:
        record["state"] = "ambiguous"
        record["note"] = "Repeated texture: several patches match almost equally. Confirm the landing point yourself."
    else:
        record["state"] = "suggested"
        record["note"] = "One distinct landing point; still requires operator confirmation."
    return record


def add_consistency(suggestions, extent):
    """Compare each preferred landing point with the robust shared displacement."""
    preferred = []
    for row in suggestions:
        if row["candidates"]:
            bx, by = pixel_units([row["before"]], extent)[0]
            cx, cy = row["candidates"][0]["center_px"]
            preferred.append((row["index"], cx - bx, cy - by))
    if len(preferred) < 2:
        return {"consensus_shift_px": None, "consistent_mark_count": 0, "review_mark_count": len(suggestions)}
    shifts = np.array([[dx, dy] for _, dx, dy in preferred])
    consensus = np.median(shifts, axis=0)
    for row in suggestions:
        match = next((item for item in preferred if item[0] == row["index"]), None)
        if match is None:
            row["consensus"] = "held"
            continue
        delta = float(np.hypot(match[1] - consensus[0], match[2] - consensus[1]))
        row["preferred_residual_px"] = round(delta, 2)
        row["consensus"] = "fits" if delta <= CONSISTENCY_TOLERANCE_PX else "outlier"
    fitting = sum(1 for row in suggestions if row.get("consensus") == "fits")
    return {
        "consensus_shift_px": [round(float(consensus[0]), 2), round(float(consensus[1]), 2)],
        "consistent_mark_count": fitting,
        "review_mark_count": len(suggestions) - fitting,
        "consistency_tolerance_px": CONSISTENCY_TOLERANCE_PX,
        "note": "Consensus is a review aid across preferred suggestions only; it never confirms a point.",
    }


def placement_suggestions(store, target, form):
    prior = identifier(form.get("reference"), "reference")
    if prior == target:
        raise SurveyError("Select a different camera reference.", field="reference")
    with store.use() as db:
        first, last = db.documents("frames", [prior, target])
        if first["station_id"] != last["station_id"] or first["extent"] != last["extent"]:
            raise SurveyError("Use camera photos from the same station and with identical dimensions.",
                              field="reference")
        images = [fetch_work(db, key) for key in (prior, target)]
        marks_by_frame = [db.matching("marks", where=(("frame_id", "=", key),)) for key in (prior, target)]
    settings = SuggestionSettings.parse(form, last["extent"])
    exclusions = [mark_union(image.size, marks, {"ignore", "occlusion"})
                  for image, marks in zip(images, marks_by_frame)]
    surfaces = [prepped_surface(image, excluded) for image, excluded in zip(images, exclusions)]
    (before_surface, before_active), (after_surface, after_active) = surfaces
    suggestions = [
        suggest_one_mark(i, mark, before_surface, after_surface, before_active, after_active,
                         last["extent"], settings)
        for i, mark in enumerate(settings.marks, 1)
    ]
    consensus = add_consistency(suggestions, last["extent"])
    return {
        "reference_id": prior, "frame_id": target, "extent": list(last["extent"]),
        "search_radius_px": settings.search_radius_px, "template_px": settings.template_px,
        "suggestions": suggestions, "consistency": consensus,
        "method": "Local normalized-correlation search with support masking; suggestions are unconfirmed.",
    }
