"""Monitor discovery and wallpaper assignment, via the IDesktopWallpaper shim."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

POWERSHELL = "powershell.exe"
SHIM = config.SCRIPTS_DIR / "set_wallpaper.ps1"

# Registry WallpaperStyle -> DESKTOP_WALLPAPER_POSITION
STYLE_TO_POSITION = {"0": 0, "1": 1, "2": 2, "6": 3, "10": 4, "22": 5}


class WallpaperError(RuntimeError):
    """Raised when the desktop wallpaper could not be inspected or changed."""


@dataclass(frozen=True)
class Monitor:
    index: int
    width: int
    height: int
    left: int
    top: int

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


def _run_shim(args: list[str]) -> str:
    if not SHIM.is_file():
        raise WallpaperError(f"shim script missing: {SHIM}")

    command = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SHIM),
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WallpaperError(f"could not run wallpaper shim: {exc}") from exc

    if completed.returncode != 0:
        raise WallpaperError(
            f"wallpaper shim failed ({completed.returncode}): {completed.stderr.strip() or completed.stdout.strip()}"
        )
    if completed.stderr.strip():
        log.debug("shim stderr: %s", completed.stderr.strip())
    return completed.stdout.strip()


def list_monitors() -> list[Monitor]:
    raw = _run_shim(["-Mode", "List"])
    if not raw:
        raise WallpaperError("shim returned no monitors")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WallpaperError(f"could not parse monitor list: {raw[:200]}") from exc

    if isinstance(payload, dict):
        payload = [payload]

    monitors = [
        Monitor(
            index=int(entry["index"]),
            width=int(entry["width"]),
            height=int(entry["height"]),
            left=int(entry.get("left", 0)),
            top=int(entry.get("top", 0)),
        )
        for entry in payload
        if int(entry.get("width", 0)) > 0 and int(entry.get("height", 0)) > 0
    ]
    if not monitors:
        raise WallpaperError("no usable monitors reported")
    return monitors


def next_slot() -> str:
    """Alternate output filenames.

    Windows caches wallpapers by path, so writing the same filename every cycle
    can leave the desktop showing the previous image. Alternating between two
    names sidesteps the cache entirely.
    """
    marker = config.STATE_DIR / "slot.txt"
    previous = ""
    try:
        previous = marker.read_text(encoding="utf-8").strip()
    except OSError:
        pass

    slot = config.WALLPAPER_SLOTS[1] if previous == config.WALLPAPER_SLOTS[0] else config.WALLPAPER_SLOTS[0]
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(slot, encoding="utf-8")
    except OSError as exc:
        log.debug("could not persist slot marker: %s", exc)
    return slot


def output_path(monitor_index: int, slot: str) -> Path:
    return config.STATE_DIR / config.WALLPAPER_NAME_TEMPLATE.format(index=monitor_index, slot=slot)


def apply(assignments: dict[int, Path]) -> None:
    """Assign one image per monitor and switch the layout to Fill."""
    if not assignments:
        return

    payload = [{"index": index, "path": str(path)} for index, path in sorted(assignments.items())]
    payload_file = config.STATE_DIR / "assignments.json"
    payload_file.parent.mkdir(parents=True, exist_ok=True)
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_shim(
        ["-Mode", "Set", "-Payload", str(payload_file), "-Position", str(config.WALLPAPER_POSITION_FILL)]
    )
    log.info("wallpaper %s", result or "applied")


# --------------------------------------------------------------------------- backup / restore


def capture_backup() -> None:
    """Record the wallpaper in place before we first replace it.

    Never overwrites an existing backup, so the earliest capture - the one that
    predates this tracker - is the one kept.
    """
    if config.BACKUP_FILE.exists():
        log.debug("wallpaper backup already present; leaving it untouched")
        return

    script = (
        "$p = Get-ItemProperty 'HKCU:\\Control Panel\\Desktop' "
        "-Name Wallpaper,WallpaperStyle,TileWallpaper -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{Wallpaper=$p.Wallpaper;WallpaperStyle=$p.WallpaperStyle;"
        "TileWallpaper=$p.TileWallpaper} | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            log.warning("could not capture wallpaper backup")
            return
        data = json.loads(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.warning("could not capture wallpaper backup: %s", exc)
        return

    from datetime import datetime, timezone

    data["CapturedAt"] = datetime.now(timezone.utc).isoformat()
    config.BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.BACKUP_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
    log.info("captured wallpaper backup -> %s", config.BACKUP_FILE)


def restore() -> bool:
    """Put the original wallpaper back."""
    backup = config.BACKUP_FILE
    if not backup.is_file():
        log.error("no backup at %s", backup)
        return False

    try:
        # utf-8-sig so a hand-edited file with a BOM still parses.
        data = json.loads(backup.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("could not read backup: %s", exc)
        return False

    original = data.get("Wallpaper")
    if not original or not Path(original).is_file():
        log.error("backed-up wallpaper is missing: %s", original)
        return False

    position = STYLE_TO_POSITION.get(str(data.get("WallpaperStyle", "10")), 4)
    if str(data.get("WallpaperStyle")) == "0" and str(data.get("TileWallpaper")) == "1":
        position = 1

    _run_shim(["-Mode", "Restore", "-Path", original, "-Position", str(position)])
    log.info("restored original wallpaper: %s", original)
    return True


