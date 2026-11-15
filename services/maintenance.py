"""
MaintenanceService — the admin's housekeeping buttons and the log viewer.

Every action is idempotent and returns a short ``{"action", "affected", "message"}`` summary.
"""

from __future__ import annotations

import shutil
import time
from collections import deque
from pathlib import Path

from configs import get_key_prefix, get_redis, get_settings
from modules import get_logger, log_dir
from services.analytics import analytics
from services.delivery import DELIVERIES_DIR, delivery_service
from services.errors import InvalidRequestError, NotFoundError
from services.manager import download_manager
from services.streaming import stream_service

logger = get_logger(__name__)

_LEVELS = ("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")


class MaintenanceService:
    """Singleton: ``maintenance``."""

    def _drop(self, pattern: str) -> int:
        r = get_redis()
        keys = list(r.scan_iter(f"{get_key_prefix()}:{pattern}", count=500))
        for i in range(0, len(keys), 500):
            r.delete(*keys[i:i + 500])
        return len(keys)

    @property
    def actions(self) -> dict[str, tuple[str, str]]:
        """name → (label, what it does) — the dashboard renders these as buttons."""
        return {
            "clear-meta-cache": ("Clear lookup cache", "Cached previews, searches and playlist reads."),
            "clear-media-cache": ("Clear media cache index", "Finished files are no longer reused for other visitors (files stay)."),
            "clear-lyrics-cache": ("Clear lyrics cache", "Found and not-found lyrics."),
            "clear-stream-cache": ("Clear player cache", "Stream URLs of the in-app player (this API process)."),
            "clear-covers": ("Clear cover cache", "Album art extracted from library files (rebuilt on demand)."),
            "sweep-now": ("Run cleanup now", "Delete expired deliveries, idle paused jobs and orphaned folders."),
            "expire-all-deliveries": ("Expire all deliveries", "Server mode: delete every prepared file / ZIP and its job now."),
            "reconcile-now": ("Recover stuck downloads", "Running items of a dead worker become Paused."),
            "clear-finished": ("Clear finished jobs", "Every finished / failed / cancelled job of every visitor."),
            "clear-visitor-cookies": ("Delete all visitor cookies", "Every visitor's own pasted YouTube cookies."),
            "clear-stats": ("Reset statistics", "All dashboard counters and charts."),
        }

    def run(self, action: str) -> dict:
        """
        Run one maintenance action.

        Raises:
            InvalidRequestError: unknown action.
        """
        if action not in self.actions:
            raise InvalidRequestError(f"Unknown action: {action}")
        n: int
        if action == "clear-meta-cache":
            n = self._drop("META:*")
        elif action == "clear-media-cache":
            n = self._drop("MEDIA:*")
        elif action == "clear-lyrics-cache":
            n = self._drop("LYRICS:*")
        elif action == "clear-stream-cache":
            n = len(stream_service._cache)
            stream_service.clear()
        elif action == "clear-covers":
            covers = get_settings().download.downloads_dir / ".covers"
            n = sum(1 for f in covers.glob("*") if f.is_file()) if covers.is_dir() else 0
            shutil.rmtree(covers, ignore_errors=True)
        elif action == "sweep-now":
            n = sum((delivery_service.sweep() or {}).values())
        elif action == "expire-all-deliveries":
            r = get_redis()
            ids = r.zrange(f"{get_key_prefix()}:DELIVERIES", 0, -1)
            for jid in ids:
                delivery_service.purge(jid)
            n = len(ids)
        elif action == "reconcile-now":
            n = download_manager.reconcile()
        elif action == "clear-finished":
            n = download_manager.clear_finished(delete_files=False)
        elif action == "clear-visitor-cookies":
            n = self._drop("VCOOKIES:*")
        else:
            n = analytics.clear()
        logger.info(f"admin maintenance: {action} ({n})")
        return {"action": action, "affected": n, "message": f"{self.actions[action][0]}: {n} affected"}

    def deliveries_usage(self) -> dict[str, int]:
        """How much the server-mode deliveries occupy right now."""
        root = get_settings().download.downloads_dir / DELIVERIES_DIR
        files = size = 0
        if root.is_dir():
            for f in root.rglob("*"):
                try:
                    if f.is_file():
                        files += 1
                        size += f.stat().st_size
                except OSError:
                    continue
        jobs = int(get_redis().zcard(f"{get_key_prefix()}:DELIVERIES"))
        return {"jobs": jobs, "files": files, "bytes": size}

    # ── dashboard ───────────────────────────────────────────────────

    def overview(self) -> dict:
        """Everything the admin dashboard's first screen shows, in one call."""
        from services.access import access_service  # local: access ↔ maintenance never import each other at load
        from services.library import library_service
        from services.runtime import runtime_service, slot_limiter

        s = get_settings()
        r = get_redis()
        stats = download_manager.stats()
        return {
            "mode": "server" if s.server.mode else "local",
            "maintenance": s.server.maintenance,
            "health": {
                "redis": bool(r.ping()),
                "worker": bool(r.exists(f"{get_key_prefix()}:WORKER:HEARTBEAT")),
                "running_slots": len(slot_limiter.holders()),
            },
            "queue": stats.model_dump(),
            "runtime": runtime_service.get().model_dump(),
            "storage": library_service.usage(),
            "deliveries": self.deliveries_usage(),
            "admin": {"sessions": access_service.active_sessions(), "login_failures": access_service.login_failures(),
                      "bans": len(access_service.bans())},
            "limits": {
                "workers": s.task.workers, "max_parallel_cap": min(s.task.max_parallel_cap, s.task.workers), "max_speed": s.server.max_speed,
                "max_active_jobs": s.server.max_active_jobs, "max_job_items": s.server.max_job_items,
                "delivery_ttl_minutes": s.server.delivery_ttl_minutes, "rate_limit_per_minute": s.server.rate_limit_per_minute,
            },
            "stats": analytics.overview(),
        }

    # ── logs ────────────────────────────────────────────────────────

    def log_files(self) -> list[dict]:
        d = Path(log_dir())
        if not d.is_dir():
            return []
        return sorted(
            ({"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime} for f in d.glob("*.log")),
            key=lambda x: x["name"],
        )

    def tail(self, name: str, lines: int = 300, level: str | None = None, query: str | None = None) -> dict:
        """
        Last lines of a log file (optionally only ``level`` and above, or containing ``query``).

        Raises:
            NotFoundError / InvalidRequestError: unknown file or a bad name.
        """
        if Path(name).name != name or not name.endswith(".log"):
            raise InvalidRequestError("Bad log name")
        f = Path(log_dir()) / name
        if not f.is_file():
            raise NotFoundError("No such log file")
        lines = max(10, min(int(lines), 2000))
        min_level = _LEVELS.index(level.upper()) if level and level.upper() in _LEVELS else None
        q = (query or "").lower()
        out: deque[str] = deque(maxlen=lines)
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if q and q not in line.lower():
                    continue
                if min_level is not None and not any(lv in line for lv in _LEVELS[min_level:]):
                    continue
                out.append(line.rstrip("\n"))
        return {"name": name, "lines": list(out), "at": time.time()}


maintenance = MaintenanceService()
