"""One update cycle: fetch, model, render, apply."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import api, config, render, wallpaper
from .history import History
from .model import (
    CampaignState,
    Dispatch,
    MajorOrder,
    MajorOrderTask,
    PlanetCard,
    WarSnapshot,
    build_major_order,
    choose_background_planet,
    latest_dispatch,
    select_planets,
)

log = logging.getLogger("hd2tracker")


# --------------------------------------------------------------------------- infrastructure


def setup_logging(verbose: bool = False) -> None:
    config.ensure_state_dirs()
    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # pythonw.exe has no usable stdout; only attach a console handler when there is one.
    if sys.stderr is not None and sys.stderr.isatty():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)


class SingleInstance:
    """Best-effort lock so overlapping scheduled runs cannot fight over files."""

    STALE_AFTER = 600  # seconds

    def __init__(self, path: Path | None = None):
        self.path = path or config.LOCK_FILE
        self.acquired = False

    def __enter__(self) -> "SingleInstance":
        try:
            if self.path.exists() and time.time() - self.path.stat().st_mtime > self.STALE_AFTER:
                log.warning("clearing stale lock %s", self.path)
                self.path.unlink(missing_ok=True)
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            self.acquired = True
        except FileExistsError:
            self.acquired = False
        except OSError as exc:
            log.debug("lock unavailable (%s); continuing without one", exc)
            self.acquired = True
        return self

    def __exit__(self, *exc_info) -> None:
        if self.acquired:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------------------- snapshot cache


def _card_to_dict(card: PlanetCard) -> dict:
    return {
        "index": card.index,
        "name": card.name,
        "sector": card.sector,
        "biome": card.biome_name,
        "biomeDescription": card.biome_description,
        "owner": card.owner,
        "players": card.players,
        "state": card.state.value,
        "progress": round(card.progress_pct, 4),
        "progressRate": card.progress_rate,
        "opposing": card.opposing_faction,
        "timePct": card.time_pct,
        "timeRate": card.time_rate,
        "endsAt": card.ends_at.isoformat().replace("+00:00", "Z") if card.ends_at else None,
    }


def _card_from_dict(data: dict) -> PlanetCard:
    ends_at = data.get("endsAt")
    return PlanetCard(
        index=int(data["index"]),
        name=data.get("name", ""),
        sector=data.get("sector", ""),
        biome_name=data.get("biome", ""),
        biome_description=data.get("biomeDescription", ""),
        owner=data.get("owner", "Humans"),
        players=int(data.get("players", 0)),
        state=CampaignState(data.get("state", "liberation")),
        progress_pct=float(data.get("progress", 0.0)),
        progress_rate=data.get("progressRate"),
        opposing_faction=data.get("opposing"),
        time_pct=data.get("timePct"),
        time_rate=data.get("timeRate"),
        ends_at=datetime.fromisoformat(ends_at.replace("Z", "+00:00")) if ends_at else None,
    )


def _dispatch_to_dict(dispatch: Dispatch) -> dict:
    return {
        "id": dispatch.id,
        "published": dispatch.published.isoformat().replace("+00:00", "Z") if dispatch.published else None,
        "headline": dispatch.headline,
        "body": dispatch.body,
    }


def _dispatch_from_dict(data: dict) -> Dispatch | None:
    if not isinstance(data, dict):
        return None
    published = data.get("published")
    try:
        stamp = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
    except (AttributeError, ValueError):
        stamp = None
    return Dispatch(
        id=int(data.get("id", 0) or 0),
        published=stamp,
        headline=data.get("headline") or None,
        body=str(data.get("body") or ""),
    )


def load_dispatch() -> tuple[Dispatch | None, datetime | None]:
    """Cached dispatch and when it was last fetched."""
    if not config.DISPATCH_CACHE.exists():
        return None, None
    try:
        payload = json.loads(config.DISPATCH_CACHE.read_text(encoding="utf-8"))
        fetched_raw = payload.get("fetchedAt")
        fetched = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00")) if fetched_raw else None
        return _dispatch_from_dict(payload.get("dispatch") or {}), fetched
    except (OSError, ValueError, json.JSONDecodeError, AttributeError) as exc:
        log.warning("could not read dispatch cache: %s", exc)
        return None, None


def save_dispatch(dispatch: Dispatch, fetched_at: datetime) -> None:
    payload = {
        "fetchedAt": fetched_at.isoformat().replace("+00:00", "Z"),
        "dispatch": _dispatch_to_dict(dispatch),
    }
    temp = config.DISPATCH_CACHE.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    temp.replace(config.DISPATCH_CACHE)


def resolve_dispatch(now: datetime) -> Dispatch | None:
    """Return the newest dispatch, refetching only when the cache has aged out.

    The feed is 372 KB and cannot be filtered server-side, while dispatches are
    published two or three times a day - so it gets its own refresh interval
    rather than riding the five-minute render cycle.
    """
    cached, fetched_at = load_dispatch()

    if cached is not None and fetched_at is not None:
        age_minutes = (now - fetched_at).total_seconds() / 60.0
        if 0 <= age_minutes < config.DISPATCH_REFRESH_MINUTES:
            log.debug("dispatch cache is %.1f minutes old; not refetching", age_minutes)
            return cached

    try:
        dispatch = latest_dispatch(api.fetch_dispatches())
    except api.ApiError as exc:
        log.warning("dispatch feed unavailable: %s", exc)
        return cached

    if dispatch is None:
        return cached

    try:
        save_dispatch(dispatch, now)
    except OSError as exc:
        log.debug("could not cache dispatch: %s", exc)
    log.info("dispatch refreshed: %s", dispatch.headline or dispatch.body[:60])
    return dispatch


def save_snapshot(snapshot: WarSnapshot) -> None:
    payload = {
        "observedAt": snapshot.observed_at.isoformat().replace("+00:00", "Z"),
        "totalPlayers": snapshot.total_players,
        "activeCampaigns": snapshot.active_campaigns,
        "backgroundIndex": snapshot.background.index if snapshot.background else None,
        "planets": [_card_to_dict(card) for card in snapshot.planets],
        "majorOrder": None,
        "dispatch": _dispatch_to_dict(snapshot.dispatch) if snapshot.dispatch else None,
    }
    if snapshot.major_order is not None:
        payload["majorOrder"] = {
            "title": snapshot.major_order.title,
            "briefing": snapshot.major_order.briefing,
            "decoded": snapshot.major_order.decoded,
            "expiresAt": (
                snapshot.major_order.expires_at.isoformat().replace("+00:00", "Z")
                if snapshot.major_order.expires_at
                else None
            ),
            "tasks": [
                {"label": task.label, "current": task.current, "goal": task.goal}
                for task in snapshot.major_order.tasks
            ],
        }

    temp = config.SNAPSHOT_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    temp.replace(config.SNAPSHOT_FILE)


def load_snapshot() -> WarSnapshot | None:
    if not config.SNAPSHOT_FILE.exists():
        return None
    try:
        payload = json.loads(config.SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("could not read cached snapshot: %s", exc)
        return None

    try:
        observed_at = datetime.fromisoformat(str(payload["observedAt"]).replace("Z", "+00:00"))
        planets = [_card_from_dict(entry) for entry in payload.get("planets", [])]
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("cached snapshot is malformed: %s", exc)
        return None

    major_order = None
    raw_order = payload.get("majorOrder")
    if isinstance(raw_order, dict):
        expires = raw_order.get("expiresAt")
        major_order = MajorOrder(
            title=raw_order.get("title", "MAJOR ORDER"),
            briefing=raw_order.get("briefing", ""),
            tasks=[
                MajorOrderTask(
                    label=task.get("label", "Objective"),
                    current=int(task.get("current", 0)),
                    goal=int(task.get("goal", 1)),
                )
                for task in raw_order.get("tasks", [])
            ],
            expires_at=datetime.fromisoformat(expires.replace("Z", "+00:00")) if expires else None,
            decoded=bool(raw_order.get("decoded", True)),
        )

    background_index = payload.get("backgroundIndex")
    background = next((card for card in planets if card.index == background_index), None)

    return WarSnapshot(
        planets=planets,
        major_order=major_order,
        total_players=int(payload.get("totalPlayers", 0)),
        active_campaigns=int(payload.get("activeCampaigns", 0)),
        observed_at=observed_at,
        background=background,
        dispatch=_dispatch_from_dict(payload.get("dispatch") or {}) if payload.get("dispatch") else None,
    )


# --------------------------------------------------------------------------- cycle


def build_snapshot() -> WarSnapshot:
    """Fetch live data and turn it into a renderable snapshot."""
    history = History.load()
    campaigns, assignments, server_time = api.fetch_war_state()

    cards = select_planets(campaigns, server_time, history.previous_order)
    total_players = sum(card.players for card in cards)

    for card in cards:
        history.record(card.index, server_time, card.progress_pct)
    history.prune({card.index for card in cards})

    for card in cards:
        card.progress_rate = history.rate_per_hour(card.index)

    top = cards[: config.PLANET_COUNT]
    background = choose_background_planet(cards, history.previous_top_index)

    major_order = None
    if assignments:
        # Only pay for the planet-name lookup when there is a Major Order to label.
        planet_names = {card.index: card.name for card in cards}
        try:
            planet_names.update(api.fetch_planet_names())
        except Exception:  # noqa: BLE001 - labels are cosmetic
            log.debug("planet name lookup failed; using campaign names only")
        major_order = build_major_order(assignments, server_time, planet_names)

    history.previous_order = [card.index for card in cards]
    history.previous_top_index = background.index if background else None
    history.save()

    # Throttled and cached; a failure here must not take the cycle down.
    try:
        dispatch = resolve_dispatch(server_time)
    except Exception:  # noqa: BLE001 - the dispatch is decoration, not data
        log.exception("dispatch resolution failed; continuing without it")
        dispatch = None

    return WarSnapshot(
        planets=top,
        major_order=major_order,
        total_players=total_players,
        active_campaigns=len(cards),
        observed_at=server_time,
        background=background,
        dispatch=dispatch,
    )


def render_all(
    snapshot: WarSnapshot, monitors: list[wallpaper.Monitor], slot: str, output_dir: Path | None = None
) -> dict[int, Path]:
    now = datetime.now(timezone.utc)
    written: dict[int, Path] = {}

    # Monitors sharing both a size and a taskbar layout share a render.
    cache: dict[tuple, "object"] = {}
    for monitor in monitors:
        key = (monitor.size, monitor.insets)
        image = cache.get(key)
        if image is None:
            image = render.render_monitor(snapshot, monitor.size, now, monitor.insets)
            cache[key] = image

        if output_dir is not None:
            path = output_dir / f"preview_mon{monitor.index}_{monitor.width}x{monitor.height}.png"
        else:
            path = wallpaper.output_path(monitor.index, slot)

        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp.png")
        image.save(temp, "PNG")
        temp.replace(path)
        written[monitor.index] = path
        log.info("rendered monitor %d (%dx%d) -> %s", monitor.index, monitor.width, monitor.height, path.name)

    return written


def run_cycle(preview_dir: Path | None = None, apply_wallpaper: bool = True) -> int:
    """Run one full update. Returns a process exit code."""
    config.ensure_state_dirs()

    try:
        monitors = wallpaper.list_monitors()
    except wallpaper.WallpaperError as exc:
        log.error("could not enumerate monitors: %s", exc)
        return 3

    try:
        snapshot = build_snapshot()
    except api.ApiError as exc:
        log.error("API unavailable: %s", exc)
        cached = load_snapshot()
        if cached is None:
            log.error("no cached snapshot to fall back on; leaving the desktop alone")
            return 2
        age = datetime.now(timezone.utc) - cached.observed_at
        cached.stale = age > timedelta(minutes=config.STALE_AFTER_MINUTES)
        log.warning("re-rendering cached snapshot (%.0f minutes old)", age.total_seconds() / 60)
        snapshot = cached

    if not snapshot.planets:
        log.error("no active campaigns returned; leaving the desktop alone")
        return 2

    if apply_wallpaper:
        wallpaper.capture_backup()

    slot = wallpaper.next_slot() if apply_wallpaper else "preview"
    written = render_all(snapshot, monitors, slot, preview_dir)

    if not snapshot.stale:
        save_snapshot(snapshot)

    if apply_wallpaper:
        try:
            wallpaper.apply(written)
        except wallpaper.WallpaperError as exc:
            log.error("could not set wallpaper: %s", exc)
            return 4

    return 0


def describe(snapshot: WarSnapshot) -> str:
    """Human-readable dump used by --dry-run."""
    from .history import format_rate

    now = datetime.now(timezone.utc)
    lines = [
        f"Observed at : {snapshot.observed_at.astimezone():%Y-%m-%d %H:%M:%S %Z}",
        f"Helldivers  : {snapshot.total_players:,} across {snapshot.active_campaigns} active campaigns",
    ]

    background = snapshot.background_planet
    if background:
        lines.append(f"Backdrop    : {background.name} — {background.biome_name}")

    if snapshot.dispatch is None:
        lines.append("Dispatch    : none")
    else:
        dispatch = snapshot.dispatch
        age = dispatch.age(now)
        lines.append(f"Dispatch    : [{dispatch.id}] {dispatch.headline or '(no headline)'} — {age}")
        body = dispatch.body.replace("\n", " ")
        lines.append(f"              {body[:150]}{'…' if len(body) > 150 else ''}")

    if snapshot.major_order is None:
        lines.append("Major Order : none active")
    else:
        order = snapshot.major_order
        lines.append(
            f"Major Order : {order.headline} — {order.completed_count}/{order.total_count} objectives"
            f" — {order.time_remaining(now) or 'no expiry'}"
        )
        for task in order.tasks:
            mark = "x" if task.complete else " "
            lines.append(f"              [{mark}] {task.label}  ({task.current:,}/{task.goal:,})")

    lines.append("")
    header = f"{'#':<3}{'PLANET':<18}{'OWNER':<12}{'STATE':<17}{'PLAYERS':>8}  {'PROGRESS':>9}  {'RATE':>10}"
    lines.append(header)
    lines.append("-" * len(header))

    for rank, card in enumerate(snapshot.planets, start=1):
        lines.append(
            f"{rank:<3}{card.name[:17]:<18}{card.owner:<12}{card.state.value:<17}"
            f"{card.players:>8,}  {card.progress_pct:>8.2f}%  {format_rate(card.progress_rate):>10}"
        )
        if card.is_timed and card.time_pct is not None:
            lines.append(
                f"   └─ vs {card.opposing_faction or '?':<10} window elapsed "
                f"{card.time_pct:.1f}%  ends in {card.time_remaining(now)}"
            )

    return "\n".join(lines)
