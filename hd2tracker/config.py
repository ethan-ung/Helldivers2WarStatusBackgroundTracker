"""Paths, colours, layout constants and tunables.

Everything a user might reasonably want to tweak lives here.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- paths

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "assets"
BIOME_OVERRIDE_DIR = ASSETS_DIR / "biomes"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# State lives inside the repo (git-ignored) so the project is self-contained.
# Ignored files survive branch switches, so the wallpaper paths stay valid.
STATE_DIR = Path(os.environ.get("HD2_STATE_DIR", REPO_ROOT / ".state"))
HISTORY_FILE = STATE_DIR / "history.json"
SNAPSHOT_FILE = STATE_DIR / "last_snapshot.json"
BACKUP_FILE = STATE_DIR / "wallpaper_backup.json"
DISPATCH_CACHE = STATE_DIR / "dispatch.json"
LOCK_FILE = STATE_DIR / "tracker.lock"
LOG_FILE = STATE_DIR / "tracker.log"
BIOME_CACHE_DIR = STATE_DIR / "biome_cache"

LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 2

# --------------------------------------------------------------------------- api

API_BASE = os.environ.get("HD2_API_BASE", "https://api.helldivers2.dev")
API_CLIENT_NAME = "Helldivers2WarStatusBackgroundTracker"
API_LANGUAGE = "en-US"

# The API asks clients to identify themselves so its maintainers can reach
# whoever is running a misbehaving client. Anything reachable works: an email
# address, a GitHub handle, a repository URL.
#
# It is resolved at runtime so no personal address has to live in version
# control:
#
#   1. the HD2_API_CONTACT environment variable
#   2. contact.local in the repository root, which is git-ignored
#   3. a neutral placeholder
#
# See contact.local.example.
CONTACT_FILE = REPO_ROOT / "contact.local"
UNCONFIGURED_CONTACT = f"{API_CLIENT_NAME} (contact not configured)"


def _resolve_api_contact() -> str:
    from_env = os.environ.get("HD2_API_CONTACT", "").strip()
    if from_env:
        return from_env

    try:
        content = CONTACT_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        return UNCONFIGURED_CONTACT

    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return UNCONFIGURED_CONTACT


API_CONTACT = _resolve_api_contact()

HTTP_TIMEOUT = 15.0
HTTP_RETRIES = 3
HTTP_BACKOFF_BASE = 1.5
# Rate limit is 5 requests / 10s; we make 2 per cycle, spaced to stay well clear.
INTER_REQUEST_DELAY = 0.5

# --------------------------------------------------------------------------- behaviour

PLANET_COUNT = 4

# Only consider planets with an active campaign. Planets without a campaign have
# no meaningful liberation figure, and in practice carry far fewer players.
CAMPAIGNS_ONLY = True

# History window used for the least-squares %/hour fit.
HISTORY_WINDOW_MINUTES = 90
HISTORY_MAX_SAMPLES = 24
# Below these thresholds we report no rate rather than inventing a noisy one.
RATE_MIN_SAMPLES = 2
RATE_MIN_SPAN_MINUTES = 10.0

# The busiest planet decides the backdrop. Without hysteresis the background
# flips whenever two planets trade the top spot; a challenger must clear the
# incumbent by this fraction to take over.
BACKGROUND_HYSTERESIS = True
BACKGROUND_HYSTERESIS_MARGIN = 0.05
# Card ordering is left strictly literal by default.
ORDER_HYSTERESIS = False
ORDER_HYSTERESIS_MARGIN = 0.05

# If the API is unreachable, keep re-rendering the last good snapshot but mark it
# stale once it passes this age.
STALE_AFTER_MINUTES = 20

# The dispatch feed cannot be filtered server-side and is 372 KB (154 KB gzipped)
# every time, while new dispatches appear only two or three times a day. Fetching
# it on its own throttle keeps the cost proportionate; the cached copy is reused
# by every render in between.
DISPATCH_REFRESH_MINUTES = 15
DISPATCH_BODY_LINES = 4

# --------------------------------------------------------------------------- colours

FACTION_COLORS = {
    "Humans": (59, 130, 246),       # Super Earth blue
    "Terminids": (234, 179, 8),     # yellow
    "Automaton": (220, 38, 38),     # red
    "Illuminate": (168, 85, 247),   # purple
}
FACTION_FALLBACK_COLOR = (148, 163, 184)

# The API uses "Automaton"; the community usually says "Automatons".
FACTION_DISPLAY_NAMES = {
    "Humans": "SUPER EARTH",
    "Terminids": "TERMINIDS",
    "Automaton": "AUTOMATONS",
    "Illuminate": "ILLUMINATE",
}

SUPER_EARTH_BLUE = FACTION_COLORS["Humans"]

# Panel chrome
PANEL_BG = (10, 13, 20, 208)
PANEL_BORDER = (70, 84, 104, 140)
CARD_BG = (18, 23, 33, 170)
CARD_BORDER = (52, 64, 82, 120)
TEXT_PRIMARY = (233, 238, 245)
TEXT_SECONDARY = (150, 163, 180)
TEXT_MUTED = (104, 117, 136)
TRACK_COLOR = (38, 46, 60, 255)
ACCENT_YELLOW = (250, 204, 21)
WARN_RED = (239, 68, 68)

# Dispatches read as an official broadcast, so they take Super Earth blue rather
# than competing with the Major Order's yellow.
DISPATCH_ACCENT = (59, 130, 246)

# Major Order objective tick boxes, matching the in-game HUD.
OBJECTIVE_COMPLETE = (74, 222, 128)
OBJECTIVE_BOX_EMPTY = (30, 37, 48)
OBJECTIVE_BOX_BORDER = (72, 86, 105)

# On an enemy-held planet the unfilled part of the bar is that faction's colour.
# It is drawn dimmed: several planets sit at 0.0% liberated, and a solid block of
# faction colour at full strength reads as a *full* bar rather than an empty one.
BAR_REMAINDER_ALPHA = 150

# --------------------------------------------------------------------------- layout

# The panel is vertical, so its size tracks display *height* rather than width -
# otherwise a 3440-wide ultrawide clamps out and the text ends up tiny relative
# to the screen. Width is still capped so it never dominates a wide desktop.
PANEL_REFERENCE_HEIGHT = 1080
PANEL_BASE_WIDTH = 500
# Panel width is always PANEL_BASE_WIDTH * scale, so these bounds are expressed
# through the scale rather than clamped separately - clamping width on its own
# lets the fonts and their container disagree.
PANEL_SCALE_MIN = 0.45
PANEL_SCALE_MAX = 1.45
PANEL_WIDTH_FRACTION = 0.32
PANEL_WIDTH_MAX = 700
PANEL_MARGIN = 28
PANEL_PADDING = 22
CARD_GAP = 10
# Slack from the full-height panel is absorbed by growing the card gaps, up to
# this bound; anything beyond it is left at the bottom above the footer.
CARD_GAP_MAX = 62
CARD_PADDING = 12
BAR_HEIGHT = 9
# The in-game HUD is angular, so nothing in the panel is rounded.
BAR_RADIUS = 0
CORNER_RADIUS = 0
# Major Order tick box, a square this many units on a side before scaling.
CHECKBOX_SIZE = 12
OBJECTIVE_GAP = 7

# Right-side scrim that darkens the backdrop behind the panel.
SCRIM_WIDTH_MULTIPLIER = 2.1
SCRIM_MAX_ALPHA = 232

# --------------------------------------------------------------------------- fonts

WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
FONT_DIR = WINDIR / "Fonts"

# Bahnschrift is a variable condensed face that suits the HD2 look; Segoe UI
# Semibold and Consolas are the fallbacks. All three verified present.
FONT_CANDIDATES_DISPLAY = ["bahnschrift.ttf", "seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"]
FONT_CANDIDATES_BODY = ["segoeui.ttf", "bahnschrift.ttf", "arial.ttf"]
FONT_CANDIDATES_MONO = ["consola.ttf", "cour.ttf"]

# Users may drop their own .ttf into assets/fonts/ to override the display face.
FONT_OVERRIDE_DIR = ASSETS_DIR / "fonts"

# --------------------------------------------------------------------------- wallpaper

# Windows caches wallpapers by path, so writing the same filename repeatedly can
# silently fail to refresh. Alternate between two names per monitor.
WALLPAPER_SLOTS = ("a", "b")
WALLPAPER_NAME_TEMPLATE = "hd2_mon{index}_{slot}.png"
# DESKTOP_WALLPAPER_POSITION.DWPOS_FILL
WALLPAPER_POSITION_FILL = 4

SCHEDULED_TASK_NAME = "Helldivers2Wallpaper"
UPDATE_INTERVAL_MINUTES = 5


def ensure_state_dirs() -> None:
    """Create the state directories if they do not yet exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BIOME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
