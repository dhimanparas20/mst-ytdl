"""
JobStore — Redis persistence for jobs and items.

Layout (``P`` = ``REDIS_KEY_PREFIX``, default ``YTDL``):
    P:JOB:<job_id>          HASH   DownloadJob, one JSON value per field
    P:ITEM:<item_id>        HASH   DownloadItem, one JSON value per field
    P:JOB_ITEMS:<job_id>    LIST   item ids in playlist order
    P:JOBS                  ZSET   job ids scored by created_at (newest first)

Concurrency model:
    * Records are HASHES so a worker patches ``percent``/``speed`` with one
      HSET without racing the API, which owns ``status``/``control``/``token``.
    * Any change that depends on the CURRENT status (claim, pause, resume ...)
      goes through ``transition_item`` — an optimistic WATCH/MULTI
      transaction — so exactly one actor wins a race.
    * Keys expire ``TASK_RECORD_RETENTION_DAYS`` after their last transition.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from enum import Enum
from typing import Any

from configs import get_key_prefix, get_redis, get_settings
from models import DownloadItem, DownloadJob, DownloadStatus
from modules.redis_util import decode_hash, encode_hash

Updates = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


def _plain(value: Any) -> Any:
    """Enums → their values (everything else is already JSON-friendly)."""
    return value.value if isinstance(value, Enum) else value


def _plain_map(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: _plain(v) for k, v in mapping.items()}


class JobStore:
    """Typed access to job/item records. Use the module singleton ``job_store``."""

    # ── keys ────────────────────────────────────────────────────────

    @property
    def r(self):
        return get_redis()

    @property
    def prefix(self) -> str:
        return get_key_prefix()

    def job_key(self, job_id: str) -> str:
        return f"{self.prefix}:JOB:{job_id}"

    def item_key(self, item_id: str) -> str:
        return f"{self.prefix}:ITEM:{item_id}"

    def job_items_key(self, job_id: str) -> str:
        return f"{self.prefix}:JOB_ITEMS:{job_id}"

    @property
    def jobs_index(self) -> str:
        return f"{self.prefix}:JOBS"

    @property
    def ttl(self) -> int:
        return get_settings().task.record_retention_days * 86400

    # ── jobs ────────────────────────────────────────────────────────

    def create_job(self, job: DownloadJob) -> DownloadJob:
        """Persist a new job and register it in the newest-first index."""
        now = time.time()
        job.created_at = job.created_at or now
        job.updated_at = now
        pipe = self.r.pipeline()
        pipe.hset(self.job_key(job.job_id), mapping=encode_hash(job.model_dump(mode="json")))
        pipe.expire(self.job_key(job.job_id), self.ttl)
        pipe.zadd(self.jobs_index, {job.job_id: job.created_at})
        pipe.execute()
        return job

    def get_job(self, job_id: str) -> DownloadJob | None:
        raw = self.r.hgetall(self.job_key(job_id))
        return DownloadJob.model_validate(decode_hash(raw)) if raw else None

    def get_jobs(self, job_ids: Iterable[str]) -> list[DownloadJob]:
        """Batch fetch (one round trip); missing ids are skipped."""
        ids = list(job_ids)
        if not ids:
            return []
        pipe = self.r.pipeline(transaction=False)
        for jid in ids:
            pipe.hgetall(self.job_key(jid))
        return [
            DownloadJob.model_validate(decode_hash(raw)) for raw in pipe.execute() if raw
        ]

    def update_job(self, job_id: str, **fields: Any) -> bool:
        """Patch job fields. Returns False when the job no longer exists."""
        key = self.job_key(job_id)
        if not self.r.exists(key):
            return False
        fields["updated_at"] = time.time()
        pipe = self.r.pipeline()
        pipe.hset(key, mapping=encode_hash(_plain_map(fields)))
        pipe.expire(key, self.ttl)
        pipe.execute()
        return True

    def transition_job(
        self, job_id: str, allowed_resolve: set[str], updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Atomically patch a job only if its ``resolve_state`` is in ``allowed_resolve``."""
        key = self.job_key(job_id)

        def txn(pipe):
            raw = pipe.hgetall(key)
            if not raw:
                return None
            cur = decode_hash(raw)
            if cur.get("resolve_state") not in allowed_resolve:
                return None
            patch = _plain_map({**updates, "updated_at": time.time()})
            pipe.multi()
            pipe.hset(key, mapping=encode_hash(patch))
            pipe.expire(key, self.ttl)
            return {**cur, **patch}

        return self.r.transaction(txn, key, value_from_callable=True)

    def list_job_ids(self, offset: int = 0, limit: int = 50) -> list[str]:
        """Newest-first job ids (index entries whose hash expired are pruned)."""
        ids = self.r.zrevrange(self.jobs_index, offset, offset + limit - 1)
        if not ids:
            return []
        pipe = self.r.pipeline(transaction=False)
        for jid in ids:
            pipe.exists(self.job_key(jid))
        alive = pipe.execute()
        dead = [jid for jid, ok in zip(ids, alive) if not ok]
        if dead:
            self.r.zrem(self.jobs_index, *dead)
        return [jid for jid, ok in zip(ids, alive) if ok]

    def count_jobs(self) -> int:
        return int(self.r.zcard(self.jobs_index))

    def delete_job(self, job_id: str) -> int:
        """Remove a job with all its items. Returns how many items were removed."""
        item_ids = self.r.lrange(self.job_items_key(job_id), 0, -1)
        pipe = self.r.pipeline()
        for iid in item_ids:
            pipe.delete(self.item_key(iid))
        pipe.delete(self.job_items_key(job_id))
        pipe.delete(self.job_key(job_id))
        pipe.zrem(self.jobs_index, job_id)
        pipe.execute()
        return len(item_ids)

    # ── items ───────────────────────────────────────────────────────

    def add_items(self, items: list[DownloadItem]) -> None:
        """Persist items and append their ids to the job's ordered list."""
        if not items:
            return
        now = time.time()
        pipe = self.r.pipeline()
        for it in items:
            it.created_at = it.created_at or now
            it.updated_at = now
            pipe.hset(self.item_key(it.item_id), mapping=encode_hash(it.model_dump(mode="json")))
            pipe.expire(self.item_key(it.item_id), self.ttl)
            pipe.rpush(self.job_items_key(it.job_id), it.item_id)
        pipe.expire(self.job_items_key(items[0].job_id), self.ttl)
        pipe.execute()

    def get_item(self, item_id: str) -> DownloadItem | None:
        raw = self.r.hgetall(self.item_key(item_id))
        return DownloadItem.model_validate(decode_hash(raw)) if raw else None

    def get_item_ids(self, job_id: str) -> list[str]:
        return self.r.lrange(self.job_items_key(job_id), 0, -1)

    def get_items(self, job_id: str) -> list[DownloadItem]:
        """All items of a job in playlist order."""
        return self.get_items_many([job_id]).get(job_id, [])

    def get_items_many(self, job_ids: list[str]) -> dict[str, list[DownloadItem]]:
        """Items of many jobs in two round trips (id lists, then every hash)."""
        if not job_ids:
            return {}
        pipe = self.r.pipeline(transaction=False)
        for jid in job_ids:
            pipe.lrange(self.job_items_key(jid), 0, -1)
        id_lists = pipe.execute()

        pipe = self.r.pipeline(transaction=False)
        flat: list[tuple[str, str]] = []
        for jid, ids in zip(job_ids, id_lists):
            for iid in ids:
                flat.append((jid, iid))
                pipe.hgetall(self.item_key(iid))
        raws = pipe.execute() if flat else []

        out: dict[str, list[DownloadItem]] = {jid: [] for jid in job_ids}
        for (jid, _iid), raw in zip(flat, raws):
            if raw:
                out[jid].append(DownloadItem.model_validate(decode_hash(raw)))
        return out

    def patch_item(self, item_id: str, **fields: Any) -> bool:
        """
        Cheap field patch (progress, metadata). Does NOT guard on status — use
        ``transition_item`` for anything that depends on the current state.
        """
        key = self.item_key(item_id)
        fields["updated_at"] = time.time()
        pipe = self.r.pipeline(transaction=False)
        pipe.exists(key)
        pipe.hset(key, mapping=encode_hash(_plain_map(fields)))
        exists, _ = pipe.execute()
        if not exists:
            self.r.delete(key)  # HSET resurrected a deleted item — undo
            return False
        return True

    def get_control_state(self, item_id: str) -> dict[str, Any] | None:
        """``{"control", "token", "status"}`` or None when the item is gone."""
        vals = self.r.hmget(self.item_key(item_id), "control", "token", "status")
        if vals[2] is None:
            return None
        control, token, status = (decode_hash({"v": v}).get("v") for v in vals)
        return {"control": control, "token": token, "status": status}

    def transition_item(
        self,
        item_id: str,
        allowed: Iterable[DownloadStatus],
        updates: Updates,
        *,
        expect_token: str | None = None,
    ) -> DownloadItem | None:
        """
        Atomically move an item to a new state.

        Applies ``updates`` only when the item's current status is in
        ``allowed`` (and its token equals ``expect_token`` when given).
        ``updates`` may be a dict or ``callable(current_dict) -> dict`` for
        read-modify-write values (e.g. ``attempts + 1``).

        Returns:
            The updated item, or None when the guard failed / item is gone.
        """
        key = self.item_key(item_id)
        allowed_values = {s.value for s in allowed}

        def txn(pipe):
            raw = pipe.hgetall(key)
            if not raw:
                return None
            cur = decode_hash(raw)
            if cur.get("status") not in allowed_values:
                return None
            if expect_token is not None and cur.get("token") != expect_token:
                return None
            patch = updates(cur) if callable(updates) else dict(updates)
            patch = _plain_map({**patch, "updated_at": time.time()})
            pipe.multi()
            pipe.hset(key, mapping=encode_hash(patch))
            pipe.expire(key, self.ttl)
            return {**cur, **patch}

        merged = self.r.transaction(txn, key, value_from_callable=True)
        return DownloadItem.model_validate(merged) if merged else None

    def delete_item(self, item_id: str, job_id: str) -> None:
        pipe = self.r.pipeline()
        pipe.delete(self.item_key(item_id))
        pipe.lrem(self.job_items_key(job_id), 0, item_id)
        pipe.execute()


job_store = JobStore()
