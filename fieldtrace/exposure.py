"""Build a conservative exposure-assist view from a same-position bright/dark burst pair.

The assist view never invents edges: each tile is copied from one source only when that
source is independently readable, outline disagreement is held out as a conflict for
manual review, and areas clipped or unsupported in both stay marked as not read.
"""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .contracts import SurveyError, flag, identifier, integer, number, word
from .images import background, fetch_work, image_bytes, intensities, mark_union
from .inspection import percentiles, prepend_key_line, quality_edges

# Tile provenance codes.
UNREAD, TARGET, REFERENCE, CONFLICT = 0, 1, 2, 3
SOURCE_NAMES = {UNREAD: "unread", TARGET: "target", REFERENCE: "reference", CONFLICT: "conflict"}
PROVENANCE_PALETTE = {
    TARGET: (119, 162, 227),
    REFERENCE: (106, 208, 160),
    CONFLICT: (242, 164, 75),
    UNREAD: (70, 80, 84),
}
CONFLICT_COLOUR = (242, 164, 75)
UNREAD_COLOUR = (46, 54, 60)
SCALE_LIMITS = (0.25, 4.0)


@dataclass(frozen=True)
class ExposureSettings:
    shadows: float
    highlights: float
    tile_px: int
    readable_fraction: float
    edge_threshold: float
    min_conflict_area: int
    conflict_area_limit: float
    use_exclusions: bool

    @classmethod
    def parse(cls, raw):
        return cls(
            number(raw.get("shadows"), "shadows", 0.005, 0.35, default=0.06),
            number(raw.get("highlights"), "highlights", 0.65, 1.0, default=0.97),
            integer(raw.get("tile_px"), "tile_px", 8, 120, default=24),
            number(raw.get("readable_fraction"), "readable_fraction", 0.2, 0.95, default=0.5),
            number(raw.get("edge_threshold"), "edge_threshold", 0.02, 0.3, default=0.07),
            integer(raw.get("min_conflict_area"), "min_conflict_area", 8, 4000, default=48),
            number(raw.get("conflict_area_limit"), "conflict_area_limit", 0.03, 0.9, default=0.25),
            flag(raw.get("use_exclusions"), "use_exclusions", default=True),
        )

    def json(self):
        return {
            "shadows": self.shadows, "highlights": self.highlights, "tile_px": self.tile_px,
            "readable_fraction": self.readable_fraction, "edge_threshold": self.edge_threshold,
            "min_conflict_area": self.min_conflict_area,
            "conflict_area_limit": self.conflict_area_limit,
            "use_exclusions": self.use_exclusions,
        }


def readability(luma, support, excluded, settings):
    """Per-pixel readable mask: supported, not excluded and inside the tonal window."""
    return support & ~excluded & (luma > settings.shadows) & (luma < settings.highlights)


def robust_scale(luma_a, luma_b, common):
    """Median ratio that maps reference tones onto the target frame's tone range."""
    if int(common.sum()) < 64:
        return 1.0, int(common.sum()), False
    centre_a = float(np.median(luma_a[common]))
    centre_b = float(np.median(luma_b[common]))
    if centre_a <= 1e-6:
        return 1.0, int(common.sum()), False
    scale = float(np.clip(centre_b / centre_a, *SCALE_LIMITS))
    return scale, int(common.sum()), True


