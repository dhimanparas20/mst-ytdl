"""
Redis helpers shared by services.

    validate_redis_connection   structural URL check + PING probe
    encode_hash / decode_hash   JSON-per-field hash (de)serialisation

Why JSON per field: records are stored as Redis hashes so a worker can patch
``percent`` or ``speed`` with one HSET without rewriting (and racing over) the
whole record — while every value still round-trips its Python type
(None, bool, float, list, dict) through ``json``.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

import redis

_REDIS_URL_SCHEMES = ("redis", "rediss", "unix")


def validate_redis_connection(url: str, timeout: float = 5.0) -> None:
    """
    Validate a Redis URL and verify connectivity with a PING.

    Raises:
        ValueError: empty / malformed URL or unsupported scheme.
        ConnectionError: server unreachable.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Redis connection URL cannot be empty.")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _REDIS_URL_SCHEMES:
        raise ValueError(
            f"Unsupported Redis URL scheme {parsed.scheme!r}; expected one of {_REDIS_URL_SCHEMES}."
        )
    if parsed.scheme != "unix" and not parsed.hostname:
        raise ValueError(f"Redis connection URL is missing a host: {url!r}")

    probe = redis.Redis.from_url(
        url, decode_responses=True, socket_connect_timeout=timeout, socket_timeout=timeout
    )
    try:
        probe.ping()
    except redis.exceptions.RedisError as exc:
        raise ConnectionError(f"Could not connect to Redis at {url!r}: {exc}") from exc
    finally:
        probe.close()


def encode_hash(mapping: Mapping[str, Any]) -> dict[str, str]:
    """``{"a": None, "b": 1.5}`` → ``{"a": "null", "b": "1.5"}`` (HSET-ready)."""
    return {k: json.dumps(v, ensure_ascii=False) for k, v in mapping.items()}


def decode_hash(raw: Mapping[str, str] | None) -> dict[str, Any]:
    """Inverse of :func:`encode_hash`; tolerant of non-JSON legacy values."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = json.loads(v)
        except (TypeError, ValueError):
            out[k] = v
    return out
