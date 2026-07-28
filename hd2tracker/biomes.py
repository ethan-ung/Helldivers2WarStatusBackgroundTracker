"""Biome backdrops.

The API gives a biome name and a prose description but no imagery whatsoever, so
we synthesise a stylised landscape from a per-biome palette: gradient sky, a
celestial body, three layers of ridgeline, horizon haze, film grain and a
vignette. Everything is seeded from the biome name, so a given biome always
renders identically.

Drop an image at ``assets/biomes/<Biome Name>.jpg`` (or .png/.webp) to override
the generated art for that biome.

Deliberately pure Pillow - NumPy is not installed, and per-pixel Python loops are
avoided by building gradients as 1-pixel strips and letting Pillow's C resize do
the work.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import config

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]

OVERRIDE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


class Palette:
    __slots__ = ("sky_top", "sky_bottom", "haze", "far", "mid", "near", "glow", "stars")

    def __init__(
        self,
        sky_top: RGB,
        sky_bottom: RGB,
        haze: RGB,
        far: RGB,
        mid: RGB,
        near: RGB,
        glow: RGB | None = None,
        stars: bool = False,
    ):
        self.sky_top = sky_top
        self.sky_bottom = sky_bottom
        self.haze = haze
        self.far = far
        self.mid = mid
        self.near = near
        self.glow = glow
        self.stars = stars


# All 25 biome names observed across the 271 planets in the live galaxy.
PALETTES: dict[str, Palette] = {
    "Acidic Badlands": Palette((38, 46, 24), (108, 118, 46), (140, 150, 70), (62, 68, 32), (44, 48, 24), (26, 28, 15), glow=(190, 205, 90)),
    "Basic Swamp": Palette((28, 40, 38), (86, 104, 84), (112, 130, 106), (44, 58, 50), (30, 42, 36), (18, 26, 22)),
    "Boneyard": Palette((44, 40, 46), (146, 134, 122), (168, 156, 142), (80, 74, 70), (56, 52, 50), (32, 30, 30)),
    "Cyberstan Megafactory": Palette((18, 20, 28), (58, 66, 84), (78, 92, 116), (40, 46, 60), (28, 32, 42), (16, 18, 24), glow=(120, 190, 235)),
    "Deadlands": Palette((48, 34, 30), (140, 96, 70), (162, 118, 88), (74, 54, 44), (52, 38, 32), (30, 22, 19)),
    "Deciduous Autumn Forest": Palette((52, 38, 30), (176, 118, 62), (198, 142, 80), (92, 58, 34), (62, 40, 26), (34, 23, 16), glow=(240, 178, 96)),
    "Deciduous Forest": Palette((36, 50, 40), (118, 148, 104), (140, 168, 122), (54, 76, 54), (38, 54, 38), (22, 32, 23)),
    "Desert Cliffs": Palette((70, 48, 34), (198, 146, 92), (214, 168, 116), (110, 70, 44), (78, 50, 32), (44, 29, 19), glow=(250, 200, 130)),
    "Desert Dunes": Palette((88, 60, 36), (224, 176, 112), (236, 198, 142), (132, 90, 52), (94, 64, 38), (54, 37, 22), glow=(255, 214, 150)),
    "Desert Oasis": Palette((72, 62, 44), (206, 182, 128), (196, 190, 150), (104, 92, 60), (72, 68, 46), (40, 40, 28), glow=(250, 226, 164)),
    "Ethereal Jungle": Palette((28, 24, 52), (96, 74, 148), (126, 100, 180), (52, 42, 88), (36, 29, 62), (20, 17, 36), glow=(178, 148, 250)),
    "Haunted Swamp": Palette((22, 26, 32), (64, 76, 78), (86, 102, 100), (34, 44, 46), (24, 31, 33), (14, 19, 20), glow=(120, 180, 160)),
    "Hive World": Palette((44, 26, 30), (154, 84, 74), (172, 106, 92), (82, 44, 42), (56, 30, 30), (32, 17, 17), glow=(226, 128, 96)),
    "Icy Glaciers": Palette((44, 62, 84), (176, 202, 224), (198, 220, 238), (86, 112, 140), (60, 82, 106), (34, 48, 64), glow=(232, 244, 255)),
    "Ionic Crimson": Palette((46, 16, 24), (168, 52, 60), (192, 78, 84), (88, 28, 34), (60, 19, 24), (34, 11, 14), glow=(248, 108, 108)),
    "Ionic Jungle": Palette((22, 38, 44), (74, 138, 140), (98, 164, 162), (38, 72, 76), (26, 50, 53), (15, 29, 31), glow=(130, 220, 214)),
    "Magma": Palette((36, 18, 16), (150, 60, 30), (188, 88, 40), (74, 32, 22), (48, 21, 15), (28, 13, 10), glow=(255, 146, 60)),
    "Moon": Palette((10, 12, 20), (36, 42, 56), (54, 62, 78), (44, 48, 58), (30, 33, 40), (17, 19, 24), glow=(214, 222, 236), stars=True),
    "Plains": Palette((48, 60, 44), (152, 168, 116), (172, 186, 138), (74, 90, 60), (52, 64, 42), (30, 37, 25), glow=(238, 232, 170)),
    "Rocky Canyons": Palette((58, 44, 40), (170, 126, 98), (188, 148, 120), (94, 66, 52), (66, 46, 37), (38, 27, 21)),
    "Scorched Moor": Palette((40, 30, 30), (128, 88, 70), (150, 108, 84), (68, 46, 40), (46, 31, 27), (26, 18, 16), glow=(226, 140, 84)),
    "Super Earth": Palette((26, 44, 78), (108, 156, 210), (140, 182, 226), (48, 76, 118), (33, 54, 86), (19, 31, 50), glow=(230, 240, 255)),
    "Supercolony": Palette((46, 40, 18), (166, 146, 52), (186, 168, 74), (86, 74, 28), (58, 50, 20), (33, 29, 12), glow=(240, 216, 96)),
    "Tundra": Palette((50, 58, 70), (162, 178, 190), (184, 198, 208), (82, 96, 110), (57, 68, 80), (33, 39, 47), glow=(226, 236, 246)),
    "Volcanic Jungle": Palette((34, 26, 24), (116, 78, 56), (146, 100, 66), (58, 44, 34), (40, 30, 24), (23, 17, 14), glow=(238, 130, 66)),
}

NEUTRAL = Palette((24, 28, 36), (78, 90, 108), (98, 112, 132), (44, 52, 64), (31, 37, 46), (18, 21, 27))

# The community and the wiki do not always use the API's exact biome names, so
# accept the common variants when matching files in assets/biomes/.
BIOME_FILE_ALIASES: dict[str, tuple[str, ...]] = {
    "Deciduous Autumn Forest": ("Autumn Forest", "Autumnal Forest", "Deciduous Autumn"),
    "Magma": ("Magma Desert", "Magma Wastes"),
    "Cyberstan Megafactory": ("Cyberstan", "Megafactory"),
    "Icy Glaciers": ("Ice Glaciers", "Glaciers"),
    "Supercolony": ("Super Colony",),
    "Acidic Badlands": ("Acid Badlands",),
}


def palette_for(biome_name: str) -> Palette:
    return PALETTES.get(biome_name, NEUTRAL)


# --------------------------------------------------------------------------- helpers


def _seed(biome_name: str) -> int:
    return int(hashlib.sha256(biome_name.encode("utf-8")).hexdigest()[:8], 16)


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _vertical_gradient(size: tuple[int, int], top: RGB, bottom: RGB) -> Image.Image:
    """Build a 1px-wide gradient strip and let Pillow stretch it in C."""
    width, height = size
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    span = max(1, height - 1)
    for y in range(height):
        pixels[0, y] = _lerp(top, bottom, y / span)
    return strip.resize(size, Image.Resampling.BILINEAR)


def _ridge_points(
    rng: random.Random, width: int, samples: int
) -> list[float]:
    """A deterministic sum-of-sines ridgeline, normalised to 0..1."""
    octaves = [
        (rng.uniform(0.6, 1.4), rng.uniform(0, math.tau), rng.uniform(0.45, 0.6)),
        (rng.uniform(1.8, 3.2), rng.uniform(0, math.tau), rng.uniform(0.18, 0.3)),
        (rng.uniform(4.0, 7.0), rng.uniform(0, math.tau), rng.uniform(0.07, 0.14)),
        (rng.uniform(9.0, 15.0), rng.uniform(0, math.tau), rng.uniform(0.02, 0.06)),
    ]
    values = []
    for i in range(samples):
        t = i / max(1, samples - 1)
        value = sum(amp * math.sin(freq * math.tau * t + phase) for freq, phase, amp in octaves)
        values.append(value)

    low, high = min(values), max(values)
    span = high - low if high > low else 1.0
    return [(v - low) / span for v in values]


def _draw_ridge(
    canvas: Image.Image,
    rng: random.Random,
    color: RGB,
    baseline: float,
    amplitude: float,
    blur: float = 0.0,
) -> None:
    width, height = canvas.size
    step = max(2, width // 480)
    samples = width // step + 2
    profile = _ridge_points(rng, width, samples)

    points = [(0, height)]
    for i, value in enumerate(profile):
        x = min(width, i * step)
        y = (baseline - value * amplitude) * height
        points.append((x, y))
    points.append((width, height))

    if blur > 0:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(points, fill=color + (255,))
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
        canvas.alpha_composite(layer) if canvas.mode == "RGBA" else canvas.paste(layer, (0, 0), layer)
    else:
        ImageDraw.Draw(canvas).polygon(points, fill=color)


def _add_glow(canvas: Image.Image, palette: Palette, rng: random.Random) -> None:
    """A soft celestial disc low in the sky."""
    if palette.glow is None:
        return
    width, height = canvas.size
    radius = int(min(width, height) * rng.uniform(0.07, 0.11))
    cx = int(width * rng.uniform(0.18, 0.55))
    cy = int(height * rng.uniform(0.24, 0.40))

    halo_radius = radius * 6
    box = halo_radius * 2
    halo = Image.new("L", (box, box), 0)
    # The ellipse must sit well inside the box: if it touches the edges, the blur
    # tail is clipped and the paste leaves a visible rectangular seam.
    inset = halo_radius * 0.45
    ImageDraw.Draw(halo).ellipse(
        (halo_radius - inset, halo_radius - inset, halo_radius + inset, halo_radius + inset),
        fill=90,
    )
    halo = halo.filter(ImageFilter.GaussianBlur(halo_radius * 0.18))

    tint = Image.new("RGB", (box, box), palette.glow)
    canvas.paste(tint, (cx - halo_radius, cy - halo_radius), halo)

    disc = Image.new("L", (radius * 4, radius * 4), 0)
    ImageDraw.Draw(disc).ellipse((radius, radius, radius * 3, radius * 3), fill=225)
    disc = disc.filter(ImageFilter.GaussianBlur(radius * 0.18))
    canvas.paste(
        Image.new("RGB", (radius * 4, radius * 4), palette.glow),
        (cx - radius * 2, cy - radius * 2),
        disc,
    )


def _add_stars(canvas: Image.Image, rng: random.Random, horizon: float) -> None:
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)
    count = int(width * height / 14000)
    for _ in range(count):
        x = rng.uniform(0, width)
        y = rng.uniform(0, height * horizon)
        brightness = rng.randint(120, 255)
        size = 1 if rng.random() < 0.85 else 2
        draw.ellipse((x, y, x + size, y + size), fill=(brightness, brightness, brightness))


def _add_haze(canvas: Image.Image, palette: Palette, horizon: float) -> None:
    """Brighten the band just above the horizon so ridges read as distant."""
    width, height = canvas.size
    strip = Image.new("L", (1, height), 0)
    pixels = strip.load()
    center = horizon * height
    falloff = max(1.0, height * 0.18)
    for y in range(height):
        distance = abs(y - center) / falloff
        pixels[0, y] = max(0, min(120, int(120 * math.exp(-distance * distance))))
    mask = strip.resize((width, height), Image.Resampling.BILINEAR)
    canvas.paste(Image.new("RGB", (width, height), palette.haze), (0, 0), mask)


def _add_grain(canvas: Image.Image, sigma: float = 7.0) -> Image.Image:
    noise = Image.effect_noise(canvas.size, sigma).convert("L")
    noise_rgb = Image.merge("RGB", (noise, noise, noise))
    return ImageChops.overlay(canvas, noise_rgb)


def _add_vignette(canvas: Image.Image, strength: int = 96) -> None:
    width, height = canvas.size
    mask = Image.radial_gradient("L").resize((width, height), Image.Resampling.BILINEAR)
    mask = mask.point(lambda v: int(v * strength / 255))
    canvas.paste(Image.new("RGB", (width, height), (0, 0, 0)), (0, 0), mask)


# --------------------------------------------------------------------------- generation


def generate(biome_name: str, size: tuple[int, int]) -> Image.Image:
    palette = palette_for(biome_name)
    rng = random.Random(_seed(biome_name or "unknown"))
    horizon = rng.uniform(0.56, 0.66)

    canvas = _vertical_gradient(size, palette.sky_top, palette.sky_bottom)

    if palette.stars:
        _add_stars(canvas, rng, horizon * 0.9)
    _add_glow(canvas, palette, rng)
    _add_haze(canvas, palette, horizon)

    _draw_ridge(canvas, rng, palette.far, horizon + 0.02, 0.10, blur=2.5)
    _draw_ridge(canvas, rng, palette.mid, horizon + 0.13, 0.13, blur=1.0)
    _draw_ridge(canvas, rng, palette.near, horizon + 0.30, 0.16)

    canvas = _add_grain(canvas)
    _add_vignette(canvas)
    return canvas


# --------------------------------------------------------------------------- overrides / cache


def _normalise(value: str) -> str:
    return value.lower().replace(" ", "").replace("_", "").replace("-", "")


def find_override(biome_name: str) -> Path | None:
    """Locate a user-supplied image for a biome.

    Matching ignores case, spaces, underscores and hyphens, and accepts the
    aliases in ``BIOME_FILE_ALIASES``.
    """
    directory = config.BIOME_OVERRIDE_DIR
    if not biome_name or not directory.is_dir():
        return None

    # Fast path: an exact filename match.
    for suffix in OVERRIDE_SUFFIXES:
        candidate = directory / f"{biome_name}{suffix}"
        if candidate.is_file():
            return candidate

    accepted = {_normalise(biome_name)}
    accepted.update(_normalise(alias) for alias in BIOME_FILE_ALIASES.get(biome_name, ()))

    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return None

    for entry in entries:
        if entry.is_file() and entry.suffix.lower() in OVERRIDE_SUFFIXES:
            if _normalise(entry.stem) in accepted:
                return entry
    return None


def cover_crop(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to cover the target box, then centre-crop the overflow."""
    target_w, target_h = size
    source_w, source_h = image.size
    if source_w <= 0 or source_h <= 0:
        return Image.new("RGB", size, (0, 0, 0))

    scale = max(target_w / source_w, target_h / source_h)
    scaled = image.resize(
        (max(target_w, round(source_w * scale)), max(target_h, round(source_h * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


def get_backdrop(biome_name: str, size: tuple[int, int]) -> Image.Image:
    """Return the backdrop for a biome, preferring a user override, then cache."""
    override = find_override(biome_name)
    if override is not None:
        try:
            with Image.open(override) as source:
                return cover_crop(source.convert("RGB"), size)
        except (OSError, ValueError) as exc:
            log.warning("could not load override %s: %s", override, exc)

    safe_name = "".join(c if c.isalnum() else "_" for c in (biome_name or "unknown"))
    cache_path = config.BIOME_CACHE_DIR / f"{safe_name}_{size[0]}x{size[1]}.png"

    if cache_path.exists():
        try:
            with Image.open(cache_path) as cached:
                return cached.convert("RGB")
        except (OSError, ValueError):
            log.debug("biome cache entry unreadable, regenerating: %s", cache_path)

    image = generate(biome_name, size)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_suffix(".tmp.png")
        image.save(temp, "PNG", optimize=False)
        temp.replace(cache_path)
    except OSError as exc:
        log.debug("could not cache biome backdrop: %s", exc)

    return image
