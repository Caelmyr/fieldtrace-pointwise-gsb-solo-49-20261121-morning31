"""Decode, orient and render bounded camera uploads and survey views."""

from dataclasses import dataclass
from io import BytesIO
import hashlib
import warnings
from pathlib import PurePath

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from scipy import ndimage

from .clock import exif_fields
from .config import IMAGE_FORMATS, MAX_FRAME_EDGE, MAX_IMAGE_BYTES, MAX_PARSED_PIXELS, MAX_THUMB_EDGE
from .contracts import Limit, SurveyError

Image.MAX_IMAGE_PIXELS = MAX_PARSED_PIXELS
GRAY = (34, 39, 42)
SAFE_FILENAME = {"\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"}
MARK_COLORS = {
    "animal": (109, 224, 156),
    "identity": (242, 183, 104),
    "occlusion": (247, 112, 109),
    "ignore": (205, 168, 246),
    "restricted": (250, 121, 205),
    "fixture": (70, 210, 226),
}


@dataclass(frozen=True)
class Decoded:
    original: bytes
    mime: str
    metadata: dict
    original_extent: tuple
    image: Image.Image
    valid: np.ndarray
    depth: int
    kind: str

    @property
    def extent(self):
        return self.image.size

    @property
    def digest(self):
        return hashlib.sha256(self.original).hexdigest()


def safe_filename(raw):
    raw = raw.replace("\\", "/") if isinstance(raw, str) else ""
    candidate = PurePath(raw).name
    clean = "".join(c for c in candidate if c.isprintable() and c not in SAFE_FILENAME)
    return clean.strip()[:140] or "camera-frame"


def read_upload(upload):
    if upload is None or not upload.filename:
        raise SurveyError("Choose a camera image for import.", field="images")
    data = upload.stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise Limit("An input image exceeds 16 MiB.")
    if not data:
        raise SurveyError("The selected input is empty.", field="images")
    return safe_filename(upload.filename), data


def profile_rgb(image):
    profile = image.info.get("icc_profile")
    if not profile or image.mode not in {"RGB", "CMYK", "LAB", "L", "RGBA"}:
        return image.convert("RGB"), None
    try:
        raw_profile = ImageCms.ImageCmsProfile(BytesIO(profile))
        work = image.convert("RGB") if image.mode == "RGBA" else image
        result = ImageCms.profileToProfile(work, raw_profile, ImageCms.createProfile("sRGB"),
                                           outputMode="RGB")
        return result, "Converted embedded colour profile to sRGB"
    except (OSError, ValueError, TypeError, ImageCms.PyCMSError):
        return image.convert("RGB"), "Unusable embedded colour profile, using standard preview"


def unsigned_luminance(image):
    values = np.asarray(image)
    if values.ndim != 2:
        raise SurveyError("The single channel input has an inconsistent layout.", field="images")
    if int(values.min()) < 0 or int(values.max()) > 65535:
        raise SurveyError("Use a single unsigned luminance channel from 0 to 65,535.", field="images")
    converted = np.rint(values.astype(np.float32) * (255 / 65535)).astype(np.uint8)
    return Image.fromarray(converted).convert("RGB")


def appearance(oriented):
    if oriented.mode in {"I;16", "I;16L", "I;16B", "I"}:
        return unsigned_luminance(oriented), np.ones(oriented.size[::-1], dtype=bool), 16, "grayscale", None
    if oriented.mode == "F":
        raise SurveyError("Camera inputs require bounded unsigned image samples.", field="images")
    alpha = "A" in oriented.getbands() or "transparency" in oriented.info
    if alpha:
        container = oriented.convert("RGBA")
        rgb, profile = profile_rgb(container)
        valid = np.asarray(container.getchannel("A")) >= 128
    else:
        rgb, profile = profile_rgb(oriented)
        valid = np.ones(oriented.size[::-1], dtype=bool)
    kind = "grayscale" if oriented.mode in {"1", "L", "LA"} else "rgb"
    return rgb, valid, 8, kind, profile


def resize_extent(extent, edge):
    width, height = extent
    ratio = min(1, edge / max(width, height))
    return max(1, int(round(width * ratio))), max(1, int(round(height * ratio)))


def resize_supported(rgb, support, extent):
    if rgb.size == extent:
        return rgb, support
    alpha = Image.fromarray((support * 255).astype(np.uint8), mode="L")
    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)
    result = rgba.resize(extent, Image.Resampling.LANCZOS)
    mask = np.asarray(result.getchannel("A")) >= 128
    return result.convert("RGB"), mask


