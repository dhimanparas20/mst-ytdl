"""Server-Sent Events: live queue snapshots for the UI."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from api.deps import Owner
from modules import get_logger
from services import download_manager

router = APIRouter(tags=["events"])
logger = get_logger(__name__)

POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15.0


def _frame(event: str, payload: str) -> str:
    return f"event: {event}\ndata: {payload}\n\n"


async def _stream(request: Request, owner: str | None = None):
    """
    Yield a ``snapshot`` frame whenever the queue changed, a comment keep-alive
    otherwise. Identical consecutive snapshots are suppressed, so an idle
    queue costs one tiny comment every 15 s.
    """
    yield "retry: 2000\n\n"
    last: str | None = None
    last_sent = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        try:
            snap = (lambda: download_manager.snapshot(owner=owner)) if owner else download_manager.snapshot
            views, stats = await run_in_threadpool(snap)
            payload = json.dumps(
                {
                    "jobs": [v.model_dump(mode="json") for v in views],
                    "stats": stats.model_dump(mode="json"),
                },
                separators=(",", ":"),
            )
            if payload != last:
                last = payload
                last_sent = time.monotonic()
                yield _frame("snapshot", payload)
            elif time.monotonic() - last_sent > KEEPALIVE_SECONDS:
                last_sent = time.monotonic()
                yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Redis hiccup — tell the UI, keep the stream alive
            logger.warning(f"snapshot failed: {exc}")
            yield _frame("problem", json.dumps({"error": str(exc)}))
            await asyncio.sleep(2.0)
        await asyncio.sleep(POLL_SECONDS)


@router.get("/events")
async def events(request: Request, who: Owner) -> StreamingResponse:
    """``text/event-stream`` of queue snapshots (``snapshot``) and problems (``problem``); server mode: your queue only."""
    return StreamingResponse(
        _stream(request, who),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
