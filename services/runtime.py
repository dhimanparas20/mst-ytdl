"""
Runtime controls shared by the API and the workers.

RuntimeService  settings the UI can change without a restart (Redis hash
                ``P:RUNTIME``): the soft parallel-download limit and a
                per-download speed cap.
SlotLimiter     cross-process semaphore enforcing that limit. Huey's consumer
                thread count is the HARD ceiling; the limiter is the adjustable
                soft limit below it.

Slot design: a ZSET ``P:SLOTS`` (member = item id, score = acquire time) plus a
per-slot heartbeat key ``P:SLOT:<item>`` with a short TTL. A slot whose key
expired belongs to a dead worker and is reclaimed on the next acquire — so a
crashed consumer can never wedge the queue.
"""

from __future__ import annotations

import time
from typing import Any

from configs import get_key_prefix, get_redis, get_settings
from models import RuntimeSettings
from modules import human_bytes
from modules.redis_util import decode_hash, encode_hash
from modules.ytdlp_util import sanitize_relpath
from services.errors import InvalidRequestError
from services.ytdl_opts import speed_ceiling

SLOT_TTL_S = 45  # heartbeat window; workers refresh every ~10s


class RuntimeService:
    """Read/write runtime-adjustable settings. Singleton: ``runtime_service``."""

    @staticmethod
    def clean_folder(value: str | None) -> str | None:
        """Sanitise a user-chosen base folder (relative, no traversal, no hidden/dot segments)."""
        clean = sanitize_relpath(value, fallback="")
        parts = [p for p in clean.split("/") if p and not p.startswith(".")]
        return "/".join(parts) or None

    @property
    def key(self) -> str:
        return f"{get_key_prefix()}:RUNTIME"

    def get(self) -> RuntimeSettings:
        """Stored values layered over the .env defaults, clamped to the cap."""
        s = get_settings()
        stored = decode_hash(get_redis().hgetall(self.key))
        cap = s.task.max_parallel_cap
        max_parallel = int(stored.get("max_parallel") or s.task.max_parallel)
        speed = stored["speed_limit"] if "speed_limit" in stored else s.download.speed_limit
        folder = stored["default_folder"] if "default_folder" in stored else s.download.default_folder
        ceiling = speed_ceiling()
        return RuntimeSettings(
            max_parallel=max(1, min(max_parallel, cap)),
            speed_limit=(min(speed, ceiling) if speed and ceiling else speed) or None,
            default_folder=self.clean_folder(folder),
            max_parallel_cap=cap,
            max_speed=ceiling,
        )

    def update(
        self, *, max_parallel: int | None = None, speed_limit: Any = "unset", default_folder: Any = "unset"
    ) -> RuntimeSettings:
        """
        Change runtime settings.

        Args:
            max_parallel: new soft limit (clamped to ``1..TASK_MAX_PARALLEL_CAP``; server mode: above it → error).
            speed_limit: bytes/sec per download; ``None`` removes the cap;
                the default sentinel leaves it unchanged. Server mode: above ``SERVER_MAX_SPEED`` → error.
            default_folder: base folder inside downloads/ (sanitised; ``None`` / ``""`` = none);
                the default sentinel leaves it unchanged.

        Raises:
            InvalidRequestError: server mode, a value above the admin's ceiling.
        """
        patch: dict[str, Any] = {}
        s = get_settings()
        if max_parallel is not None:
            cap = s.task.max_parallel_cap
            if s.server.mode and int(max_parallel) > cap:  # server mode: a hard ceiling, not a silent clamp
                raise InvalidRequestError(f"Parallel downloads are limited to {cap} by the admin")
            patch["max_parallel"] = max(1, min(int(max_parallel), cap))
        if speed_limit != "unset":
            ceiling = speed_ceiling()
            if ceiling and speed_limit and int(speed_limit) > ceiling:
                raise InvalidRequestError(f"The speed cap cannot exceed the admin's limit of {human_bytes(ceiling)}/s")
            patch["speed_limit"] = int(speed_limit) if speed_limit else None
        if default_folder != "unset":
            patch["default_folder"] = self.clean_folder(default_folder)
        if patch:
            get_redis().hset(self.key, mapping=encode_hash(patch))
        return self.get()


class SlotLimiter:
    """Atomic parallel-download slots. Singleton: ``slot_limiter``."""

    @property
    def zkey(self) -> str:
        return f"{get_key_prefix()}:SLOTS"

    def slot_key(self, item_id: str) -> str:
        return f"{get_key_prefix()}:SLOT:{item_id}"

    def acquire(self, item_id: str, limit: int) -> bool:
        """
        Try to take a slot for the holder ``item_id`` (idempotent for that holder;
        the runner passes ``"<item_id>:<run_id>"``).

        Dead holders (heartbeat key gone) are reclaimed inside the same
        transaction, so the count is always of live downloads.
        """
        r = get_redis()
        zkey = self.zkey

        def txn(pipe):
            members = pipe.zrange(zkey, 0, -1)
            alive = [m for m in members if pipe.exists(self.slot_key(m))]
            stale = [m for m in members if m not in alive]
            pipe.multi()
            if stale:
                pipe.zrem(zkey, *stale)
            if item_id in alive or len(alive) < limit:
                pipe.zadd(zkey, {item_id: time.time()}, nx=True)
                pipe.set(self.slot_key(item_id), 1, ex=SLOT_TTL_S)
                return True
            return False

        return bool(r.transaction(txn, zkey, value_from_callable=True))

    def heartbeat(self, item_id: str) -> None:
        """Keep the slot alive (call from a live worker every few seconds)."""
        get_redis().set(self.slot_key(item_id), 1, ex=SLOT_TTL_S)

    def release(self, item_id: str) -> None:
        """Give the slot back."""
        r = get_redis()
        pipe = r.pipeline()
        pipe.zrem(self.zkey, item_id)
        pipe.delete(self.slot_key(item_id))
        pipe.execute()

    def holders(self) -> list[str]:
        """Item ids that currently hold a LIVE slot."""
        r = get_redis()
        members = r.zrange(self.zkey, 0, -1)
        if not members:
            return []
        pipe = r.pipeline(transaction=False)
        for m in members:
            pipe.exists(self.slot_key(m))
        return [m for m, ok in zip(members, pipe.execute()) if ok]

    def is_alive(self, holder: str) -> bool:
        """Does this exact holder still have a live heartbeat?"""
        return bool(get_redis().exists(self.slot_key(holder)))

    def live_items(self) -> set[str]:
        """
        Item ids with at least one live holder. Holders are ``<item_id>:<run_id>``
        (one per execution attempt) so a stale task can never release the slot
        of a newer run of the same item.
        """
        return {h.split(":", 1)[0] for h in self.holders()}


runtime_service = RuntimeService()
slot_limiter = SlotLimiter()
