"""Inspect equivalent windows without conflating missing area with luminance change."""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .contracts import SurveyError, flag, identifier, integer, number, option, rectangle, word
from .images import (
    display_tone, fetch_work, image_bytes, intensities, label, mark_union, normalized_crop, show_support,
)
from .inspection import percentiles


@dataclass(frozen=True)
class PairSettings:
    box: tuple | None
    view: str
    brightness: str
    gamma: float
    stripe: int
    use_exclusions: bool

    @classmethod
    def parse(cls, raw):
        return cls(
            rectangle(raw["box"]) if raw.get("box") else None,
            option(raw.get("view"), "view", {"two", "wipe", "alternating"}, default="two"),
            option(raw.get("brightness"), "brightness", {"color", "common-window"}, default="color"),
            number(raw.get("gamma"), "gamma", .35, 4, default=1),
            integer(raw.get("stripe"), "stripe", 8, 160, default=46),
            flag(raw.get("use_exclusions"), "use_exclusions", default=True),
        )

    def json(self):
        return {
            "box": self.box, "view": self.view, "brightness": self.brightness,
            "gamma": self.gamma, "stripe": self.stripe, "use_exclusions": self.use_exclusions,
        }


def camera_extract(photo, marks, selection, skipped):
    complete = normalized_crop(selection, photo.size) if selection else (0, 0, *photo.size)
    x0, y0, x1, y1 = complete
    if min(x1 - x0, y1 - y0) < 8:
        raise SurveyError("The shared window must retain at least eight pixels on each side.", field="box")
    panel = photo.crop(complete)
    intensity, support = intensities(panel)
    if skipped:
        exclusions = mark_union(photo.size, marks, {"ignore", "occlusion"})[y0:y1, x0:x1]
    else:
        exclusions = np.zeros(support.shape, dtype=bool)
    core = support & ~exclusions
    return panel, intensity, core, list(complete), int(exclusions.sum())


def paired_histograms(v1, v2, m1, m2):
    comparable = m1 & m2
    one = v1[comparable]
    two = v2[comparable]
    n = int(comparable.sum())
    difference = two - one
    levels = np.linspace(0, 1, 21)
    delta_edges = np.linspace(-1, 1, 31)
    counts = {
        "joint": n, "left_only": int((m1 & ~m2).sum()), "right_only": int((m2 & ~m1).sum()),
        "neither": int((~m1 & ~m2).sum()),
    }
    return {
        "support": counts, "left": percentiles(one), "right": percentiles(two),
        "difference": percentiles(difference), "luminance_edges": levels.tolist(),
        "left_bins": np.histogram(one, levels)[0].tolist(), "right_bins": np.histogram(two, levels)[0].tolist(),
        "difference_edges": delta_edges.tolist(), "difference_bins": np.histogram(difference, delta_edges)[0].tolist(),
    }


def shared_window(luminance, support):
    combined = np.concatenate([image[ok] for image, ok in zip(luminance, support)])
    if len(combined) < 8:
        return 0.0, 1.0
    low, high = np.quantile(combined, (.03, .97))
    if high < low + 0.025:
        return max(0, float(low) - .05), min(1, float(high) + .05)
    return float(low), float(high)


def visible_layer(photo, values, support, limits, settings):
    if settings.brightness == "color":
        return show_support(photo, support)
    map_ = display_tone(values, support, limits[0], limits[1], settings.gamma)
    grayscale = Image.fromarray(map_).convert("RGB")
    return show_support(grayscale, support)


def merge_comparison(left, right, mode, stripe):
    w, h = left.size
    merged = left.copy()
    if mode == "wipe":
        centre = w // 2
        merged.paste(right.crop((centre, 0, w, h)), (centre, 0))
        ImageDraw.Draw(merged).line((centre, 0, centre, h), fill=(237, 204, 116), width=2)
    else:
        for x in range(stripe, w, 2 * stripe):
            end = min(w, x + stripe)
            merged.paste(right.crop((x, 0, end, h)), (x, 0))
    return merged


def histogram_chart(result, width=980, height=183):
    canvas = Image.new("RGB", (width, height), (26, 35, 37))
    draw = ImageDraw.Draw(canvas)
    x0, x1, y0, y1 = 41, width - 39, 42, height - 33
    count = len(result["left_bins"])
    max_y = max(1, *result["left_bins"], *result["right_bins"])
    hues = ((117, 229, 163), (226, 160, 151))
    for bins, hue in zip((result["left_bins"], result["right_bins"]), hues):
        points = [(x0 + (x1 - x0) * (i + .5) / count, y1 - (y1 - y0) * n / max_y) for i, n in enumerate(bins)]
        draw.line(points, fill=hue, width=3)
    draw.line((x0, y1, x1, y1), fill=(118, 144, 131), width=1)
    for intensity in (0, .25, .5, .75, 1):
        x = x0 + intensity * (x1 - x0)
        draw.line((x, y1, x, y1 + 4), fill=(132, 154, 144))
        label(draw, (min(x - 5, width - 34), y1 + 7), f"{intensity:.2f}", size=11)
    label(draw, (x0, 10), "Shared valid pixels · image brightness distribution", size=14)
    label(draw, (width - 189, 10), "First", size=13, fill=hues[0])
    label(draw, (width - 119, 10), "Second", size=13, fill=hues[1])
    return canvas


