"""Original procedural woodland camera imagery for the labelled synthetic survey."""

from io import BytesIO
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

from .images import image_bytes

DEMO_DIMENSIONS = (1000, 648)
DEMO_GREEN = (80, 83, 55)


def fractal_field(rng, dims, scales):
    height, width = dims
    image = np.zeros(dims, dtype=np.float32)
    for scale, strength in scales:
        panel = rng.uniform(-1, 1, (max(3, int(height / scale)), max(3, int(width / scale)))).astype(np.float32)
        expanded = Image.fromarray(panel).resize((width, height), Image.Resampling.BICUBIC)
        image += np.asarray(expanded) * strength
    return image


def stage_mask(x, y, left, right):
    z = 0.5 * (np.tanh((x - left) / 18) - np.tanh((x - right) / 18))
    near = np.clip((y - x * 0.04 - 282) / 45, 0, 1)
    return z * near


def forest_layer(rng, view):
    width, height = DEMO_DIMENSIONS
    y, x = np.indices((height, width), dtype=np.float32)
    haze = np.clip(1 - ((x - width * 0.63) ** 2 + (y - height * 0.35) ** 2) / 360000, 0, 1)
    turbulence = fractal_field(rng, (height, width), ((105, 24), (19, 10), (6, 7), (2, 5)))
    luminance = 25 + 39 * haze + turbulence
    if view == "ridge":
        left = 620 - y * 0.74 + 29 * np.sin(y / 125)
        right = 650 + y * 0.47
    else:
        left = 350 - y * 0.51 + 28 * np.sin(y / 128)
        right = 347 + y * 0.62
    openness = stage_mask(x, y, left, right)
    wall = np.clip(y / height + turbulence / 40, 0, 1)
    brightness = luminance + openness * (44 + wall * 24)
    channels = np.stack((brightness * (0.77 + .55 * openness), brightness * (0.98 + openness * .11),
                         brightness * (0.63 + .27 * openness)), axis=-1)
    return np.uint8(np.clip(channels, 0, 255))


