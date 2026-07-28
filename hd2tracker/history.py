"""Rolling sample history, used to derive liberation rate in %/hour.

A single delta between two five-minute samples is very noisy on slow-moving
planets - a 0.02% tick becomes a swingy 0.24%/hour. Instead we keep a window of
recent samples and fit a least-squares line through them.

We deliberately report *no* rate until there is enough history to mean anything,
rather than printing a confident-looking 0.0.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# A campaign that ends and restarts resets progress to zero. Fitting across that
# discontinuity would report a wildly negative rate, so we drop the old samples.
RESET_DROP_THRESHOLD = 20.0


class History:
    def __init__(self, samples: dict[int, list[tuple[datetime, float]]] | None = None):
        self.samples: dict[int, list[tuple[datetime, float]]] = samples or {}
        self.previous_order: list[int] = []
        self.previous_top_index: int | None = None

    # ----------------------------------------------------------------- persistence

    @classmethod
    def load(cls, path: Path | None = None) -> "History":
        path = path or config.HISTORY_FILE
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read history (%s); starting fresh", exc)
            return cls()

        if raw.get("version") != SCHEMA_VERSION:
            # Rates are cheap to rebuild, so an unrecognised schema is simply
            # discarded rather than migrated.
            log.info("history schema %s is not v%d; starting fresh", raw.get("version"), SCHEMA_VERSION)
            return cls()

        samples: dict[int, list[tuple[datetime, float]]] = {}
        for key, entries in (raw.get("planets") or {}).items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            parsed: list[tuple[datetime, float]] = []
            for entry in entries or []:
                try:
                    stamp = datetime.fromisoformat(str(entry[0]).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    parsed.append((stamp.astimezone(timezone.utc), float(entry[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            if parsed:
                samples[index] = parsed

        history = cls(samples)
        order = raw.get("order")
        if isinstance(order, list):
            history.previous_order = [int(i) for i in order if isinstance(i, int)]
        top = raw.get("topIndex")
        history.previous_top_index = top if isinstance(top, int) else None
        return history

    def save(self, path: Path | None = None) -> None:
        path = path or config.HISTORY_FILE
        payload = {
            "version": SCHEMA_VERSION,
            "planets": {
                str(index): [[stamp.isoformat().replace("+00:00", "Z"), round(value, 4)] for stamp, value in entries]
                for index, entries in self.samples.items()
            },
            "order": self.previous_order,
            "topIndex": self.previous_top_index,
        }
        _atomic_write_json(path, payload)

    # ----------------------------------------------------------------- sampling

    def record(self, index: int, when: datetime, value: float) -> None:
        entries = self.samples.setdefault(index, [])

        if entries and value < entries[-1][1] - RESET_DROP_THRESHOLD:
            log.info("planet %s progress dropped %.1f -> %.1f; resetting history", index, entries[-1][1], value)
            entries.clear()

        # Guard against a duplicate cycle (same timestamp) skewing the fit.
        if entries and abs((when - entries[-1][0]).total_seconds()) < 1.0:
            entries[-1] = (when, value)
        else:
            entries.append((when, value))

        cutoff = when - timedelta(minutes=config.HISTORY_WINDOW_MINUTES)
        trimmed = [entry for entry in entries if entry[0] >= cutoff]
        if len(trimmed) > config.HISTORY_MAX_SAMPLES:
            trimmed = trimmed[-config.HISTORY_MAX_SAMPLES :]
        self.samples[index] = trimmed

    def prune(self, keep: set[int]) -> None:
        """Forget planets that are no longer part of an active campaign."""
        for index in list(self.samples):
            if index not in keep:
                del self.samples[index]

    # ----------------------------------------------------------------- rates

    def rate_per_hour(self, index: int) -> float | None:
        """Least-squares slope of progress over time, in percent per hour."""
        entries = self.samples.get(index) or []
        if len(entries) < config.RATE_MIN_SAMPLES:
            return None

        span_minutes = (entries[-1][0] - entries[0][0]).total_seconds() / 60.0
        if span_minutes < config.RATE_MIN_SPAN_MINUTES:
            return None

        origin = entries[0][0]
        xs = [(stamp - origin).total_seconds() / 3600.0 for stamp, _ in entries]
        ys = [value for _, value in entries]

        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denominator = sum((x - mean_x) ** 2 for x in xs)
        if denominator <= 1e-9:
            return None

        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        return numerator / denominator


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    temp.replace(path)


def format_rate(rate: float | None) -> str:
    """Render a rate for display, or an em dash while the window fills."""
    if rate is None:
        return "—"
    if abs(rate) < 0.005:
        return "0.00 %/h"
    return f"{rate:+.2f} %/h"
