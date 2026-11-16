"""
WebhookService — one HTTP POST when a job finishes (``DL_WEBHOOK_URL``, both modes).

Called from the places where an item settles (the runner, direct cancels in the manager, a failed
playlist expansion). :meth:`notify_job` re-derives the job status and only fires when it is terminal
(completed / failed / partial / cancelled). Each *finish* is announced once: the key
``P:WEBHOOK:<job>:<sha1(status + latest finished_at)>`` is claimed with ``SET NX`` (TTL = record retention),
so two items settling at the same moment, or a repeated ``_after_settle``, send one request, while a retry that
finishes again later sends a new one.

Delivery is fire-and-forget on a daemon thread with a short timeout; nothing here ever raises.

Body (JSON)::

    {"event": "job.completed", "job_id": "…", "title": "…", "status": "completed", "kind": "single",
     "url": "…", "total_items": 1, "completed": 1, "failed": 0, "cancelled": 0, "folder": "…",
     "error": null, "finished_at": 1700000000.0}

The ``Title`` header (RFC 2047 encoded when needed) and ``Tags`` header make an `ntfy <https://ntfy.sh>`_ topic URL work too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.request
from typing import Any

from configs import get_key_prefix, get_redis, get_settings
from models import DownloadStatus, JobView
from modules import get_logger
from services.store import job_store

logger = get_logger(__name__)

_TIMEOUT_S = 10
_TAGS = {
    DownloadStatus.COMPLETED: "white_check_mark",
    DownloadStatus.PARTIAL: "warning",
    DownloadStatus.FAILED: "x",
    DownloadStatus.CANCELLED: "stop_button",
}


def _header_text(text: str) -> str:
    """A header-safe title: single line, ≤120 chars, RFC 2047 base64 when it is not plain ASCII."""
    one = " ".join(str(text or "").split())[:120] or "mst-ytdl"
    if one.isascii():
        return one
    return "=?UTF-8?B?" + base64.b64encode(one.encode()).decode() + "?="


class WebhookService:
    """Singleton: ``webhook_service``."""

    @property
    def url(self) -> str | None:
        return get_settings().download.webhook_url

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    # ── payload ─────────────────────────────────────────────────────

    @staticmethod
    def payload(view: JobView, finished_at: float) -> dict[str, Any]:
        """The JSON body for a terminal job view."""
        return {
            "event": f"job.{view.status.value}",
            "job_id": view.job_id,
            "title": view.title,
            "status": view.status.value,
            "kind": view.kind.value,
            "url": view.url,
            "total_items": view.total_items,
            "completed": view.counts.get(DownloadStatus.COMPLETED.value, 0),
            "failed": view.counts.get(DownloadStatus.FAILED.value, 0),
            "cancelled": view.counts.get(DownloadStatus.CANCELLED.value, 0),
            "folder": view.folder,
            "error": view.resolve_error,
            "finished_at": finished_at,
        }

    # ── entry point ─────────────────────────────────────────────────

    def notify_job(self, job_id: str) -> bool:
        """
        Announce the job when it is finished and this finish was not announced yet.

        Never raises.

        Returns:
            True when a request was handed to the sender.
        """
        if not self.enabled:
            return False
        try:
            job = job_store.get_job(job_id)
            if job is None:
                return False
            items = job_store.get_items(job_id)
            view = JobView.build(job, items)
            if not view.status.is_terminal:
                return False
            finished_at = max([i.finished_at or 0.0 for i in items] + [job.updated_at or 0.0])
            sig = hashlib.sha1(f"{view.status.value}|{finished_at}".encode()).hexdigest()[:16]
            claimed = get_redis().set(
                f"{get_key_prefix()}:WEBHOOK:{job_id}:{sig}", "1", nx=True, ex=get_settings().task.record_retention_days * 86400
            )
            if not claimed:
                return False
            self._dispatch(self.url, self.payload(view, finished_at))  # type: ignore[arg-type]
            return True
        except Exception:
            logger.warning("webhook notify failed for job %s", job_id, exc_info=True)
            return False

    # ── sending ─────────────────────────────────────────────────────

    def _dispatch(self, url: str, payload: dict[str, Any]) -> None:
        """Send on a daemon thread so a slow endpoint never blocks a worker."""
        threading.Thread(target=self._post, args=(url, payload), name="webhook", daemon=True).start()

    @staticmethod
    def _post(url: str, payload: dict[str, Any]) -> bool:
        """POST the JSON body; returns success and swallows every error (logged at warning level)."""
        try:
            status = DownloadStatus(payload["status"])
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "mst-ytdl",
                "Title": _header_text(payload.get("title") or ""),
                "Tags": _TAGS.get(status, "inbox_tray"),
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 — operator-configured URL
                resp.read(1024)
            return True
        except Exception as exc:
            logger.warning("webhook POST failed: %s", type(exc).__name__)
            return False


webhook_service = WebhookService()
