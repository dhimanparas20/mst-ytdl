"""
Password hashing with the standard library only (scrypt) — no extra dependency.

Encoded form (safe to keep in ``.env``)::

    scrypt:<n>:<r>:<p>:<salt, urlsafe base64>:<hash, urlsafe base64>

(":" not "$": docker compose would try to interpolate "$" in an env file.)

Generate one with ``uv run python main.py hash-password``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_N, _R, _P = 2**14, 8, 1
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """
    Hash a password for storage.

    Args:
        password: the plain password (any non-empty string).

    Returns:
        The encoded ``scrypt:…`` string.

    Raises:
        ValueError: empty password.
    """
    if not password:
        raise ValueError("Password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    """
    Constant-time check of ``password`` against an encoded hash.

    Returns:
        False for a wrong password, an empty one, or a malformed / missing hash.
    """
    if not password or not encoded:
        return False
    try:
        algo, n, r, p, salt, digest = encoded.strip().split(":")
        if algo != "scrypt":
            return False
        expected = _unb64(digest)
        got = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=_MAXMEM
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def is_valid_hash(encoded: str | None) -> bool:
    """Does ``encoded`` look like something :func:`hash_password` produced?"""
    if not encoded:
        return False
    parts = encoded.strip().split(":")
    return len(parts) == 6 and parts[0] == "scrypt" and all(parts[1:4]) and all(x.isdigit() for x in parts[1:4])
