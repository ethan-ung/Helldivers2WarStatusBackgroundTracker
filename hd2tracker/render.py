"""Compose the wallpaper: biome backdrop, right-side scrim, vertical status panel.

Bar colour rules, per the spec:

* liberation progress and Super Earth defence progress are always Super Earth blue
* the opposing campaign bar takes the colour of the faction pressing it

Everything scales off the panel width so the same layout works on a 1080p panel
and a 1440p ultrawide.
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import biomes, config
from .history import format_rate
from .model import MajorOrder, PlanetCard, WarSnapshot

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- fonts


@lru_cache(maxsize=64)
def _load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    candidates: list[Path] = []

    if kind == "display":
        override = config.FONT_OVERRIDE_DIR
        if override.is_dir():
            candidates.extend(sorted(override.glob("*.ttf")))
            candidates.extend(sorted(override.glob("*.otf")))
        names = config.FONT_CANDIDATES_DISPLAY
    elif kind == "mono":
        names = config.FONT_CANDIDATES_MONO
    else:
        names = config.FONT_CANDIDATES_BODY

    candidates.extend(config.FONT_DIR / name for name in names)

    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except (OSError, ValueError):
            continue

    log.warning("no TrueType font found for %r; falling back to bitmap default", kind)
    return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    """Truncate with an ellipsis so long planet names cannot overflow the panel."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and draw.textlength(trimmed + ellipsis, font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ""


# --------------------------------------------------------------------------- primitives


def _panel_metrics(size: tuple[int, int]) -> tuple[int, float]:
    """Panel width and the type scale that goes with it.

    Scale follows display height so text stays legible on a tall screen; width is
    then derived from that scale and clamped against the display width so the
    panel cannot swallow a narrow desktop.
    """
    width, height = size
    scale = height / config.PANEL_REFERENCE_HEIGHT
    scale = max(config.PANEL_SCALE_MIN, min(config.PANEL_SCALE_MAX, scale))

    panel_width = int(config.PANEL_BASE_WIDTH * scale)
    panel_width = min(panel_width, int(width * config.PANEL_WIDTH_FRACTION))
    panel_width = max(config.PANEL_WIDTH_MIN, min(config.PANEL_WIDTH_MAX, panel_width))
    return panel_width, scale


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _apply_scrim(canvas: Image.Image, panel_width: int) -> None:
    """Darken the right-hand side so panel text stays legible over any backdrop."""
    width, height = canvas.size
    scrim_width = min(width, int(panel_width * config.SCRIM_WIDTH_MULTIPLIER))
    start_x = width - scrim_width

    strip = Image.new("L", (width, 1), 0)
    pixels = strip.load()
    for x in range(start_x, width):
        t = (x - start_x) / max(1, scrim_width - 1)
        pixels[x, 0] = int(config.SCRIM_MAX_ALPHA * _smoothstep(t))
    mask = strip.resize((width, height), Image.Resampling.BILINEAR)
    canvas.paste(Image.new("RGB", (width, height), (4, 6, 11)), (0, 0), mask)