def conflict_mask(edges_a, edges_b, read_a, read_b, settings):
    """Edge outlines present in only one frame where both frames are readable.

    A conflict needs a *strong* edge in one frame and a genuinely flat response in the
    other; symmetric edge jitter from sensor grain is ignored. Dilation tolerates a
    one-pixel position shift, then connected components below the minimum area are
    treated as noise rather than a subject-motion conflict.
    """
    common = read_a & read_b
    strong = settings.edge_threshold
    weak = settings.edge_threshold * 0.4
    edge_a = edges_a >= strong
    edge_b = edges_b >= strong
    # One side shows an outline, the other side's response there stays below the weak band.
    one_way = ((edge_a & (edges_b < weak)) | (edge_b & (edges_a < weak))) & common
    disagree = ndimage.binary_dilation(one_way, iterations=2) & common
    connected, _ = ndimage.label(disagree, structure=np.ones((3, 3), dtype=bool))
    areas = np.bincount(connected.ravel())
    kept = [i for i in range(1, len(areas)) if areas[i] >= settings.min_conflict_area]
    selected = np.isin(connected, kept) if kept else np.zeros(disagree.shape, dtype=bool)
    extent = disagree.shape[::-1]
    objects = ndimage.find_objects(connected)
    components = []
    for i in kept:
        rows, cols = objects[i - 1]
        here = connected[rows, cols] == i
        area = int(here.sum())
        ys, xs = np.nonzero(here)
        components.append({
            "area_px": area,
            "box": [cols.start, rows.start, cols.stop - cols.start, rows.stop - rows.start],
            "box_normalized": [cols.start / extent[0], rows.start / extent[1],
                               (cols.stop - cols.start) / extent[0], (rows.stop - rows.start) / extent[1]],
            "centroid": [(cols.start + float(xs.mean())) / extent[0],
                         (rows.start + float(ys.mean())) / extent[1]],
        })
    components.sort(key=lambda item: (-item["area_px"], item["box"][1], item["box"][0]))
    return selected, components, int(common.sum())


def tile_boxes(extent, tile_px):
    width, height = extent
    for top in range(0, height, tile_px):
        for left in range(0, width, tile_px):
            yield left, top, min(width, left + tile_px), min(height, top + tile_px)


def classify_tiles(extent, luma_a, luma_b, read_a, read_b, support_a, support_b, conflicts, settings):
    """Assign every tile a provenance code and record the evidence behind the choice."""
    codes = np.zeros(extent[::-1], dtype=np.uint8)
    supported = support_a | support_b
    records = []
    counts = {"target": 0, "reference": 0, "conflict": 0, "unread": 0}
    for index, (x0, y0, x1, y1) in enumerate(tile_boxes(extent, settings.tile_px)):
        area = (x1 - x0) * (y1 - y0)
        target_frac = float(read_b[y0:y1, x0:x1].sum()) / area
        reference_frac = float(read_a[y0:y1, x0:x1].sum()) / area
        supported_frac = float(supported[y0:y1, x0:x1].sum()) / area
        conflict_px = int(conflicts[y0:y1, x0:x1].sum())
        luma_window_a = luma_a[y0:y1, x0:x1]
        luma_window_b = luma_b[y0:y1, x0:x1]
        support_window = supported[y0:y1, x0:x1]
        readable_a = read_a[y0:y1, x0:x1]
        readable_b = read_b[y0:y1, x0:x1]
        mean_a_readable = float(np.mean(luma_window_a[readable_a])) if readable_a.any() else None
        mean_b_readable = float(np.mean(luma_window_b[readable_b])) if readable_b.any() else None
        mean_a = float(np.mean(luma_window_a[support_window])) if support_window.any() else None
        mean_b = float(np.mean(luma_window_b[support_window])) if support_window.any() else None
        if conflict_px:
            code, reason = CONFLICT, "outlines-disagree"
        elif target_frac >= settings.readable_fraction:
            code, reason = TARGET, "target-readable"
        elif reference_frac >= settings.readable_fraction:
            code, reason = REFERENCE, "target-clipped-reference-readable"
        else:
            code = UNREAD
            reason = unread_reason(supported_frac, reference_frac, target_frac, mean_a, mean_b, settings)
        codes[y0:y1, x0:x1] = code
        counts[SOURCE_NAMES[code]] += 1
        records.append({
            "index": index, "box": [x0, y0, x1 - x0, y1 - y0],
            "source": SOURCE_NAMES[code], "reason": reason,
            "supported_fraction": round(supported_frac, 4),
            "target_readable_fraction": round(target_frac, 4),
            "reference_readable_fraction": round(reference_frac, 4),
            "target_mean_luma": None if mean_b_readable is None else round(mean_b_readable, 4),
            "reference_mean_luma": None if mean_a_readable is None else round(mean_a_readable, 4),
            "target_tile_mean_luma": None if mean_b is None else round(mean_b, 4),
            "reference_tile_mean_luma": None if mean_a is None else round(mean_a, 4),
            "conflict_px": conflict_px,
        })
    return codes, records, counts


