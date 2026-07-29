"""Normalise raw API JSON into the structures the renderer draws.

The important logic here is campaign classification. Three cases exist, and they
are *not* distinguished by ``campaign.faction`` - that field reads "Humans" for
every campaign, because it describes who is fighting, not who is being fought.

The real discriminator is ``planet.event``:

* no event                       -> LIBERATION, we are pushing an enemy planet
* event, ``faction == "Humans"`` -> TIMED_LIBERATION, a human-initiated push with
  a deadline on an enemy-held planet
* event, ``faction != "Humans"`` -> DEFENSE, an enemy is invading our planet

The API documents ``event.faction`` as "the faction that initiated the event",
which is what makes the middle case detectable.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config

log = logging.getLogger(__name__)

HUMANS = "Humans"


class CampaignState(enum.Enum):
    LIBERATION = "liberation"
    TIMED_LIBERATION = "timed_liberation"
    DEFENSE = "defense"


# Faction ids as they appear inside Major Order task values.
FACTION_IDS = {1: "Humans", 2: "Terminids", 3: "Automaton", 4: "Illuminate"}


@dataclass
class PlanetCard:
    index: int
    name: str
    sector: str
    biome_name: str
    biome_description: str
    owner: str
    players: int
    state: CampaignState

    # Primary bar: always Super Earth blue, always "how far along are we".
    progress_pct: float
    progress_rate: float | None = None

    # Secondary bar: only for timed campaigns. Coloured by the opposing faction,
    # measures how much of the event window has burned.
    opposing_faction: str | None = None
    time_pct: float | None = None
    time_rate: float | None = None
    ends_at: datetime | None = None

    @property
    def is_timed(self) -> bool:
        return self.state in (CampaignState.TIMED_LIBERATION, CampaignState.DEFENSE)

    @property
    def progress_label(self) -> str:
        if self.state is CampaignState.DEFENSE:
            return "DEFENSE"
        return "LIBERATED"

    def time_remaining(self, now: datetime) -> str | None:
        if self.ends_at is None:
            return None
        seconds = (self.ends_at - now).total_seconds()
        if seconds <= 0:
            return "ENDED"
        hours, remainder = divmod(int(seconds), 3600)
        minutes = remainder // 60
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


def _compact(value: int) -> str:
    """Shorten large counts so they fit beside an objective."""
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
        return text.replace(".0M", "M")
    if value >= 10_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:,}"


@dataclass
class MajorOrderTask:
    label: str
    current: int
    goal: int

    @property
    def complete(self) -> bool:
        return self.goal > 0 and self.current >= self.goal

    @property
    def fraction(self) -> float:
        if self.goal <= 0:
            return 0.0
        return max(0.0, min(1.0, self.current / self.goal))

    @property
    def display_progress(self) -> str | None:
        """Plain-text count for a counter objective, ``None`` for a binary one.

        Hold and liberate objectives are succeed-or-not, so their tick box says
        everything. A counter that runs for a week would otherwise sit as an
        empty box the whole time, which is why the figure is shown alongside it.
        """
        if self.goal <= 1:
            return None
        return f"{_compact(self.current)} / {_compact(self.goal)}"


@dataclass
class MajorOrder:
    title: str
    briefing: str
    tasks: list[MajorOrderTask] = field(default_factory=list)
    expires_at: datetime | None = None
    decoded: bool = True

    @property
    def headline(self) -> str:
        """The line to show under the MAJOR ORDER label.

        The API frequently returns a literal "MAJOR ORDER" title, which would
        just repeat the label; in that case the briefing carries the actual
        instruction and is used instead.
        """
        title = (self.title or "").strip()
        if title and title.upper() != "MAJOR ORDER":
            return title
        return (self.briefing or "").strip() or title or "MAJOR ORDER"

    @property
    def completed_count(self) -> int:
        return sum(1 for task in self.tasks if task.complete)

    @property
    def total_count(self) -> int:
        return len(self.tasks)

    def time_remaining(self, now: datetime) -> str | None:
        if self.expires_at is None:
            return None
        seconds = (self.expires_at - now).total_seconds()
        if seconds <= 0:
            return "EXPIRED"
        hours, remainder = divmod(int(seconds), 3600)
        minutes = remainder // 60
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


@dataclass
class WarSnapshot:
    planets: list[PlanetCard]
    major_order: MajorOrder | None
    total_players: int
    active_campaigns: int
    observed_at: datetime
    stale: bool = False
    # Chosen with hysteresis, so it can differ from planets[0] when two planets
    # are trading the top slot.
    background: PlanetCard | None = None

    @property
    def background_planet(self) -> PlanetCard | None:
        if self.background is not None:
            return self.background
        return self.planets[0] if self.planets else None


# --------------------------------------------------------------------------- helpers


def _text(value: object) -> str:
    """Localised fields arrive as plain strings when Accept-Language is set."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("en-US", "en", "message"):
            if key in value and isinstance(value[key], str):
                return value[key]
        for candidate in value.values():
            if isinstance(candidate, str):
                return candidate
    return ""


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pct_from_health(health: object, max_health: object) -> float:
    """Progress is the share of the health pool that has been ground down."""
    try:
        health_value = float(health)
        max_value = float(max_health)
    except (TypeError, ValueError):
        return 0.0
    if max_value <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - health_value / max_value) * 100.0))


