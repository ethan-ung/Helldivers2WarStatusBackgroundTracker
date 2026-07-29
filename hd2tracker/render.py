"""Compose the wallpaper: biome backdrop, right-side scrim, vertical status panel.

Bar colour rules, per the spec:

* liberation progress and Super Earth defence progress are always Super Earth blue
* the opposing campaign bar takes the colour of the faction pressing it

Everything scales off the panel width so the same layout works on a 1080p panel
and a 1440p ultrawide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import biomes, config
from .history import format_rate
from .model import Dispatch, MajorOrder, MajorOrderTask, PlanetCard, WarSnapshot

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


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int | None = None,
) -> list[str]:
    """Greedy word wrap - Pillow has no auto-wrap of its own."""
    words = (text or "").split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)

    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(draw, lines[-1] + " …", font, max_width)

    # A single word longer than the column would still overflow.
    return [_fit(draw, line, font, max_width) for line in lines]


# --------------------------------------------------------------------------- primitives


def _content_height(
    cards: list[PlanetCard], mo_height: int, dispatch_height: int, scale: float, card_gap: int
) -> int:
    """Total height the panel contents need at a given scale."""
    height = int(60 * scale)  # header
    if mo_height:
        height += mo_height + card_gap
    if dispatch_height:
        height += dispatch_height + card_gap
    for card in cards:
        height += _card_height(card, scale) + card_gap
    return height + int(14 * scale)  # footer line


@dataclass
class PanelLayout:
    panel_width: int
    scale: float
    cards: list[PlanetCard]
    card_gap: int
    major_order: MajorOrderLayout | None
    dispatch: DispatchLayout | None = None


def _layout(
    cards: list[PlanetCard],
    order: MajorOrder | None,
    dispatch: Dispatch | None,
    size: tuple[int, int],
    insets: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> PanelLayout:
    """Resolve panel width, type scale, how many cards fit and the gap between them.

    Scale is the single source of truth - panel width is always derived from it,
    so the two can never disagree and leave text overflowing its container. It
    starts from display height (the panel is vertical, so height is what governs
    legibility) and is then held down by whichever of these binds first:

    * the share of display width the panel may occupy
    * the absolute maximum panel width
    * the absolute maximum scale

    Whatever survives is shrunk until the contents fit vertically, and only if
    that bottoms out at the scale floor do cards get dropped.

    The Major Order block is *measured* rather than estimated: its height depends
    on how its text wraps and how many objectives it carries, so it is laid out
    against the current panel width on every iteration.
    """
    width, height = size
    inset_left, inset_top, inset_right, inset_bottom = insets
    usable_width = max(1, width - inset_left - inset_right)
    usable_height = max(1, height - inset_top - inset_bottom)

    scale = usable_height / config.PANEL_REFERENCE_HEIGHT
    scale = min(
        scale,
        (usable_width * config.PANEL_WIDTH_FRACTION) / config.PANEL_BASE_WIDTH,
        config.PANEL_WIDTH_MAX / config.PANEL_BASE_WIDTH,
        config.PANEL_SCALE_MAX,
    )
    scale = max(scale, config.PANEL_SCALE_MIN)

    # Text measurement needs a drawing context but not a real canvas.
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    shown = list(cards)
    mo_layout: MajorOrderLayout | None = None
    dispatch_layout: DispatchLayout | None = None
    card_gap = int(config.CARD_GAP * scale)

    for _ in range(12):
        panel_width = max(1, round(config.PANEL_BASE_WIDTH * scale))
        padding = int(config.PANEL_PADDING * scale)
        margin = int(config.PANEL_MARGIN * scale)
        content_width = panel_width - padding * 2

        mo_layout = _major_order_layout(probe, order, content_width, scale) if order else None
        mo_height = mo_layout.height if mo_layout else 0
        dispatch_layout = _dispatch_layout(probe, dispatch, content_width, scale) if dispatch else None
        dispatch_height = dispatch_layout.height if dispatch_layout else 0

        base_gap = int(config.CARD_GAP * scale)
        available = usable_height - margin * 2
        needed = _content_height(shown, mo_height, dispatch_height, scale, base_gap) + padding * 2

        if needed <= available or available <= 0:
            # The panel is full height, so spread the slack across the gaps
            # instead of leaving a void at the bottom.
            slots = len(shown) + (1 if mo_height else 0) + (1 if dispatch_height else 0)
            slack = max(0, available - needed)
            card_gap = base_gap
            if slots and slack:
                card_gap = min(int(config.CARD_GAP_MAX * scale), base_gap + slack // slots)
            break

        shrunk = max(config.PANEL_SCALE_MIN, scale * (available / needed))
        if shrunk < scale - 1e-6:
            scale = shrunk
            continue
        # Already as small as we allow: drop the least busy planet rather than
        # let cards spill past the bottom of the panel.
        if len(shown) > 1:
            shown.pop()
            continue
        card_gap = base_gap
        break

    return PanelLayout(
        panel_width=max(1, round(config.PANEL_BASE_WIDTH * scale)),
        scale=scale,
        cards=shown,
        card_gap=card_gap,
        major_order=mo_layout,
        dispatch=dispatch_layout,
    )


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
    remainder_color: tuple[int, int, int] | None = None,
) -> None:
    """Square progress bar.

    ``remainder_color`` tints the unfilled portion with the colour of the faction
    holding the planet. It is drawn dimmed: a planet at 0.0% liberated would
    otherwise be a solid block of faction colour, which reads as a full bar
    rather than an empty one. The filled portion stays at full strength so the
    contrast carries the meaning.
    """
    track = (
        remainder_color + (config.BAR_REMAINDER_ALPHA,)
        if remainder_color is not None
        else config.TRACK_COLOR
    )
    draw.rectangle((x, y, x + width, y + height), fill=track)

    filled = int(width * max(0.0, min(1.0, fraction)))
    if filled <= 0:
        return
    # Keep a sliver visible at tiny fractions without overstating them.
    filled = max(filled, 2)
    draw.rectangle((x, y, x + filled, y + height), fill=color + (255,))


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
    draw.rectangle(
        (x, y, right, bottom),
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


# Row heights for the Major Order block, in unscaled units. Declared once so the
# measure pass and the draw pass cannot drift apart.
_MO_LABEL_H = 16
_MO_HEADLINE_H = 18
_MO_BRIEFING_H = 15
_MO_OBJECTIVE_LINE_H = 15
_MO_SUMMARY_H = 17
_MO_DIVIDER_GAP = 10
_MO_BRIEFING_LEAD = 5


@dataclass
class MajorOrderLayout:
    """Wrapped text and the exact height it needs, shared by measure and draw."""

    headline: list[str] = field(default_factory=list)
    briefing: list[str] = field(default_factory=list)
    objectives: list[tuple[MajorOrderTask, list[str], str | None]] = field(default_factory=list)
    summary: str = ""
    height: int = 0


def _major_order_layout(
    draw: ImageDraw.ImageDraw, order: MajorOrder, width: int, scale: float
) -> MajorOrderLayout:
    title_font = _load_font("display", int(16 * scale))
    body_font = _load_font("body", int(12 * scale))
    count_font = _load_font("mono", int(11 * scale))

    pad = int(config.CARD_PADDING * scale)
    inner_width = width - pad * 2
    box = max(8, int(config.CHECKBOX_SIZE * scale))
    indent = box + int(9 * scale)
    line_h = int(_MO_OBJECTIVE_LINE_H * scale)

    layout = MajorOrderLayout()

    # The API usually returns a literal "MAJOR ORDER" title, in which case the
    # briefing is the only real prose and gets the room to say its piece - the
    # game presents it the same way, as a paragraph with no heading.
    title = (order.title or "").strip()
    if title and title.upper() != "MAJOR ORDER":
        layout.headline = _wrap(draw, title.upper(), title_font, inner_width, max_lines=2)
        if order.briefing and order.briefing.strip() != title:
            layout.briefing = _wrap(draw, order.briefing, body_font, inner_width, max_lines=3)
    else:
        layout.briefing = _wrap(draw, order.briefing, body_font, inner_width, max_lines=5)

    for task in order.tasks:
        count = task.display_progress
        reserved = draw.textlength(count, font=count_font) + int(10 * scale) if count else 0
        lines = _wrap(draw, task.label, body_font, inner_width - indent - reserved, max_lines=2)
        layout.objectives.append((task, lines or [task.label], count))

    if order.total_count:
        layout.summary = f"{order.completed_count} / {order.total_count} OBJECTIVES COMPLETE"

    height = pad * 2
    height += int(_MO_LABEL_H * scale)
    height += len(layout.headline) * int(_MO_HEADLINE_H * scale)
    if layout.briefing:
        height += int(_MO_BRIEFING_LEAD * scale) + len(layout.briefing) * int(_MO_BRIEFING_H * scale)
    if layout.objectives:
        height += int(_MO_DIVIDER_GAP * scale)
        for _task, lines, _count in layout.objectives:
            height += max(box, len(lines) * line_h) + int(config.OBJECTIVE_GAP * scale)
    if layout.summary:
        height += int(_MO_SUMMARY_H * scale)

    layout.height = height
    return layout


def _draw_major_order(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    order: MajorOrder,
    layout: MajorOrderLayout,
    now: datetime,
    scale: float,
) -> int:
    label_font = _load_font("body", int(11 * scale))
    title_font = _load_font("display", int(16 * scale))
    body_font = _load_font("body", int(12 * scale))
    count_font = _load_font("mono", int(11 * scale))

    pad = int(config.CARD_PADDING * scale)
    inner_x = x + pad
    inner_width = width - pad * 2
    box = max(8, int(config.CHECKBOX_SIZE * scale))
    indent = box + int(9 * scale)
    line_h = int(_MO_OBJECTIVE_LINE_H * scale)
    cursor = y + pad

    draw.rectangle(
        (x, y, x + width, y + layout.height),
        fill=None,
        outline=config.ACCENT_YELLOW + (70,),
        width=1,
    )

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
    cursor += int(_MO_LABEL_H * scale)

    for line in layout.headline:
        draw.text((inner_x, cursor), line, font=title_font, fill=config.TEXT_PRIMARY + (255,))
        cursor += int(_MO_HEADLINE_H * scale)

    if layout.briefing:
        cursor += int(_MO_BRIEFING_LEAD * scale)
        for line in layout.briefing:
            draw.text((inner_x, cursor), line, font=body_font, fill=config.TEXT_SECONDARY + (255,))
            cursor += int(_MO_BRIEFING_H * scale)

    if layout.objectives:
        gap = int(_MO_DIVIDER_GAP * scale)
        lead = gap // 2
        draw.rectangle(
            (inner_x, cursor + lead, inner_x + inner_width, cursor + lead),
            fill=config.PANEL_BORDER,
        )
        cursor += gap

        for task, lines, count in layout.objectives:
            row = max(box, len(lines) * line_h)

            # Tick box, centred against the first line of text.
            box_y = cursor + max(0, (line_h - box) // 2)
            if task.complete:
                draw.rectangle(
                    (inner_x, box_y, inner_x + box, box_y + box),
                    fill=config.OBJECTIVE_COMPLETE + (255,),
                )
            else:
                draw.rectangle(
                    (inner_x, box_y, inner_x + box, box_y + box),
                    fill=config.OBJECTIVE_BOX_EMPTY + (255,),
                    outline=config.OBJECTIVE_BOX_BORDER + (255,),
                    width=1,
                )

            text_color = config.TEXT_PRIMARY if task.complete else config.TEXT_SECONDARY
            text_y = cursor
            for line in lines:
                draw.text((inner_x + indent, text_y), line, font=body_font, fill=text_color + (255,))
                text_y += line_h

            # Counter objectives carry their figure as plain text - no bar.
            if count:
                count_width = draw.textlength(count, font=count_font)
                draw.text(
                    (inner_x + inner_width - count_width, cursor),
                    count,
                    font=count_font,
                    fill=config.TEXT_MUTED + (255,),
                )

            cursor += row + int(config.OBJECTIVE_GAP * scale)

    if layout.summary:
        draw.text((inner_x, cursor), layout.summary, font=label_font, fill=config.TEXT_MUTED + (255,))

    return y + layout.height


# Row heights for the dispatch block, in unscaled units.
_DISPATCH_LABEL_H = 16
_DISPATCH_HEADLINE_H = 18
_DISPATCH_BODY_H = 15
_DISPATCH_BODY_LEAD = 4


@dataclass
class DispatchLayout:
    headline: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    height: int = 0


def _dispatch_layout(
    draw: ImageDraw.ImageDraw, dispatch: Dispatch, width: int, scale: float
) -> DispatchLayout:
    title_font = _load_font("display", int(15 * scale))
    body_font = _load_font("body", int(12 * scale))

    pad = int(config.CARD_PADDING * scale)
    inner_width = width - pad * 2

    layout = DispatchLayout()
    if dispatch.headline:
        layout.headline = _wrap(draw, dispatch.headline.upper(), title_font, inner_width, max_lines=2)
    layout.body = _wrap(
        draw,
        dispatch.body.replace("\n", " "),
        body_font,
        inner_width,
        max_lines=config.DISPATCH_BODY_LINES,
    )

    height = pad * 2 + int(_DISPATCH_LABEL_H * scale)
    height += len(layout.headline) * int(_DISPATCH_HEADLINE_H * scale)
    if layout.body:
        height += int(_DISPATCH_BODY_LEAD * scale) + len(layout.body) * int(_DISPATCH_BODY_H * scale)

    layout.height = height
    return layout


def _draw_dispatch(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    dispatch: Dispatch,
    layout: DispatchLayout,
    now: datetime,
    scale: float,
) -> int:
    label_font = _load_font("body", int(11 * scale))
    title_font = _load_font("display", int(15 * scale))
    body_font = _load_font("body", int(12 * scale))

    pad = int(config.CARD_PADDING * scale)
    inner_x = x + pad
    inner_width = width - pad * 2
    cursor = y + pad

    draw.rectangle(
        (x, y, x + width, y + layout.height),
        fill=None,
        outline=config.DISPATCH_ACCENT + (70,),
        width=1,
    )

    draw.text(
        (inner_x, cursor), "HIGH COMMAND DISPATCH", font=label_font, fill=config.DISPATCH_ACCENT + (255,)
    )
    age = dispatch.age(now)
    if age:
        age_width = draw.textlength(age, font=label_font)
        draw.text(
            (inner_x + inner_width - age_width, cursor),
            age,
            font=label_font,
            fill=config.TEXT_MUTED + (255,),
        )
    cursor += int(_DISPATCH_LABEL_H * scale)

    for line in layout.headline:
        draw.text((inner_x, cursor), line, font=title_font, fill=config.TEXT_PRIMARY + (255,))
        cursor += int(_DISPATCH_HEADLINE_H * scale)

    if layout.body:
        cursor += int(_DISPATCH_BODY_LEAD * scale)
        for line in layout.body:
            draw.text((inner_x, cursor), line, font=body_font, fill=config.TEXT_SECONDARY + (255,))
            cursor += int(_DISPATCH_BODY_H * scale)

    return y + layout.height


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
    draw.rectangle(
        (x, y, x + width, y + height),
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
    draw.rectangle(
        (x, y + pad, x + max(2, int(3 * scale)), y + height - pad),
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
    # On an enemy-held planet the unfilled remainder carries that faction's
    # colour. A planet Super Earth still holds keeps the neutral track, so a
    # defence reads as ours under pressure rather than as territory lost.
    remainder = _faction_color(card.owner) if card.owner != "Humans" else None
    _bar(
        draw,
        bar_x,
        cursor,
        bar_width,
        config.BAR_HEIGHT,
        card.progress_pct / 100.0,
        config.SUPER_EARTH_BLUE,
        remainder_color=remainder,
    )
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


def render_monitor(
    snapshot: WarSnapshot,
    size: tuple[int, int],
    now: datetime,
    insets: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Image.Image:
    """Render one monitor.

    ``insets`` are the edges the shell reserves - the taskbar, mainly. The image
    is always the full monitor size, because the wallpaper sits behind the
    taskbar; only the panel is kept inside the work area.
    """
    width, height = size
    inset_left, inset_top, inset_right, inset_bottom = insets
    layout = _layout(
        snapshot.planets[: config.PLANET_COUNT],
        snapshot.major_order,
        snapshot.dispatch,
        size,
        insets,
    )
    panel_width, scale, cards, card_gap = (
        layout.panel_width,
        layout.scale,
        layout.cards,
        layout.card_gap,
    )

    background_planet = snapshot.background_planet
    biome_name = background_planet.biome_name if background_planet else ""
    canvas = biomes.get_backdrop(biome_name, size).convert("RGB")
    _apply_scrim(canvas, panel_width)

    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    padding = int(config.PANEL_PADDING * scale)
    margin = int(config.PANEL_MARGIN * scale)
    panel_x = width - inset_right - margin - panel_width
    content_x = panel_x + padding
    content_width = panel_width - padding * 2

    # The panel runs the full height of the *work area*, so it stops above the
    # taskbar rather than disappearing behind it. Slack is already absorbed into
    # card_gap, and the footer is pinned to the bottom of the panel.
    panel_y = inset_top + margin
    panel_height = max(1, height - inset_top - inset_bottom - margin * 2)

    draw.rectangle(
        (panel_x, panel_y, panel_x + panel_width, panel_y + panel_height),
        fill=config.PANEL_BG,
        outline=config.PANEL_BORDER,
        width=1,
    )

    # On a display far taller than the contents need, the gap cap stops the
    # spreading before the slack is used up. Centre what is left over rather
    # than pooling all of it under the last card.
    mo_height = layout.major_order.height if layout.major_order else 0
    dispatch_height = layout.dispatch.height if layout.dispatch else 0
    used = _content_height(cards, mo_height, dispatch_height, scale, card_gap) + padding * 2
    cursor = panel_y + padding + max(0, (panel_height - used) // 2)

    cursor = _draw_header(draw, content_x, cursor, content_width, snapshot, now, scale)

    if snapshot.major_order is not None and layout.major_order is not None:
        cursor = _draw_major_order(
            draw,
            content_x,
            cursor,
            content_width,
            snapshot.major_order,
            layout.major_order,
            now,
            scale,
        )
        cursor += card_gap

    if snapshot.dispatch is not None and layout.dispatch is not None:
        cursor = _draw_dispatch(
            draw,
            content_x,
            cursor,
            content_width,
            snapshot.dispatch,
            layout.dispatch,
            now,
            scale,
        )
        cursor += card_gap

    for rank, card in enumerate(cards, start=1):
        cursor = _draw_card(draw, content_x, cursor, content_width, card, rank, now, scale)
        cursor += card_gap

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
            (
                inset_left + margin + int(8 * scale),
                height - inset_bottom - margin - int(16 * scale),
            ),
            caption,
            font=caption_font,
            fill=(255, 255, 255, 90),
        )

    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    return canvas