def support_poster(left, right):
    tags = left.astype(np.uint8) + 2 * right.astype(np.uint8)
    palette = np.asarray(((50, 61, 64), (119, 162, 227), (233, 162, 133), (106, 208, 160)), dtype=np.uint8)
    return Image.fromarray(palette[tags])


def add_preview_panel(base, zones, mode, width, field_y):
    left, right = zones
    slot = width - 38 if mode != "two" else width // 2 - 31
    height = 375
    if mode != "two":
        left.thumbnail((slot, height), Image.Resampling.LANCZOS)
        x = (width - left.width) // 2
        base.paste(left, (x, field_y + (height - left.height) // 2))
    else:
        for i, image in enumerate((left, right)):
            image.thumbnail((slot, height), Image.Resampling.LANCZOS)
            x = width * i // 2 + (width // 2 - image.width) // 2
            base.paste(image, (x, field_y + (height - image.height) // 2))


def camera_comparison_picture(title, images, settings, stats):
    width, height = 980, 660
    base = Image.new("RGB", (width, height), (21, 32, 34))
    drawer = ImageDraw.Draw(base)
    label(drawer, (24, 17), title, size=22)
    label(drawer, (24, 54), "First image" if settings.view == "two" else "Alternating image evidence", size=15)
    if settings.view == "two":
        label(drawer, (width // 2 + 25, 54), "Second image", size=15)
    add_preview_panel(base, images, settings.view, width, 82)
    base.paste(histogram_chart(stats, width=width), (0, 468))
    return base


def compare_photos(store, target, form, *, save=True):
    settings = PairSettings.parse(form)
    reference = identifier(form.get("reference"), "reference")
    if target == reference:
        raise SurveyError("Select a different camera photo.")
    with store.use() as db:
        frame_a, frame_b = db.documents("frames", [reference, target])
        if frame_a["extent"] != frame_b["extent"]:
            raise SurveyError("Use photos of the same orientation and image dimensions.", field="reference")
        if frame_a["station_id"] != frame_b["station_id"]:
            raise SurveyError("Choose two frames from the same station.", field="reference")
        images = [
            camera_extract(fetch_work(db, key), db.matching("marks", where=(("frame_id", "=", key),)),
                           settings.box, settings.use_exclusions)
            for key in (reference, target)
        ]
    arrays = [item[1] for item in images]
    visibility = [item[2] for item in images]
    limits = shared_window(arrays, visibility)
    report = paired_histograms(*arrays, *visibility)
    report.update(window=images[0][3], limits=list(limits), excluded_counts=[item[4] for item in images],
                  illumination=[frame_a["illumination"], frame_b["illumination"]])
    if report["support"]["joint"] < 8:
        raise SurveyError("Fewer than eight pixels are visible in both source windows.", field="box")
    if not save:
        return report
    panels = [visible_layer(image, values, valid, limits, settings)
              for image, values, valid, _, _ in images]
    if settings.view != "two":
        panels[0] = merge_comparison(*panels, settings.view, settings.stripe)
    title = word(form.get("name"), "comparison name", default="Camera window comparison")
    board = camera_comparison_picture(title, panels, settings, report)
    support = support_poster(*visibility)
    with store.use(write=True) as db:
        record = db.append("glances", {
            "frame_id": target, "reference_id": reference, "name": title, "kind": "window-comparison",
            "settings": settings.json(), "report": report,
            "sources": [{"id": frame["id"], "digest": frame["digest"]} for frame in (frame_a, frame_b)],
        }, action="comparison window saved")
        db.add_asset(record["id"], "guide", image_bytes(board), "image/png")
        db.add_asset(record["id"], "support", image_bytes(support), "image/png")
        return record


def blur_comparison(store, frame, form):
    request = PairSettings.parse(form)
    with store.use() as db:
        image = fetch_work(db, frame)
        marks = db.matching("marks", where=(("frame_id", "=", frame),))
    crop, luma, valid, extent, omitted = camera_extract(image, marks, request.box, request.use_exclusions)
    if int(valid.sum()) < 36:
        raise SurveyError("Not enough illuminated samples are available.", field="box")
    low, high = shared_window([luma], [valid])
    values = np.where(valid, luma, 0)
    sizes = (1.0, 2.5, 5.0)
    detail = []
    for radius in sizes:
        weight = ndimage.gaussian_filter(valid.astype(np.float32), radius)
        base = ndimage.gaussian_filter(values, radius)
        reference_subtracted = luma - np.divide(base, weight, out=np.zeros_like(values), where=weight > .8)
        active = valid & (weight > .8)
        if np.count_nonzero(active) < 8:
            continue
        measurement = np.abs(reference_subtracted[active])
        detail.append({"sigma_px": radius, "support": int(active.sum()), "detail": percentiles(measurement)})
    return {"levels": [low, high], "extent": extent, "excluded_pixels": omitted, "distinct_detail": detail}