def decode(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise Limit("The input image exceeds 16 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as photo:
                media_type = IMAGE_FORMATS.get(photo.format)
                if not media_type:
                    raise SurveyError("Use a JPEG, PNG, TIFF or WebP camera image.", field="images")
                if getattr(photo, "n_frames", 1) != 1:
                    raise SurveyError("Import individual camera photos, one frame per file.", field="images")
                if photo.width < 16 or photo.height < 16:
                    raise SurveyError("The camera image is smaller than 16 pixels on one side.", field="images")
                if photo.width * photo.height > MAX_PARSED_PIXELS:
                    raise Limit("The decoded image exceeds 32 million pixels.")
                metadata = exif_fields(photo)
                oriented = ImageOps.exif_transpose(photo)
                original = oriented.size
                rgb, valid, depth, kind, colour_note = appearance(oriented)
                working = resize_extent(original, MAX_FRAME_EDGE)
                rgb, valid = resize_supported(rgb, valid, working)
                if colour_note:
                    metadata["display_profile"] = colour_note
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise Limit("The decoded image exceeds 32 million pixels.") from exc
    except (UnidentifiedImageError, ValueError, OSError, SyntaxError) as exc:
        if isinstance(exc, SurveyError):
            raise
        raise SurveyError("The file does not contain a complete supported image.", field="images") from exc
    if not np.any(valid):
        raise SurveyError("The camera image has no visible pixels.", field="images")
    return Decoded(data, media_type, metadata, original, rgb, valid, depth, kind)


def image_bytes(image, format="PNG", **params):
    stream = BytesIO()
    image.save(stream, format=format, **params)
    return stream.getvalue()


def working_image(photo):
    image = photo.image.convert("RGBA")
    image.putalpha(Image.fromarray(photo.valid.astype(np.uint8) * 255))
    return image


def thumbnail(photo):
    image = working_image(photo)
    image.thumbnail((MAX_THUMB_EDGE, MAX_THUMB_EDGE), Image.Resampling.LANCZOS)
    backdrop = Image.new("RGB", image.size, GRAY)
    backdrop.paste(image.convert("RGB"), mask=image.getchannel("A"))
    return image_bytes(backdrop, "JPEG", quality=84, optimize=True)


def frame_assets(access, frame, photo):
    key = frame["id"]
    access.add_asset(key, "source", photo.original, photo.mime)
    access.add_asset(key, "work", image_bytes(working_image(photo)), "image/png")
    access.add_asset(key, "thumbnail", thumbnail(photo), "image/jpeg")


def fetch_work(access, key):
    asset = access.asset(key, "work")
    data = access.blobs.read(asset["digest"])
    with Image.open(BytesIO(data)) as image:
        return image.convert("RGBA")


def intensities(image):
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32) / 255
    luminance = rgba[..., 0] * 0.2126 + rgba[..., 1] * 0.7152 + rgba[..., 2] * 0.0722
    return luminance, rgba[..., 3] >= 0.5


def readonly_array(image):
    return np.asarray(image.convert("RGBA"), dtype=np.uint8)


def pixel_units(points, extent):
    width, height = extent
    return [(float(x) * (width - 1), float(y) * (height - 1)) for x, y in points]


def normalized_crop(box, extent):
    x, y, width, height = box
    left = int(np.floor(x * extent[0]))
    top = int(np.floor(y * extent[1]))
    right = int(np.ceil((x + width) * extent[0]))
    bottom = int(np.ceil((y + height) * extent[1]))
    return left, top, min(extent[0], right), min(extent[1], bottom)


def polygon_mask(extent, points):
    grid = Image.new("L", extent)
    ImageDraw.Draw(grid).polygon(pixel_units(points, extent), fill=255)
    return np.asarray(grid) > 0


def mark_union(extent, marks, kinds):
    chosen = np.zeros(extent[::-1], dtype=bool)
    for mark in marks:
        if mark["kind"] in kinds:
            chosen |= polygon_mask(extent, mark["polygon"])
    return chosen


def background(extent, tile=10):
    x, y = np.meshgrid(np.arange(extent[0]), np.arange(extent[1]))
    cells = (x // tile + y // tile) % 2
    pixels = np.where(cells[..., None] > 0, (38, 44, 47), (53, 62, 65)).astype(np.uint8)
    return Image.fromarray(pixels, mode="RGB")


def show_support(image, support=None):
    canvas = background(image.size)
    if support is None and "A" in image.getbands():
        alpha = image.getchannel("A")
    elif support is not None:
        alpha = Image.fromarray(support.astype(np.uint8) * 255)
    else:
        alpha = None
    canvas.paste(image.convert("RGB"), mask=alpha)
    return canvas


def font(size=14):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def label(draw, position, text, *, size=13, fill=(240, 242, 234), background=(14, 24, 24)):
    typography = font(size)
    x, y = position
    left, top, right, bottom = draw.textbbox((x, y), str(text), font=typography)
    draw.rounded_rectangle((left - 4, top - 2, right + 4, bottom + 2), radius=3, fill=background)
    draw.text((x, y), str(text), font=typography, fill=fill)


def marked_preview(image, marks, edge=None):
    overlay = show_support(image)
    if edge:
        overlay.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(overlay)
    for mark in marks:
        poly = pixel_units(mark["polygon"], overlay.size)
        colour = MARK_COLORS.get(mark["kind"], (243, 239, 228))
        draw.line(poly + [poly[0]], fill=colour, width=max(2, overlay.width // 415), joint="curve")
        anchor = min(poly, key=lambda point: (point[1], point[0]))
        x = min(overlay.width * 0.76, max(4, anchor[0] + 5))
        y = max(4, anchor[1] + 4)
        label(draw, (x, y), mark["name"][:36], fill=colour, size=max(11, overlay.width // 72))
    return overlay


def display_tone(values, active, low, high, gamma):
    if high <= low:
        return np.zeros_like(values, dtype=np.uint8)
    scaled = np.zeros(values.shape, dtype=np.float32)
    scaled[active] = np.clip((values[active] - low) / (high - low), 0, 1) ** (1 / gamma)
    return np.rint(scaled * 255).astype(np.uint8)


def quality_edges(values, active):
    support = ndimage.binary_erosion(active, iterations=1)
    dx = ndimage.sobel(values, axis=1, mode="nearest") / 8
    dy = ndimage.sobel(values, axis=0, mode="nearest") / 8
    return np.hypot(dx, dy), support


def support_geometry(active):
    rows, columns = np.nonzero(active)
    if not len(rows):
        return {"count": 0, "fraction": 0, "bounds": None}
    left, top = int(columns.min()), int(rows.min())
    right, bottom = int(columns.max() + 1), int(rows.max() + 1)
    return {"count": len(rows), "fraction": len(rows) / active.size,
            "bounds": [left, top, right - left, bottom - top]}