def _elapsed_pct(start: datetime | None, end: datetime | None, now: datetime) -> float | None:
    if start is None or end is None:
        return None
    total = (end - start).total_seconds()
    if total <= 0:
        return None
    return max(0.0, min(100.0, (now - start).total_seconds() / total * 100.0))


def _hours_remaining_rate(start: datetime | None, end: datetime | None) -> float | None:
    """The clock advances at a constant %/hour for the whole window."""
    if start is None or end is None:
        return None
    total_hours = (end - start).total_seconds() / 3600.0
    if total_hours <= 0:
        return None
    return 100.0 / total_hours


# --------------------------------------------------------------------------- planets


def build_planet_card(campaign: dict, now: datetime) -> PlanetCard | None:
    planet = campaign.get("planet")
    if not isinstance(planet, dict):
        return None

    index = planet.get("index")
    if not isinstance(index, int):
        return None

    biome = planet.get("biome") if isinstance(planet.get("biome"), dict) else {}
    statistics = planet.get("statistics") if isinstance(planet.get("statistics"), dict) else {}
    event = planet.get("event") if isinstance(planet.get("event"), dict) else None

    owner = planet.get("currentOwner") or "Humans"
    card = PlanetCard(
        index=index,
        name=_text(planet.get("name")) or f"PLANET {index}",
        sector=_text(planet.get("sector")),
        biome_name=_text(biome.get("name")),
        biome_description=_text(biome.get("description")),
        owner=owner,
        players=int(statistics.get("playerCount") or 0),
        state=CampaignState.LIBERATION,
        progress_pct=_pct_from_health(planet.get("health"), planet.get("maxHealth")),
    )

    if event:
        initiator = event.get("faction") or ""
        start = _parse_time(event.get("startTime"))
        end = _parse_time(event.get("endTime"))

        card.progress_pct = _pct_from_health(event.get("health"), event.get("maxHealth"))
        card.ends_at = end
        card.time_pct = _elapsed_pct(start, end, now)
        card.time_rate = _hours_remaining_rate(start, end)

        if initiator == HUMANS:
            # We initiated a timed push on a planet the enemy still holds.
            card.state = CampaignState.TIMED_LIBERATION
            card.opposing_faction = owner
        else:
            # An enemy initiated an invasion of one of our planets.
            card.state = CampaignState.DEFENSE
            card.opposing_faction = initiator or owner

    return card


def _apply_hysteresis(
    cards: list[PlanetCard], previous_order: list[int], margin: float
) -> list[PlanetCard]:
    """Keep an incumbent ahead unless a challenger clears it by ``margin``.

    Without this the list reshuffles every cycle whenever two planets sit within
    a few hundred players of one another.
    """
    if not previous_order:
        return cards

    rank_of = {index: rank for rank, index in enumerate(previous_order)}
    ordered = list(cards)
    changed = True
    passes = 0
    while changed and passes < len(ordered):
        changed = False
        passes += 1
        for i in range(len(ordered) - 1):
            upper, lower = ordered[i], ordered[i + 1]
            # Only protect an incumbent that previously outranked the challenger.
            upper_rank = rank_of.get(upper.index)
            lower_rank = rank_of.get(lower.index)
            if upper_rank is None or lower_rank is None or lower_rank >= upper_rank:
                continue
            # `lower` used to be ahead; it keeps the slot unless clearly beaten.
            if upper.players < lower.players * (1.0 + margin):
                ordered[i], ordered[i + 1] = lower, upper
                changed = True
    return ordered


def select_planets(
    campaigns: list[dict], now: datetime, previous_order: list[int] | None = None
) -> list[PlanetCard]:
    cards: list[PlanetCard] = []
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        card = build_planet_card(campaign, now)
        if card is not None:
            cards.append(card)

    cards.sort(key=lambda card: card.players, reverse=True)

    if config.ORDER_HYSTERESIS and previous_order:
        cards = _apply_hysteresis(cards, previous_order, config.ORDER_HYSTERESIS_MARGIN)

    return cards


def choose_background_planet(
    cards: list[PlanetCard], previous_top_index: int | None
) -> PlanetCard | None:
    """Pick the busiest planet, resisting a flip-flop when two are neck and neck."""
    if not cards:
        return None
    leader = cards[0]
    if not config.BACKGROUND_HYSTERESIS or previous_top_index is None:
        return leader
    if leader.index == previous_top_index:
        return leader

    incumbent = next((card for card in cards if card.index == previous_top_index), None)
    if incumbent is None:
        return leader
    if leader.players < incumbent.players * (1.0 + config.BACKGROUND_HYSTERESIS_MARGIN):
        return incumbent
    return leader


