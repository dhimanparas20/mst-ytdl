"""
AnalyticsService — what is happening, for the /admin dashboard.

Cheap Redis counters, written fire-and-forget (a failed write never breaks a request):

    P:STAT:D:<yyyymmdd>        HASH        counters per day (submitted, completed, failed, bytes, searches …)
    P:STAT:H:<yyyymmddhh>      HASH        completed / failed / bytes per hour (last 72 h)
    P:STAT:FMT:<yyyymmdd>      HASH        completed downloads per output format
    P:STAT:VIS:<yyyymmdd>      HyperLogLog unique visitors per day
    P:STAT:TOPV:<yyyymmdd>     ZSET        most downloaded titles
    P:STAT:TOPQ:<yyyymmdd>     ZSET        most searched queries
    P:STAT:ERRORS              LIST        last 200 failures (JSON: time, title, error)
    P:VISITORS                 ZSET        visitor id → last seen (server mode)
    P:VISITOR:<id>             HASH        ip, user agent, first seen, requests (TTL 7 days)

Day keys expire after ``SERVER_ANALYTICS_DAYS``. Times are in ``DL_TIMEZONE``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any

from configs import get_key_prefix, get_redis, get_settings
from modules import get_logger
from modules.generics import get_tz

logger = get_logger(__name__)

_HOUR_TTL_S = 3 * 86400
_VISITOR_TTL_S = 7 * 86400
_ONLINE_S = 300  # "online" = seen within the last 5 minutes
_MAX_ERRORS = 200


def _now() -> datetime:
    return datetime.now(get_tz(get_settings().download.timezone))


class AnalyticsService:
    """Singleton: ``analytics``."""

    @property
    def _p(self) -> str:
        return f"{get_key_prefix()}:STAT"

    @property
    def _day_ttl(self) -> int:
        return get_settings().server.analytics_days * 86400 + 86400

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception:  # analytics must never break the request it observes
            logger.debug("analytics write failed", exc_info=True)

    # ── recording ───────────────────────────────────────────────────

    def count(self, name: str, n: int = 1) -> None:
        """Bump a daily counter (``submitted``, ``searches``, ``plays``, ``saves`` …)."""
        def write():
            day = f"{self._p}:D:{_now():%Y%m%d}"
            pipe = get_redis().pipeline()
            pipe.hincrby(day, name, n)
            pipe.expire(day, self._day_ttl)
            pipe.execute()
        self._safe(write)

    def search(self, query: str) -> None:
        def write():
            key = f"{self._p}:TOPQ:{_now():%Y%m%d}"
            pipe = get_redis().pipeline()
            pipe.zincrby(key, 1, query.strip().lower()[:120])
            pipe.expire(key, self._day_ttl)
            pipe.execute()
        self.count("searches")
        self._safe(write)

    def finished(self, outcome: str, *, title: str = "", fmt: str = "", size: int = 0, error: str = "",
                 cache_hit: bool = False) -> None:
        """One item settled: ``completed`` / ``failed`` / ``cancelled``."""
        def write():
            now = _now()
            day, hour = f"{self._p}:D:{now:%Y%m%d}", f"{self._p}:H:{now:%Y%m%d%H}"
            r = get_redis()
            pipe = r.pipeline()
            pipe.hincrby(day, outcome, 1)
            pipe.hincrby(hour, outcome, 1)
            if outcome == "completed":
                pipe.hincrby(day, "bytes", int(size or 0))
                pipe.hincrby(hour, "bytes", int(size or 0))
                if cache_hit:
                    pipe.hincrby(day, "cache_hits", 1)
                if fmt:
                    fkey = f"{self._p}:FMT:{now:%Y%m%d}"
                    pipe.hincrby(fkey, fmt, 1)
                    pipe.expire(fkey, self._day_ttl)
                if title:
                    tkey = f"{self._p}:TOPV:{now:%Y%m%d}"
                    pipe.zincrby(tkey, 1, title[:160])
                    pipe.expire(tkey, self._day_ttl)
            if outcome == "failed":
                pipe.lpush(f"{self._p}:ERRORS", json.dumps({"at": time.time(), "title": title[:160], "error": error[:300]}))
                pipe.ltrim(f"{self._p}:ERRORS", 0, _MAX_ERRORS - 1)
            pipe.expire(day, self._day_ttl)
            pipe.expire(hour, _HOUR_TTL_S)
            pipe.execute()
        self._safe(write)

    def seen(self, visitor: str, ip: str, user_agent: str) -> None:
        """A visitor made a request (server mode; called by the visitor middleware)."""
        def write():
            now = time.time()
            r = get_redis()
            key = f"{get_key_prefix()}:VISITOR:{visitor}"
            pipe = r.pipeline()
            pipe.zadd(f"{get_key_prefix()}:VISITORS", {visitor: now})
            pipe.hsetnx(key, "first", now)
            pipe.hset(key, mapping={"ip": ip, "ua": (user_agent or "")[:200], "last": now})
            pipe.hincrby(key, "requests", 1)
            pipe.expire(key, _VISITOR_TTL_S)
            day = f"{self._p}:VIS:{_now():%Y%m%d}"
            pipe.pfadd(day, visitor)
            pipe.expire(day, self._day_ttl)
            pipe.execute()
        self._safe(write)

    # ── reading ─────────────────────────────────────────────────────

    def overview(self, days: int = 14) -> dict[str, Any]:
        """Series and top lists for the dashboard."""
        r = get_redis()
        now = _now()
        day_names = [(now - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        pipe = r.pipeline(transaction=False)
        for d in day_names:
            pipe.hgetall(f"{self._p}:D:{d:%Y%m%d}")
            pipe.pfcount(f"{self._p}:VIS:{d:%Y%m%d}")
        raw = pipe.execute()
        daily = []
        for i, d in enumerate(day_names):
            h, visitors = raw[i * 2], raw[i * 2 + 1]
            daily.append({"day": f"{d:%Y-%m-%d}", "visitors": int(visitors or 0),
                          **{k: int(v) for k, v in (h or {}).items()}})
        hours = [(now - timedelta(hours=i)) for i in range(23, -1, -1)]
        pipe = r.pipeline(transaction=False)
        for h in hours:
            pipe.hgetall(f"{self._p}:H:{h:%Y%m%d%H}")
        hourly = [{"hour": f"{h:%H}:00", **{k: int(v) for k, v in (x or {}).items()}} for h, x in zip(hours, pipe.execute())]
        today = f"{now:%Y%m%d}"
        formats = {k: int(v) for k, v in r.hgetall(f"{self._p}:FMT:{today}").items()}
        top_videos = [{"title": t, "count": int(c)} for t, c in r.zrevrange(f"{self._p}:TOPV:{today}", 0, 9, withscores=True)]
        top_searches = [{"query": q, "count": int(c)} for q, c in r.zrevrange(f"{self._p}:TOPQ:{today}", 0, 9, withscores=True)]
        errors = []
        for raw_err in r.lrange(f"{self._p}:ERRORS", 0, 19):
            try:
                errors.append(json.loads(raw_err))
            except ValueError:
                continue
        online = r.zcount(f"{get_key_prefix()}:VISITORS", time.time() - _ONLINE_S, "+inf")
        return {"daily": daily, "hourly": hourly, "formats": formats, "top_videos": top_videos,
                "top_searches": top_searches, "errors": errors, "online": int(online), "today": daily[-1]}

    def visitors(self, limit: int = 200) -> list[dict[str, Any]]:
        """Most recently seen visitors with their address, browser and request count."""
        r = get_redis()
        ids = r.zrevrange(f"{get_key_prefix()}:VISITORS", 0, limit - 1, withscores=True)
        pipe = r.pipeline(transaction=False)
        for vid, _ in ids:
            pipe.hgetall(f"{get_key_prefix()}:VISITOR:{vid}")
        out = []
        for (vid, last), info in zip(ids, pipe.execute()):
            if not info:
                continue
            out.append({
                "id": vid, "short": vid[:8], "ip": info.get("ip", ""), "ua": info.get("ua", ""),
                "first": float(info.get("first") or last), "last": last, "requests": int(info.get("requests") or 0),
                "online": time.time() - last < _ONLINE_S,
            })
        return out

    def clear(self) -> int:
        """Drop all statistics (not the visitors list)."""
        r = get_redis()
        keys = list(r.scan_iter(f"{self._p}:*", count=500))
        if keys:
            r.delete(*keys)
        return len(keys)


analytics = AnalyticsService()
