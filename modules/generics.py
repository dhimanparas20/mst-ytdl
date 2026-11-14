"""Small, project-agnostic helpers: time, sizes, durations, ids."""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
_SIZE_MULT = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


@lru_cache(maxsize=16)
def get_tz(name: str) -> ZoneInfo:
    """Cached ``ZoneInfo`` (falls back to UTC for unknown names)."""
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def now_tz(tz: str = "UTC") -> datetime:
    """Current time in the named timezone."""
    return datetime.now(get_tz(tz))


def today_str(tz: str = "UTC", fmt: str = "%Y-%m-%d") -> str:
    """Today's date in ``tz`` formatted with ``fmt`` (folder names etc.)."""
    return now_tz(tz).strftime(fmt)


def now_iso(tz: str = "UTC") -> str:
    """ISO-8601 timestamp in ``tz``."""
    return now_tz(tz).isoformat()


def get_timestamp() -> float:
    """Epoch seconds (float)."""
    return time.time()


def new_id() -> str:
    """Short unique id (12 hex chars) — enough for a personal queue, URL-safe."""
    return uuid.uuid4().hex[:12]


def parse_size(value: str | int | float) -> int:
    """
    Parse a human size into bytes.

    Args:
        value: ``2048``, ``"500K"``, ``"2M"``, ``"1.5GB"``.

    Returns:
        Size in bytes.

    Raises:
        ValueError: when the text is not a size.
    """
    if isinstance(value, (int, float)):
        return int(value)
    m = _SIZE_RE.match(str(value))
    if not m:
        raise ValueError(f"Invalid size: {value!r} (use e.g. 500K, 2M)")
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).lower()])


def human_bytes(n: float | int | None) -> str:
    """``1536`` → ``'1.5 KiB'``."""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def human_duration(seconds: float | int | None) -> str:
    """``3725`` → ``'1:02:05'``; ``65`` → ``'1:05'``."""
    if seconds is None:
        return "-"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
