"""
Dispatcher seam between services and the task queue.

Layering (AGENTS.md §2) forbids services from importing ``tasks`` — yet the
manager must enqueue work. ``tasks/`` registers a Huey-backed dispatcher at
import time; tests register a recording fake. Services only ever call
``get_dispatcher()``.
"""

from __future__ import annotations

from typing import Protocol


class Dispatcher(Protocol):
    """What the manager needs from a queue."""

    def enqueue_resolve(self, job_id: str) -> None:
        """Expand a playlist job into items."""

    def enqueue_item(self, item_id: str, token: str, delay: int = 0) -> None:
        """Run one item's download (``delay`` seconds from now)."""

    def enqueue_delivery(self, job_id: str) -> None:
        """Server mode: build the job's file / ZIP once it has finished (e.g. after a cancel)."""


_dispatcher: Dispatcher | None = None


def set_dispatcher(dispatcher: Dispatcher | None) -> None:
    """Register (or clear) the active dispatcher."""
    global _dispatcher
    _dispatcher = dispatcher


def get_dispatcher() -> Dispatcher:
    """Return the active dispatcher.

    Raises:
        RuntimeError: nothing registered — ``import tasks`` first.
    """
    if _dispatcher is None:
        raise RuntimeError("No dispatcher registered — import `tasks` before using the manager")
    return _dispatcher
