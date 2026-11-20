"""Pixel-supported cross sections and spatial readings on a camera photograph."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .contracts import SurveyError, integer, list_value, number, unit_point, word
from .images import fetch_work, image_bytes, intensities, label, mark_union, pixel_units, show_support

TRACE_COLOR = (150, 244, 183)
MISSING_COLOR = (234, 150, 108)
GRID_COLOR = (80, 102, 97)
LINE_COLOR = (143, 224, 191)
TEXT_COLOR = (230, 238, 224)


@dataclass(frozen=True)
class LineSettings:
    points: tuple
    interval_px: float
    stripe_width: float
    minimum_coverage: float
    keep_channels: str

    @classmethod
    def parse(cls, value):
        defaults = {"luminance", "rgb"}
        mode = value.get("channels", "luminance")
        if mode not in defaults:
            raise SurveyError("Choose luminance or RGB profile readings.", field="channels")
        pts = [unit_point(row, "points") for row in list_value(value.get("points"), "points", min_items=2, max_items=25)]
        if any(math.dist(a, b) < 0.00001 for a, b in zip(pts, pts[1:])):
            raise SurveyError("Duplicate profile points do not define a direction.", field="points")
        return cls(
            tuple(pts),
            number(value.get("interval_px"), "interval_px", 0.5, 80, default=3),
            number(value.get("stripe_width"), "stripe_width", 1, 80, default=5),
            number(value.get("minimum_coverage"), "minimum_coverage", 0.1, 1, default=0.7),
            mode,
        )

    def json(self):
        return {
            "points": self.points, "interval_px": self.interval_px, "stripe_width": self.stripe_width,
            "minimum_coverage": self.minimum_coverage, "channels": self.keep_channels,
        }


def profile_stations(pts, interval):
    pts = np.asarray(pts, dtype=float)
    vec = np.diff(pts, axis=0)
    lengths = np.linalg.norm(vec, axis=1)
    if np.any(lengths < 0.01):
        raise SurveyError("The drawn points are too close at this photo resolution.", field="points")
    breaks = np.r_[0, np.cumsum(lengths)]
    total = float(breaks[-1])
    count = math.ceil(total / interval) + 1
    if count > 1200:
        raise SurveyError("Increase the station interval for this long profile.", field="interval_px")
    at = np.linspace(0, total, int(max(2, count)))
    k = np.clip(np.searchsorted(breaks, at, side="right") - 1, 0, len(lengths) - 1)
    arc = at - breaks[k]
    tangent = vec[k] / lengths[k, None]
    positions = pts[k] + tangent * arc[:, None]
    normal = np.column_stack((-tangent[:, 1], tangent[:, 0]))
    return at, positions, normal, breaks


def profile_sampling(values, valid, centers, normal, width):
    samples = max(1, int(round(width)))
    axes = np.linspace(-(width - 1) / 2, (width - 1) / 2, samples)
    positions = centers[:, None, :] + normal[:, None, :] * axes[None, :, None]
    coords = [positions[..., 1].ravel(), positions[..., 0].ravel()]
    flat_valid = ndimage.map_coordinates(valid.astype(np.float32), coords, order=1, mode="constant", cval=0)
    weights = flat_valid.reshape(len(centers), samples)
    observed = weights > 0.999
    bands = []
    for values_ in values:
        raw = ndimage.map_coordinates(np.where(valid, values_, 0), coords, order=1, mode="constant", cval=0)
        bands.append(raw.reshape(len(centers), samples))
    return np.stack(bands, axis=0), observed


def summarize_sample(row, support, minimum):
    fraction = float(np.mean(support))
    result = {"fraction": fraction, "count": int(support.sum()), "supported": fraction >= minimum}
    if result["supported"]:
        values = row[:, support]
        result["median"] = [float(x) for x in np.median(values, axis=1)]
        result["minimum"] = [float(x) for x in np.min(values, axis=1)]
        result["maximum"] = [float(x) for x in np.max(values, axis=1)]
    else:
        result.update(median=None, minimum=None, maximum=None)
    return result


def measurement_signal(photo, rgb):
    values, visible = intensities(photo)
    if rgb:
        channels = np.asarray(photo.convert("RGB"), dtype=np.float32).transpose(2, 0, 1) / 255
    else:
        channels = values[None, ...]
    return channels, visible


def section_report(photo, exclusions, settings):
    vector = pixel_units(settings.points, photo.size)
    arc, centers, normal, joints = profile_stations(vector, settings.interval_px)
    values, visible = measurement_signal(photo, settings.keep_channels == "rgb")
    samples, support = profile_sampling(values, visible & ~exclusions, centers, normal, settings.stripe_width)
    results = []
    for i, (length, point) in enumerate(zip(arc, centers), 1):
        found = summarize_sample(samples[:, i - 1, :], support[i - 1], settings.minimum_coverage)
        results.append(found | {"index": i, "station_px": float(length), "position_px": point.tolist()})
    counts = sum(int(row["supported"]) for row in results)
    return {
        "station_count": len(results), "supported": counts,
        "length_px": float(arc[-1]), "joints_px": joints.tolist(), "samples": results,
        "mode": settings.keep_channels, "support_fraction": counts / len(results),
        "dimensions": photo.size,
    }


def section_picture(photo, report, points):
    image = show_support(photo)
    image.thumbnail((950, 670), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image)
    path = pixel_units(points, image.size)
    draw.line(path, width=3, fill=TRACE_COLOR, joint="curve")
    source_width, source_height = report["dimensions"]
    stride = max(1, report["station_count"] // 120)
    for reading in report["samples"][::stride]:
        x, y = reading["position_px"]
        cx, cy = x * image.width / source_width, y * image.height / source_height
        fill = TRACE_COLOR if reading["supported"] else MISSING_COLOR
        draw.ellipse((cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5), fill=fill)
    label(draw, (12, 12), "Camera profile · marked path", size=14)
    return image


def section_chart(report, width=950, height=250):
    canvas = Image.new("RGB", (width, height), (21, 32, 34))
    draw = ImageDraw.Draw(canvas)
    corners = (48, 36, width - 28, height - 47)
    x0, y0, x1, y1 = corners
    for value in (0.0, 0.25, 0.5, 0.75, 1.0):
        axis_y = y1 - (y1 - y0) * value
        draw.line((x0, axis_y, x1, axis_y), fill=GRID_COLOR)
        label(draw, (11, int(axis_y) - 8), str(value), size=11)
    last = report["length_px"]
    items = report["samples"]
    for k, color in enumerate([LINE_COLOR] if report["mode"] == "luminance" else [
            (244, 129, 110), (123, 216, 152), (120, 180, 255)
    ]):
        previous = None
        for item in items:
            x = x0 + (x1 - x0) * item["station_px"] / max(0.01, last)
            reading = item["median"]
            if reading is None:
                if k == 0:
                    draw.line((x, y1 + 6, x, y1 + 11), fill=MISSING_COLOR, width=2)
                previous = None
                continue
            y = y1 - (y1 - y0) * reading[k]
            if previous is not None:
                draw.line((*previous, x, y), fill=color, width=2)
            previous = (x, y)
    label(draw, (x0, height - 27), "0 px", size=12)
    label(draw, (x1 - 80, height - 27), f"{last:.1f} px", size=12)
    label(draw, (x0, 10), f"Supported sampling, {report['supported']} / {report['station_count']}", size=13)
    return canvas


def board(photo, result, settings):
    view = section_picture(photo, result, settings.points)
    chart = section_chart(result)
    width = max(view.width, chart.width)
    image = Image.new("RGB", (width, view.height + chart.height + 13), (21, 32, 34))
    image.paste(view, ((width - view.width) // 2, 0))
    image.paste(chart, ((width - chart.width) // 2, view.height + 13))
    return image


def save_profile(store, frame_key, form):
    settings = LineSettings.parse(form)
    with store.use() as db:
        frame = db.one("frames", frame_key)
        marks = db.matching("marks", where=(("frame_id", "=", frame_key),))
        photo = fetch_work(db, frame_key)
    excluded = mark_union(photo.size, marks, {"ignore", "occlusion"})
    result = section_report(photo, excluded, settings)
    bitmap = board(photo, result, settings)
    with store.use(write=True) as db:
        report = db.append("glances", {
            "frame_id": frame_key, "name": word(form.get("name"), "profile name", default="Camera profile"),
            "kind": "profile", "settings": settings.json(), "report": result,
            "mask_versions": {m["id"]: m["version"] for m in marks}, "source_digest": frame["digest"],
        }, action="image profile measured")
        db.add_asset(report["id"], "guide", image_bytes(bitmap), "image/png")
    return report


def patch_summary(values, footprint, candidate):
    points = [(i, values[i][candidate]) for i in range(len(values))]
    quantiles = (0.1, 0.5, 0.9)
    attributes = [np.quantile(sample, quantiles).tolist() for _, sample in points if len(sample)]
    return {
        "channels": attributes,
        "selected_pixels": int(footprint.sum()),
        "observed_pixels": int(candidate.sum()),
        "observed_fraction": float(np.count_nonzero(candidate) / max(1, np.count_nonzero(footprint))),
    }


def patch_inspection(store, frame_key, form):
    from .geometry import simple_polygon
    from .contracts import polygon
    from .images import polygon_mask
    shape = simple_polygon(polygon(form.get("points"), "points"))
    with store.use() as db:
        frame = db.one("frames", frame_key)
        image = fetch_work(db, frame_key)
        masks = db.matching("marks", where=(("frame_id", "=", frame_key),))
    support = polygon_mask(image.size, shape)
    values, valid = measurement_signal(image, True)
    excluded = mark_union(image.size, masks, {"ignore", "occlusion"})
    domains = support & valid & ~excluded
    record = patch_summary(values, support, domains)
    if record["observed_pixels"] < 8:
        raise SurveyError("Choose a patch with at least eight visible pixels.", field="points")
    record.update(frame_id=frame["id"], extent=frame["extent"], polygon=shape,
                  low_high_quantiles=[0.1, 0.5, 0.9], mode="RGB display levels")
    return record
