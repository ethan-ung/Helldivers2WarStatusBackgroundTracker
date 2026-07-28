"""HTTP client for the community Helldivers 2 API.

Uses only the standard library (``requests`` is not installed and is not needed).

Two endpoints cover everything this tracker renders:

* ``/api/v1/campaigns`` - embeds the full planet object for every active campaign,
  including biome, health, event, owner and player count.
* ``/api/v1/assignments`` - the Major Order, when one is active.

That is 2 requests per cycle against a 5-request/10-second budget.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from . import config

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """Raised when the API could not be reached or returned unusable data."""


@dataclass(frozen=True)
class ApiResponse:
    payload: Any
    server_time: datetime | None


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": config.API_LANGUAGE,
        "X-Super-Client": config.API_CLIENT_NAME,
        "X-Super-Contact": config.API_CONTACT,
        "User-Agent": f"{config.API_CLIENT_NAME}/1.0 (+{config.API_CONTACT})",
    }


def _parse_server_time(raw_date: str | None) -> datetime | None:
    """Parse the HTTP ``Date`` header into an aware UTC datetime.

    This is the authoritative clock for the tracker. The ``now`` field on
    ``/api/v1/war`` is unusable - it reports a 1972 timestamp.
    """
    if not raw_date:
        return None
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get(path: str) -> ApiResponse:
    """GET a single endpoint with retries and rate-limit awareness."""
    url = f"{config.API_BASE}{path}"
    request = urllib.request.Request(url, headers=_headers(), method="GET")

    last_error: Exception | None = None
    for attempt in range(config.HTTP_RETRIES):
        if attempt:
            delay = config.HTTP_BACKOFF_BASE**attempt
            log.debug("retrying %s in %.1fs (attempt %d)", path, delay, attempt + 1)
            time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
                body = response.read().decode("utf-8")
                server_time = _parse_server_time(response.headers.get("Date"))
                remaining = response.headers.get("X-RateLimit-Remaining")
                if remaining is not None:
                    log.debug("%s ok, rate-limit remaining=%s", path, remaining)
                return ApiResponse(json.loads(body), server_time)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 10.0
                log.warning("rate limited on %s, waiting %.0fs", path, wait)
                time.sleep(wait)
            elif 400 <= exc.code < 500 and exc.code != 408:
                # Client errors will not resolve by retrying.
                raise ApiError(f"{path} returned HTTP {exc.code}") from exc
            else:
                log.warning("%s returned HTTP %s", path, exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            log.warning("%s failed: %s", path, exc)
        except json.JSONDecodeError as exc:
            last_error = exc
            log.warning("%s returned malformed JSON: %s", path, exc)

    raise ApiError(f"{path} failed after {config.HTTP_RETRIES} attempts: {last_error}")


def fetch_planet_names(max_age_days: int = 7) -> dict[int, str]:
    """Index -> name for every planet, cached on disk.

    Only needed to label Major Order objectives that reference planets outside
    the active campaigns. Planet names never change, so the full 280 KB
    ``/api/v1/planets`` payload is fetched at most once a week.
    """
    cache_path = config.STATE_DIR / "planet_names.json"

    if cache_path.exists():
        try:
            age = time.time() - cache_path.stat().st_mtime
            if age < max_age_days * 86400:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return {int(k): str(v) for k, v in cached.items()}
        except (OSError, ValueError, json.JSONDecodeError):
            log.debug("planet name cache unreadable, refetching")

    try:
        response = _get("/api/v1/planets")
    except ApiError as exc:
        log.warning("could not refresh planet names: %s", exc)
        return {}

    names: dict[int, str] = {}
    for planet in response.payload if isinstance(response.payload, list) else []:
        if not isinstance(planet, dict):
            continue
        index, name = planet.get("index"), planet.get("name")
        if isinstance(index, int) and isinstance(name, str):
            names[index] = name

    if names:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps({str(k): v for k, v in names.items()}), encoding="utf-8")
            temp.replace(cache_path)
        except OSError as exc:
            log.debug("could not persist planet name cache: %s", exc)

    return names


def fetch_war_state() -> tuple[list[dict], list[dict], datetime]:
    """Fetch campaigns and assignments.

    Returns ``(campaigns, assignments, server_time)``. ``server_time`` falls back
    to local UTC if the ``Date`` header was missing or unparseable.
    """
    campaigns_response = _get("/api/v1/campaigns")
    time.sleep(config.INTER_REQUEST_DELAY)

    # A missing Major Order is normal, and must never take the whole cycle down.
    try:
        assignments_response = _get("/api/v1/assignments")
        assignments = assignments_response.payload
    except ApiError as exc:
        log.warning("assignments unavailable, continuing without a Major Order: %s", exc)
        assignments = []

    campaigns = campaigns_response.payload
    if not isinstance(campaigns, list):
        raise ApiError("campaigns endpoint did not return a list")
    if not isinstance(assignments, list):
        assignments = []

    server_time = campaigns_response.server_time or datetime.now(timezone.utc)
    return campaigns, assignments, server_time
