"""
Huey tasks — THIN orchestration only.

A task unpacks its arguments and delegates; ALL logic lives in ``services/``
(dependencies point downward: api → tasks → services → configs/modules/models).

    resolve_job_task     expand a playlist job into items
    download_item_task   run one item (the runner waits for a free slot itself)
    reconcile_task       every minute: heal state a crashed worker left behind
                         + publish the worker heartbeat shown by /api/health

Conventions:
    - ONE huey instance per process, defined here, re-exported by tasks/__init__.py
    - Importing this module registers the Huey-backed dispatcher with services,
      which is how the API/manager enqueue work without importing tasks
    - Tasks never raise for expected outcomes (failed/paused/cancelled are
      recorded in Redis) so Huey never retries on its own
"""

from __future__ import annotations

import threading
import time

from huey import RedisHuey, crontab

from configs import get_key_prefix, get_redis, get_redis_url, get_settings
from modules import attach_server_logs, get_logger
from services import download_manager, download_runner, set_dispatcher

logger = get_logger(__name__)

_settings = get_settings()

huey = RedisHuey(
    "mst-ytdl",
    url=get_redis_url(),
    results=False,  # state lives in our own records, not Huey's result store
    utc=False,
    immediate=_settings.task.immediate,  # tests: inline + in-memory
)

WORKER_HEARTBEAT_TTL = 150


def heartbeat_key() -> str:
    """Redis key the consumer refreshes while alive."""
    return f"{get_key_prefix()}:WORKER:HEARTBEAT"


def _beat() -> None:
    get_redis().set(heartbeat_key(), str(time.time()), ex=WORKER_HEARTBEAT_TTL)


_announced = threading.Event()


@huey.on_startup()
def _announce_worker() -> None:
    """
    Consumer boot: publish the heartbeat and repair stale records immediately.
    Huey calls this once per worker thread; only the first call does the work.
    """
    if _announced.is_set():
        return
    _announced.set()
    attach_server_logs()  # huey consumer output into logs/ as well
    try:
        _beat()
        download_manager.reconcile()
        logger.info("huey consumer started")
    except Exception:
        logger.exception("worker startup hook failed")


@huey.task(name="resolve_job")
def resolve_job_task(job_id: str) -> None:
    """Expand a playlist job (see DownloadManager.resolve_job)."""
    download_manager.resolve_job(job_id)


@huey.task(name="download_item")
def download_item_task(item_id: str, token: str) -> str:
    """Download one item; returns the outcome string (completed/paused/...)."""
    return download_runner.run(item_id, token)


@huey.periodic_task(crontab(minute="*"), name="reconcile")
def reconcile_task() -> None:
    """Heal crashed-worker leftovers; keep the heartbeat fresh."""
    _beat()
    download_manager.reconcile()


class HueyDispatcher:
    """Implements ``services.dispatch.Dispatcher`` on top of Huey."""

    def enqueue_resolve(self, job_id: str) -> None:
        resolve_job_task(job_id)

    def enqueue_item(self, item_id: str, token: str, delay: int = 0) -> None:
        if delay > 0:
            download_item_task.schedule(args=(item_id, token), delay=delay)
        else:
            download_item_task(item_id, token)


set_dispatcher(HueyDispatcher())
