"""Service-layer exceptions. The API layer maps them to HTTP status codes."""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for expected, user-facing failures."""


class NotFoundError(ServiceError):
    """Job / item / file does not exist (→ 404)."""


class ConflictError(ServiceError):
    """Action not allowed in the current state (→ 409)."""


class InvalidRequestError(ServiceError):
    """Input is well-formed JSON but unacceptable (→ 422)."""


class UpstreamError(ServiceError):
    """YouTube / the CDN refused or failed (stream expired, 403, network) (→ 502)."""
