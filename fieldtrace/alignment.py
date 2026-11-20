"""Audit apparent camera displacement using operator-confirmed immobile landmarks."""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from .contracts import SurveyError, identifier, list_value, need_object, number, unit_point, word
from .geometry import hull, signed_area
from .images import fetch_work, image_bytes, label, pixel_units, show_support

MIN_POINT_GAP = 0.008
REGULARIZER = 1e-9
MAGENTA = (243, 139, 169)
MINT = (128, 236, 188)


@dataclass(frozen=True)
class PlacementSettings:
    anchors: list
    allowed_shift_px: float
    allowed_spread_px: float

    @classmethod
    def parse(cls, values):
        raw = list_value(values.get("anchors"), "anchors", min_items=3, max_items=14)
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