# --------------------------------------------------------------------------- major order


def _task_slots(task: dict) -> tuple[list[int], list[int]]:
    values = [int(v) for v in task.get("values") or [] if isinstance(v, (int, float))]
    value_types = [int(v) for v in task.get("valueTypes") or [] if isinstance(v, (int, float))]
    return values, value_types


def _slot(values: list[int], value_types: list[int], wanted: int) -> int | None:
    """Read the value tagged with ``wanted``. Tags line up with values by index."""
    if wanted not in value_types:
        return None
    position = value_types.index(wanted)
    return values[position] if position < len(values) else None


# The planet index is tagged 12; 11 is kept as a fallback because the tagging is
# not formally documented. A candidate is only accepted if it names a real planet.
PLANET_SLOT_TAGS = (12, 11)
AMOUNT_SLOT_TAG = 3


def _task_planet(task: dict, planet_names: dict[int, str]) -> str | None:
    values, value_types = _task_slots(task)
    for tag in PLANET_SLOT_TAGS:
        index = _slot(values, value_types, tag)
        if index is not None:
            name = planet_names.get(index)
            if name:
                return name
    return None


def _decode_task_goal(task: dict) -> int:
    """Best-effort goal extraction.

    The API documents ``values`` and ``valueTypes`` as "purpose unknown", so this
    is reverse-engineered and deliberately conservative. Binary planet objectives
    need a goal of 1; counting objectives carry the target in the slot tagged 3.
    """
    task_type = task.get("type")
    values, value_types = _task_slots(task)

    # Liberate / hold a specific planet: succeed-or-not.
    if task_type in (11, 13):
        return 1

    amount = _slot(values, value_types, AMOUNT_SLOT_TAG)
    if amount is not None and amount > 0:
        return amount

    # Fall back to the largest value, ignoring slots that hold a planet index -
    # mistaking one for a target would produce a nonsense goal.
    planet_positions = {value_types.index(tag) for tag in PLANET_SLOT_TAGS if tag in value_types}
    candidates = [v for i, v in enumerate(values) if v > 0 and i not in planet_positions]
    return max(candidates) if candidates else 1


def _decode_task_label(task: dict, planet_names: dict[int, str]) -> str:
    task_type = task.get("type")
    values, value_types = _task_slots(task)

    planet_name = _task_planet(task, planet_names)

    faction_id = _slot(values, value_types, 1)
    faction_name = FACTION_IDS.get(faction_id) if faction_id is not None else None

    goal = _decode_task_goal(task)

    if task_type == 11:
        return f"Liberate {planet_name}" if planet_name else "Liberate planet"
    if task_type == 13:
        return f"Hold {planet_name}" if planet_name else "Hold planet"
    if task_type == 12:
        return f"Defend {goal} planet{'s' if goal != 1 else ''}"
    if task_type == 3:
        target = faction_name or "enemies"
        return f"Kill {goal:,} {target}"
    if task_type == 2:
        return f"Extract {goal:,} items"
    # Unverified: no live order has exercised this type, and the API documents
    # these fields as "purpose unknown". A wrong guess still reads sensibly, and
    # anything unrecognised falls through to the generic label below.
    if task_type == 9:
        return f"Complete {goal:,} Operations"
    if task_type == 15:
        return "Hold the front line"
    return f"Objective ({goal:,})"


def build_major_order(
    assignments: list[dict], now: datetime, planet_names: dict[int, str] | None = None
) -> MajorOrder | None:
    """Build the Major Order panel data, degrading rather than raising."""
    if not assignments:
        return None

    raw = assignments[0]
    if not isinstance(raw, dict):
        return None

    names = planet_names or {}
    title = _text(raw.get("title")) or "MAJOR ORDER"
    briefing = _text(raw.get("briefing")) or _text(raw.get("description"))
    expires_at = _parse_time(raw.get("expiration"))

    raw_tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
    raw_progress = raw.get("progress") if isinstance(raw.get("progress"), list) else []

    tasks: list[MajorOrderTask] = []
    decoded = True
    try:
        for position, task in enumerate(raw_tasks):
            if not isinstance(task, dict):
                continue
            current = 0
            if position < len(raw_progress):
                try:
                    current = int(raw_progress[position])
                except (TypeError, ValueError):
                    current = 0
            tasks.append(
                MajorOrderTask(
                    label=_decode_task_label(task, names),
                    current=max(0, current),
                    goal=max(1, _decode_task_goal(task)),
                )
            )
    except Exception:  # noqa: BLE001 - an odd Major Order must not break the wallpaper
        log.exception("failed to decode Major Order tasks; falling back to a summary")
        decoded = False
        tasks = []

    return MajorOrder(
        title=title,
        briefing=briefing,
        tasks=tasks,
        expires_at=expires_at,
        decoded=decoded and bool(tasks),
    )