def depth_trees(draw, rng, *, count, stage):
    width, height = DEMO_DIMENSIONS
    for _ in range(count):
        x = int(rng.uniform(-30, width + 40))
        base = int(rng.uniform(182, 344) if stage == "far" else rng.uniform(298, height - 10))
        if stage == "near" and 260 < x < 760 and base > 450:
            continue
        diameter = max(2, int(rng.normal(7 if stage == "far" else 22, 4)))
        lean = int(rng.uniform(-35, 36))
        dark = int(rng.uniform(20, 48) if stage == "far" else rng.uniform(24, 72))
        colour = (dark + 6, dark + 3, max(12, dark - 9))
        draw.polygon([(x - lean - diameter // 4, -30), (x - lean + diameter // 2, -12),
                      (x + diameter, base), (x - diameter // 6, base) ], fill=colour)
        draw.line((x - lean, 0, x + diameter // 3, base - 8), fill=(dark + 11, dark + 8, dark), width=1)
        for a in range(1, 4):
            length = int(rng.uniform(20, 85))
            y = int(rng.uniform(38, base - 15))
            stem = x - lean + lean * y / base
            side = -1 if a % 2 == 0 else 1
            draw.line((stem, y, stem + side * length * 0.46, y - length * 0.24,
                       stem + side * length, y - length), fill=colour, width=max(1, diameter // 9))


def leaf_texture(draw, rng, *, foreground):
    width, height = DEMO_DIMENSIONS
    for _ in range(2800 if foreground else 1500):
        x = int(rng.uniform(-8, width + 8))
        if foreground:
            if 165 < x < 840 and rng.random() > 0.16:
                continue
            y = int(rng.uniform(330, height + 10))
        else:
            y = int(rng.beta(1.4, 2.1) * 415)
        scale = 1 + (y / height) ** 2 * 11
        dx = (rng.random() + .28) * scale * (1 if rng.random() < .5 else -1)
        dy = (rng.random() + .3) * scale
        v = int(rng.uniform(22, 90) + y / 12)
        color = (int(v * .82), v, int(v * .56))
        if foreground and rng.random() > .66:
            color = (v + 13, v + 6, int(v * .64))
        draw.line((x, y, x + dx, y - dy), fill=color, width=1 if dy < 7 else 2)


def stones(draw, rng, view):
    for i in range(210):
        y = float(rng.uniform(330, 640))
        centre = 510 if view == "ridge" else 411
        x = float(rng.normal(centre, 38 + (y - 325) / 2.9))
        size = (0.15 + rng.random()) * (2 + y * .012)
        tone = int(rng.uniform(62, 135))
        draw.ellipse((x, y, x + size * 1.4, y + size * .65), fill=(tone + 7, tone + 2, tone - 10))
        if i % 4 == 0:
            draw.arc((x, y, x + size * 1.45, y + size * .68), 0, 180,
                     fill=(tone // 2, tone // 2 + 1, tone // 2), width=1)


def woodland(view, seed):
    rng = np.random.default_rng(seed)
    bg = Image.fromarray(forest_layer(rng, view), "RGB")
    drawer = ImageDraw.Draw(bg)
    depth_trees(drawer, rng, count=44, stage="far")
    leaf_texture(drawer, rng, foreground=False)
    depth_trees(drawer, rng, count=22, stage="near")
    stones(drawer, rng, view)
    leaf_texture(drawer, rng, foreground=True)
    return bg.filter(ImageFilter.GaussianBlur(0.48))


def animal_outline():
    return [
        (9, 45), (36, 48), (57, 34), (76, 31), (86, 29), (101, 29), (126, 27),
        (140, 17), (145, 7), (149, 25), (155, 20), (159, 27), (171, 32), (178, 36),
        (173, 42), (158, 44), (148, 49), (146, 62), (157, 80), (154, 88), (143, 84),
        (130, 64), (120, 60), (109, 57), (97, 59), (79, 58), (72, 68), (78, 86),
        (72, 92), (65, 84), (60, 71), (59, 57), (45, 61), (28, 62), (16, 57),
    ]


def portrait_skin(rng, sample):
    width, height = 186, 100
    grid = Image.new("L", (width, height))
    draw = ImageDraw.Draw(grid)
    draw.polygon(animal_outline(), fill=255)
    mask = np.asarray(grid) > 0
    yy, xx = np.indices(mask.shape)
    fur = fractal_field(rng, mask.shape, ((14, 10), (4, 12), (1, 12)))
    body = 37 + 99 * np.exp(-((xx - 89) ** 2 / 14500 + (yy - 40) ** 2 / 1850)) + fur
    streak = 1 - np.clip(np.exp(-.18 * (yy - 49 - 7 * np.sin(xx / 42)) ** 2), 0, 1) * 0.37
    chest = (xx >= 125) & (yy > 40) & (yy < 50)
    body = (body * streak) * np.where(chest, 1.25, 1.0)
    warm = (1.14, .89, .66) if sample == "fox" else (1.08, .98, .85)
    channels = np.stack([body * factor for factor in warm], axis=-1)
    alpha = np.uint8(mask) * 255
    image = Image.fromarray(np.uint8(np.clip(channels, 0, 255)), "RGB").convert("RGBA")
    image.putalpha(Image.fromarray(alpha))
    base = ImageDraw.Draw(image)
    base.ellipse((157, 31, 162, 35), fill=(51, 54, 44))
    base.point((159, 32), fill=(208, 214, 124))
    base.ellipse((174, 37, 178, 40), fill=(48, 50, 43))
    return image


def observe_animal(bg, rng, pos, sample):
    x, y, size, flip = pos
    subject = portrait_skin(rng, sample)
    if flip:
        subject = subject.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    subject = subject.resize((int(size), round(size * 100 / 186)), Image.Resampling.LANCZOS)
    shade = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    lower = y + subject.height * .91
    dt = ImageDraw.Draw(shade)
    dt.ellipse((x + size * .14, lower - 2, x + size * .92, lower + subject.height * .16), fill=(23, 27, 20, 110))
    cast = shade.filter(ImageFilter.GaussianBlur(10))
    result = Image.alpha_composite(bg.convert("RGBA"), cast)
    result.alpha_composite(subject, (int(x), int(y)))
    return result.convert("RGB")


def camera_processing(image, rng, light, dirty=False):
    px = np.asarray(image, dtype=np.float32)
    height, width = px.shape[:2]
    y, x = np.indices((height, width), dtype=np.float32)
    vignetting = .76 - 0.24 * (x - width * .5) ** 2 / (width ** 2) - .23 * (y - height * .48) ** 2 / (height ** 2)
    if light == "night-ir":
        grey = px.mean(axis=2)
        center = np.exp(-(((x - width * .54) / (.5 * width)) ** 2 + ((y - height * .66) / (.44 * height)) ** 2))
        values = (grey * (0.73 + center * 1.01) + 8 * center) * vignetting
        grain = rng.normal(0, 2.3, (height, width))
        px = np.stack((values + grain + 1, values + grain + 2, values + grain), axis=-1)
    elif light == "dusk":
        px = px * (vignetting * .79)[..., None] + (8, 11, 16)
    else:
        contrast = np.maximum(1, px / 255)
        grain = rng.normal(0, 1.2, (height, width))
        px = px * (.91 + .14 * contrast) + grain[..., None]
    if dirty:
        veiling = np.clip(1 - ((x - 126) ** 2 + (y - 338) ** 2) / 33000, 0, 1)
        px = px + veiling[..., None] * 95
    return Image.fromarray(np.uint8(np.clip(px, 0, 255)))


def demo_image(background, *, seed, light, at, position=None, flagged=False, missing=False):
    rng = np.random.default_rng(seed)
    photo = background.copy() if position is None else observe_animal(background, rng, position, "fox")
    view = camera_processing(photo, rng, light, dirty=flagged)
    if missing:
        view = view.convert("RGBA")
        alpha = Image.new("L", view.size, 255)
        draw = ImageDraw.Draw(alpha)
        draw.rectangle((771, 391, 995, 463), fill=0)
        view.putalpha(alpha)
        return image_bytes(view)
    stage = Image.Exif()
    stage[271] = "FieldTrace synthetic"
    stage[272] = "Procedural camera"
    stage[306] = at.strftime("%Y:%m:%d %H:%M:%S")
    stage[36880] = "+00:00"
    return image_bytes(view, "JPEG", quality=91, exif=stage)