def unread_reason(supported_frac, reference_frac, target_frac, mean_a, mean_b, settings):
    if supported_frac < settings.readable_fraction:
        return "unsupported-in-both"
    dark = settings.shadows * 1.5
    bright = settings.highlights * 0.985
    means = [value for value in (mean_a, mean_b) if value is not None]
    if means and all(value >= bright for value in means):
        return "both-too-bright"
    if means and all(value <= dark for value in means):
        return "both-too-dark"
    if reference_frac < settings.readable_fraction and target_frac < settings.readable_fraction:
        return "clipped-in-both"
    return "below-readable-fraction"


def feathered_reference_weight(codes, extent, tile_px):
    """Smooth tile-level source choices so adjacent tiles do not show hard seams."""
    weights = (codes == REFERENCE).astype(np.float32)
    sigma = max(1.0, tile_px / 2.5)
    smoothed = ndimage.gaussian_filter(weights, sigma=sigma, mode="nearest")
    # Only blend the tiles that actually participate; conflict/unread tiles are rendered separately.
    smoothed[codes != REFERENCE] = 0.0
    smoothed[codes == REFERENCE] = np.maximum(smoothed[codes == REFERENCE], 0.85)
    return np.clip(smoothed, 0.0, 1.0)


def hatch(drawer, box, colour, spacing=9, width=1):
    x0, y0, x1, y1 = box
    for offset in range(-(y1 - y0), x1 - x0, spacing):
        drawer.line((x0 + offset, y1, x0 + offset + (y1 - y0), y0), fill=colour, width=width)


def assist_picture(rgb_target, rgb_reference, support, codes, weights, scale, settings, extent):
    reference_tuned = np.clip(rgb_reference.astype(np.float32) * scale, 0, 255)
    target = rgb_target.astype(np.float32)
    blended = target * (1 - weights[..., None]) + reference_tuned * weights[..., None]
    canvas = np.asarray(background(extent)).astype(np.float32)
    canvas[support] = blended[support]
    image = Image.fromarray(np.rint(canvas).astype(np.uint8), mode="RGB")
    drawer = ImageDraw.Draw(image)
    border = max(1, extent[0] // 700)
    for x0, y0, x1, y1 in tile_boxes(extent, settings.tile_px):
        code = int(codes[y0, x0])
        if code == CONFLICT:
            patch = np.asarray(image)[y0:y1, x0:x1].astype(np.float32)
            dimmed = np.rint(patch * 0.45 + np.array(CONFLICT_COLOUR) * 0.18).astype(np.uint8)
            image.paste(Image.fromarray(dimmed), (x0, y0))
            hatch(drawer, (x0, y0, x1, y1), CONFLICT_COLOUR)
            drawer.rectangle((x0, y0, x1 - 1, y1 - 1), outline=CONFLICT_COLOUR, width=border)
        elif code == UNREAD:
            drawer.rectangle((x0, y0, x1, y1), fill=UNREAD_COLOUR)
            hatch(drawer, (x0, y0, x1, y1), (62, 72, 76), spacing=7)
            drawer.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(96, 108, 112), width=1)
    legend = [("Current frame", PROVENANCE_PALETTE[TARGET]),
              ("Reference frame", PROVENANCE_PALETTE[REFERENCE]),
              ("Outline conflict", PROVENANCE_PALETTE[CONFLICT]),
              ("Not read", PROVENANCE_PALETTE[UNREAD])]
    return prepend_key_line(image, legend, title="Exposure assist · source provenance per tile")


def provenance_poster(codes, extent, settings, target_name, reference_name):
    palette = np.zeros((extent[1], extent[0], 3), dtype=np.uint8)
    for code, colour in PROVENANCE_PALETTE.items():
        palette[codes == code] = colour
    image = Image.fromarray(palette, mode="RGB")
    drawer = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in tile_boxes(extent, settings.tile_px):
        drawer.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(28, 34, 37), width=1)
    legend = [("Current: " + target_name[:34], PROVENANCE_PALETTE[TARGET]),
              ("Reference: " + reference_name[:32], PROVENANCE_PALETTE[REFERENCE]),
              ("Conflict", PROVENANCE_PALETTE[CONFLICT]),
              ("Unread", PROVENANCE_PALETTE[UNREAD])]
    return prepend_key_line(image, legend, title="Tile provenance map")