def _bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    fraction: float,
    color: tuple[int, int, int],
) -> None:
    radius = min(config.BAR_RADIUS, height // 2)
    draw.rounded_rectangle((x, y, x + width, y + height), radius=radius, fill=config.TRACK_COLOR)

    filled = int(width * max(0.0, min(1.0, fraction)))
    if filled <= 0:
        return
    # Keep the cap round even at tiny fractions.
    filled = max(filled, height)
    draw.rounded_rectangle((x, y, x + filled, y + height), radius=radius, fill=color + (255,))


def _faction_color(faction: str | None) -> tuple[int, int, int]:
    if not faction:
        return config.FACTION_FALLBACK_COLOR
    return config.FACTION_COLORS.get(faction, config.FACTION_FALLBACK_COLOR)


def _faction_label(faction: str | None) -> str:
    if not faction:
        return "UNKNOWN"
    return config.FACTION_DISPLAY_NAMES.get(faction, faction.upper())


def _owner_tag(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    faction: str | None,
    font: ImageFont.FreeTypeFont,
    scale: float,
) -> int:
    """Faction-coloured ownership chip. Returns its right edge."""
    label = _faction_label(faction)
    color = _faction_color(faction)
    pad_x = max(5, int(7 * scale))
    pad_y = max(2, int(3 * scale))

    text_width = draw.textlength(label, font=font)
    ascent, descent = font.getmetrics()
    text_height = ascent + descent

    right = x + text_width + pad_x * 2
    bottom = y + text_height + pad_y * 2
    draw.rounded_rectangle(
        (x, y, right, bottom),
        radius=max(3, int(4 * scale)),
        fill=color + (46,),
        outline=color + (190,),
        width=1,
    )
    draw.text((x + pad_x, y + pad_y), label, font=font, fill=color + (255,))
    return int(right)


# --------------------------------------------------------------------------- sections


def _draw_header(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    snapshot: WarSnapshot,
    now: datetime,
    scale: float,
) -> int:
    title_font = _load_font("display", int(27 * scale))
    meta_font = _load_font("body", int(12 * scale))

    draw.text((x, y), "GALACTIC WAR", font=title_font, fill=config.TEXT_PRIMARY + (255,))
    y += int(30 * scale)

    accent_width = int(46 * scale)
    draw.rectangle((x, y, x + accent_width, y + max(2, int(2 * scale))), fill=config.SUPER_EARTH_BLUE + (255,))
    y += int(10 * scale)

    stamp = snapshot.observed_at.astimezone().strftime("%H:%M")
    left = f"{snapshot.total_players:,} HELLDIVERS"
    right = f"UPDATED {stamp}" if not snapshot.stale else f"STALE · {stamp}"
    right_color = config.WARN_RED if snapshot.stale else config.TEXT_MUTED

    draw.text((x, y), left, font=meta_font, fill=config.TEXT_SECONDARY + (255,))
    right_width = draw.textlength(right, font=meta_font)
    draw.text((x + width - right_width, y), right, font=meta_font, fill=right_color + (255,))

    return y + int(20 * scale)


def _draw_major_order(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    order: MajorOrder,
    now: datetime,
    scale: float,
) -> int:
    label_font = _load_font("body", int(11 * scale))
    title_font = _load_font("display", int(16 * scale))
    body_font = _load_font("body", int(12 * scale))

    top = y
    inner_x = x + int(config.CARD_PADDING * scale)
    inner_width = width - int(config.CARD_PADDING * scale) * 2
    cursor = y + int(config.CARD_PADDING * scale)

    draw.text((inner_x, cursor), "MAJOR ORDER", font=label_font, fill=config.ACCENT_YELLOW + (255,))
    remaining = order.time_remaining(now)
    if remaining:
        remaining_width = draw.textlength(remaining, font=label_font)
        draw.text(
            (inner_x + inner_width - remaining_width, cursor),
            remaining,
            font=label_font,
            fill=config.TEXT_MUTED + (255,),
        )
    cursor += int(15 * scale)

    draw.text(
        (inner_x, cursor),
        _fit(draw, order.title.upper(), title_font, inner_width),
        font=title_font,
        fill=config.TEXT_PRIMARY + (255,),
    )
    cursor += int(20 * scale)

    if order.decoded and order.total_count:
        summary = f"{order.completed_count} / {order.total_count} OBJECTIVES COMPLETE"
        draw.text((inner_x, cursor), summary, font=body_font, fill=config.TEXT_SECONDARY + (255,))
        cursor += int(16 * scale)

        # One slim pip per objective, filled when that objective is done.
        pip_gap = max(2, int(3 * scale))
        pip_height = max(4, int(5 * scale))
        pip_width = (inner_width - pip_gap * (order.total_count - 1)) / order.total_count
        for i, task in enumerate(order.tasks):
            px = inner_x + i * (pip_width + pip_gap)
            color = config.SUPER_EARTH_BLUE if task.complete else config.TRACK_COLOR[:3]
            draw.rounded_rectangle(
                (px, cursor, px + pip_width, cursor + pip_height),
                radius=max(1, pip_height // 2),
                fill=color + (255,),
            )
            if not task.complete and task.fraction > 0:
                draw.rounded_rectangle(
                    (px, cursor, px + max(pip_height, pip_width * task.fraction), cursor + pip_height),
                    radius=max(1, pip_height // 2),
                    fill=config.SUPER_EARTH_BLUE + (150,),
                )
        cursor += pip_height + int(6 * scale)
    else:
        fallback = order.briefing or "Objectives in progress"
        draw.text(
            (inner_x, cursor),
            _fit(draw, fallback, body_font, inner_width),
            font=body_font,
            fill=config.TEXT_SECONDARY + (255,),
        )
        cursor += int(18 * scale)

    bottom = cursor + int(config.CARD_PADDING * scale) - int(4 * scale)
    draw.rounded_rectangle(
        (x, top, x + width, bottom),
        radius=int(config.CORNER_RADIUS * scale),
        fill=None,
        outline=config.ACCENT_YELLOW + (70,),
        width=1,
    )
    return bottom


def _card_height(card: PlanetCard, scale: float) -> int:
    height = int(config.CARD_PADDING * scale) * 2
    height += int(21 * scale)  # name row
    height += int(19 * scale)  # sector + owner chip
    height += int(9 * scale)   # gap
    height += int(14 * scale) + config.BAR_HEIGHT  # primary bar row
    if card.is_timed:
        height += int(6 * scale) + int(14 * scale) + config.BAR_HEIGHT
    return height


def _draw_card(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    card: PlanetCard,
    rank: int,
    now: datetime,
    scale: float,
) -> int:
    name_font = _load_font("display", int(19 * scale))
    meta_font = _load_font("body", int(11 * scale))
    chip_font = _load_font("body", int(10 * scale))
    value_font = _load_font("mono", int(12 * scale))

    height = _card_height(card, scale)
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=int(config.CORNER_RADIUS * scale),
        fill=config.CARD_BG,
        outline=config.CARD_BORDER,
        width=1,
    )

    pad = int(config.CARD_PADDING * scale)
    inner_x = x + pad
    inner_width = width - pad * 2
    cursor = y + pad

    # A faction-coloured spine makes the owner readable at a glance.
    spine = _faction_color(card.owner)
    draw.rounded_rectangle(
        (x, y + pad, x + max(2, int(3 * scale)), y + height - pad),
        radius=1,
        fill=spine + (220,),
    )

    # Row 1: rank + planet name, player count on the right.
    players = f"{card.players:,}"
    players_width = draw.textlength(players, font=value_font)
    rank_text = f"{rank}."
    rank_width = draw.textlength(rank_text, font=meta_font)
    draw.text((inner_x + int(4 * scale), cursor + int(4 * scale)), rank_text, font=meta_font, fill=config.TEXT_MUTED + (255,))

    name_x = inner_x + int(4 * scale) + rank_width + int(6 * scale)
    name_space = inner_width - (name_x - inner_x) - players_width - int(8 * scale)
    draw.text(
        (name_x, cursor),
        _fit(draw, card.name, name_font, name_space),
        font=name_font,
        fill=config.TEXT_PRIMARY + (255,),
    )
    draw.text(
        (inner_x + inner_width - players_width, cursor + int(5 * scale)),
        players,
        font=value_font,
        fill=config.TEXT_SECONDARY + (255,),
    )
    cursor += int(21 * scale)

    # Row 2: ownership chip, then sector.
    chip_right = _owner_tag(draw, inner_x + int(4 * scale), cursor, card.owner, chip_font, scale)
    if card.sector:
        sector_x = chip_right + int(8 * scale)
        draw.text(
            (sector_x, cursor + int(3 * scale)),
            _fit(draw, f"{card.sector.upper()} SECTOR", meta_font, inner_x + inner_width - sector_x),
            font=meta_font,
            fill=config.TEXT_MUTED + (255,),
        )
    cursor += int(19 * scale) + int(9 * scale)

    bar_x = inner_x + int(4 * scale)
    bar_width = inner_width - int(4 * scale)

    # Primary bar: Super Earth blue for both liberation and defence.
    label = card.progress_label
    value = f"{card.progress_pct:.1f}%  {format_rate(card.progress_rate)}"
    draw.text((bar_x, cursor), label, font=meta_font, fill=config.TEXT_SECONDARY + (255,))
    value_width = draw.textlength(value, font=value_font)
    draw.text(
        (bar_x + bar_width - value_width, cursor),
        value,
        font=value_font,
        fill=config.TEXT_PRIMARY + (255,),
    )
    cursor += int(14 * scale)
    _bar(draw, bar_x, cursor, bar_width, config.BAR_HEIGHT, card.progress_pct / 100.0, config.SUPER_EARTH_BLUE)
    cursor += config.BAR_HEIGHT

    # Secondary bar: the opposing campaign, in that faction's colour.
    if card.is_timed and card.time_pct is not None:
        cursor += int(6 * scale)
        enemy_color = _faction_color(card.opposing_faction)
        remaining = card.time_remaining(now) or ""
        enemy_label = f"{_faction_label(card.opposing_faction)} CAMPAIGN"
        if remaining:
            enemy_label = f"{enemy_label} · {remaining}"
        # Mirror the liberation row: percentage plus rate on the right.
        enemy_value = f"{card.time_pct:.1f}%  {format_rate(card.time_rate)}"

        enemy_width = draw.textlength(enemy_value, font=value_font)
        draw.text(
            (bar_x, cursor),
            _fit(draw, enemy_label, meta_font, bar_width - enemy_width - int(10 * scale)),
            font=meta_font,
            fill=enemy_color + (255,),
        )
        draw.text(
            (bar_x + bar_width - enemy_width, cursor),
            enemy_value,
            font=value_font,
            fill=config.TEXT_SECONDARY + (255,),
        )
        cursor += int(14 * scale)
        _bar(draw, bar_x, cursor, bar_width, config.BAR_HEIGHT, card.time_pct / 100.0, enemy_color)

    return y + height


# --------------------------------------------------------------------------- entry point


def render_monitor(snapshot: WarSnapshot, size: tuple[int, int], now: datetime) -> Image.Image:
    width, height = size
    panel_width, scale = _panel_metrics(size)

    background_planet = snapshot.background_planet
    biome_name = background_planet.biome_name if background_planet else ""
    canvas = biomes.get_backdrop(biome_name, size).convert("RGB")
    _apply_scrim(canvas, panel_width)

    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    padding = int(config.PANEL_PADDING * scale)
    margin = int(config.PANEL_MARGIN * scale)
    panel_x = width - margin - panel_width
    content_x = panel_x + padding
    content_width = panel_width - padding * 2

    cards = snapshot.planets[: config.PLANET_COUNT]

    # Measure first so the panel can be vertically centred.
    content_height = int(60 * scale)
    if snapshot.major_order is not None:
        content_height += int(90 * scale) + int(config.CARD_GAP * scale)
    for card in cards:
        content_height += _card_height(card, scale) + int(config.CARD_GAP * scale)
    content_height += int(14 * scale)  # footer line

    panel_height = min(height - margin * 2, content_height + padding * 2)
    panel_y = (height - panel_height) // 2

    draw.rounded_rectangle(
        (panel_x, panel_y, panel_x + panel_width, panel_y + panel_height),
        radius=int(config.CORNER_RADIUS * 1.4 * scale),
        fill=config.PANEL_BG,
        outline=config.PANEL_BORDER,
        width=1,
    )

    cursor = panel_y + padding
    cursor = _draw_header(draw, content_x, cursor, content_width, snapshot, now, scale)

    if snapshot.major_order is not None:
        cursor = _draw_major_order(draw, content_x, cursor, content_width, snapshot.major_order, now, scale)
        cursor += int(config.CARD_GAP * scale)

    for rank, card in enumerate(cards, start=1):
        cursor = _draw_card(draw, content_x, cursor, content_width, card, rank, now, scale)
        cursor += int(config.CARD_GAP * scale)

    # Footer credit for the data source.
    footer_font = _load_font("body", int(10 * scale))
    footer = "helldivers2.dev"
    footer_width = draw.textlength(footer, font=footer_font)
    draw.text(
        (content_x + content_width - footer_width, panel_y + panel_height - padding - int(11 * scale)),
        footer,
        font=footer_font,
        fill=config.TEXT_MUTED + (170,),
    )

    if background_planet is not None and background_planet.biome_name:
        caption_font = _load_font("body", int(11 * scale))
        caption = f"{background_planet.name} · {background_planet.biome_name.upper()}"
        draw.text(
            (margin + int(8 * scale), height - margin - int(16 * scale)),
            caption,
            font=caption_font,
            fill=(255, 255, 255, 90),
        )

    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    return canvas