def pair_images(store, target_key, reference_key, use_exclusions):
    with store.use() as db:
        target, reference = db.documents("frames", [target_key, reference_key])
        if target["deployment_id"] != reference["deployment_id"] or target["extent"] != reference["extent"]:
            raise SurveyError("Choose a same-camera pair with matching framing and dimensions.",
                              field="reference")
        images = [fetch_work(db, key) for key in (reference_key, target_key)]
        marks = sum((db.matching("marks", where=(("frame_id", "=", key),))
                     for key in (reference_key, target_key)), [])
    excluded = mark_union(images[0].size, marks, {"ignore", "occlusion"}) if use_exclusions \
        else np.zeros(images[0].size[::-1], dtype=bool)
    return target, reference, images, excluded


def build_assist(store, target_key, form, *, save=True):
    reference_key = identifier(form.get("reference"), "reference")
    if reference_key == target_key:
        raise SurveyError("Select a different camera photo.", field="reference")
    settings = ExposureSettings.parse(form)
    target_doc, reference_doc, (image_ref, image_target), excluded = pair_images(
        store, target_key, reference_key, settings.use_exclusions)
    extent = image_target.size
    luma_ref, support_ref = intensities(image_ref)
    luma_target, support_target = intensities(image_target)
    read_ref = readability(luma_ref, support_ref, excluded, settings)
    read_target = readability(luma_target, support_target, excluded, settings)
    edges_ref, _ = quality_edges(luma_ref, read_ref)
    edges_target, _ = quality_edges(luma_target, read_target)
    conflicts, components, common_pixels = conflict_mask(
        edges_ref, edges_target, read_ref, read_target, settings)
    scale, tone_samples, tone_ok = robust_scale(luma_ref, luma_target, read_ref & read_target)
    codes, tiles, counts = classify_tiles(
        extent, luma_ref, luma_target, read_ref, read_target, support_ref, support_target,
        conflicts, settings)
    weights = feathered_reference_weight(codes, extent, settings.tile_px)
    rgb_ref = np.asarray(image_ref.convert("RGB"), dtype=np.uint8)
    rgb_target = np.asarray(image_target.convert("RGB"), dtype=np.uint8)
    support = support_ref | support_target
    conflict_fraction = float(conflicts.sum()) / max(1, common_pixels)
    comparison_state = ("widespread outline disagreement · check framing and subject motion"
                        if conflict_fraction > settings.conflict_area_limit else "locally comparable")
    report = {
        "extent": list(extent),
        "comparison_state": comparison_state,
        "tone_match": {"scale": round(scale, 4), "common_readable_pixels": tone_samples,
                       "estimated": tone_ok, "method": "median luminance ratio on jointly readable pixels"},
        "summary": {
            "tiles_total": len(tiles),
            "tiles_by_source": counts,
            "conflict_components": len(components),
            "conflict_pixels": int(conflicts.sum()),
            "conflict_fraction_of_common": round(conflict_fraction, 5),
            "jointly_readable_pixels": common_pixels,
            "luma": {"reference": percentiles(luma_ref[support_ref]),
                     "target": percentiles(luma_target[support_target])},
        },
        "conflicts": components[:60],
        "tiles": tiles,
        "source_frames": {
            "target": {"id": target_doc["id"], "name": target_doc["name"], "digest": target_doc["digest"]},
            "reference": {"id": reference_doc["id"], "name": reference_doc["name"],
                          "digest": reference_doc["digest"]},
        },
        "method": ("Tiles are copied only from a frame that is independently readable. "
                   "Tiles whose edge outlines disagree are held out as conflicts and never merged; "
                   "tiles clipped or unsupported in both frames stay unread."),
    }
    if not save:
        return report
    assist = assist_picture(rgb_target, rgb_ref, support, codes, weights, scale, settings, extent)
    poster = provenance_poster(codes, extent, settings, target_doc["name"], reference_doc["name"])
    title = word(form.get("name"), "assist name", default="Exposure assist view")
    with store.use(write=True) as db:
        record = db.append("glances", {
            "frame_id": target_key, "reference_id": reference_key, "name": title,
            "kind": "exposure-assist", "settings": settings.json(), "report": report,
            "sources": [{"id": frame["id"], "digest": frame["digest"]}
                        for frame in (reference_doc, target_doc)],
        }, action="exposure assist view saved")
        db.add_asset(record["id"], "assist", image_bytes(assist), "image/png")
        db.add_asset(record["id"], "provenance", image_bytes(poster), "image/png")
    return record
