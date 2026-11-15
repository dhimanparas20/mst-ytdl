"""
AccessService — who may do what when the app runs as a public server (``SERVER_MODE=true``).

    visitors      anonymous per-browser ids (cookie) that scope the queue — see the API middleware
    admin         ``SERVER_ADMIN_PASSWORD_HASH`` unlocks library edits, cookies and runtime settings;
                  a successful login creates a random session token (Redis, TTL) held in an
                  HttpOnly cookie. Failed logins are counted per IP and locked out for a while.
    rate limits   fixed one-minute windows per IP for the expensive, YouTube-touching endpoints

In personal mode (``SERVER_MODE=false``) every check here is a no-op: nothing is locked.

Keys (``P`` = prefix):
    P:ADMIN:<token>          STRING  admin session (TTL = SERVER_ADMIN_SESSION_HOURS)
    P:LOGINFAIL:<ip>         STRING  failed login counter (TTL = lockout window)
    P:RATE:<bucket>:<ip>:<m> STRING  requests in minute ``m``
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import time

from configs import get_key_prefix, get_redis, get_settings
from modules import get_logger
from modules.passwords import is_valid_hash, verify_password
from services.errors import ForbiddenError, RateLimitedError

logger = get_logger(__name__)

VISITOR_COOKIE = "ytdl_vid"
ADMIN_COOKIE = "ytdl_admin"
_VISITOR_RE = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_MAX_FAILS = 8  # wrong passwords per IP …
_LOCKOUT_S = 15 * 60  # … within this window → locked for the rest of it


class AccessService:
    """Singleton: ``access_service``."""

    # ── mode ────────────────────────────────────────────────────────

    @property
    def server_mode(self) -> bool:
        return get_settings().server.mode

    @property
    def username(self) -> str:
        return get_settings().server.admin_username

    @property
    def admin_enabled(self) -> bool:
        """A valid admin password hash is configured (the /admin dashboard login works, in either mode)."""
        return is_valid_hash(get_settings().server.admin_password_hash)

    # ── visitors ────────────────────────────────────────────────────

    @staticmethod
    def new_visitor_id() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def valid_visitor_id(value: str | None) -> bool:
        return bool(value and _VISITOR_RE.match(value))

    # ── admin sessions ──────────────────────────────────────────────

    def _session_key(self, token: str) -> str:
        return f"{get_key_prefix()}:ADMIN:{token}"

    def _fail_key(self, ip: str) -> str:
        return f"{get_key_prefix()}:LOGINFAIL:{ip}"

    def login(self, username: str, password: str, ip: str) -> str:
        """
        Check the admin password and open a session.

        Returns:
            A new session token (put it in the ``ytdl_admin`` cookie).

        Raises:
            ForbiddenError: no admin configured / wrong username or password.
            RateLimitedError: too many wrong passwords from this IP recently.
        """
        if not self.admin_enabled:
            raise ForbiddenError("Admin access is not configured on this server (SERVER_ADMIN_PASSWORD_HASH)")
        r = get_redis()
        fails = int(r.get(self._fail_key(ip)) or 0)
        if fails >= _MAX_FAILS:
            raise RateLimitedError("Too many wrong passwords — try again in 15 minutes")
        user_ok = hmac.compare_digest((username or "").strip().lower(), get_settings().server.admin_username.strip().lower())
        pw_ok = verify_password(password, get_settings().server.admin_password_hash)  # always runs: no timing hint
        if not (user_ok and pw_ok):
            pipe = r.pipeline()
            pipe.incr(self._fail_key(ip))
            pipe.expire(self._fail_key(ip), _LOCKOUT_S, nx=True)
            pipe.execute()
            logger.warning(f"admin login failed from {ip} ({fails + 1}/{_MAX_FAILS})")
            raise ForbiddenError("Wrong username or password")
        r.delete(self._fail_key(ip))
        token = secrets.token_urlsafe(32)
        r.set(self._session_key(token), str(time.time()), ex=self.session_seconds)
        logger.info(f"admin login from {ip}")
        return token

    @property
    def session_seconds(self) -> int:
        return get_settings().server.admin_session_hours * 3600

    def is_admin(self, token: str | None) -> bool:
        """Is ``token`` a live admin session?"""
        if not self.admin_enabled or not token or not _TOKEN_RE.match(token):
            return False
        return bool(get_redis().exists(self._session_key(token)))

    def logout(self, token: str | None) -> None:
        if token and _TOKEN_RE.match(token):
            get_redis().delete(self._session_key(token))

    def require_admin(self, token: str | None) -> None:
        """
        Gate for management actions.

        Raises:
            ForbiddenError: server mode and ``token`` is not an admin session.
        """
        if not self.server_mode:
            return
        if not self.is_admin(token):
            raise ForbiddenError("This needs the admin password (Settings → Admin)")

    def require_superadmin(self, token: str | None) -> None:
        """
        Gate for the /admin dashboard API.

        A configured password always needs a session. Without one: personal mode is open (your own
        machine), server mode stays locked.

        Raises:
            ForbiddenError: not allowed.
        """
        if self.admin_enabled:
            if not self.is_admin(token):
                raise ForbiddenError("Sign in to the admin dashboard")
            return
        if self.server_mode:
            raise ForbiddenError("Set SERVER_ADMIN_PASSWORD_HASH (uv run python main.py hash-password) to use the admin dashboard")

    def active_sessions(self) -> int:
        return sum(1 for _ in get_redis().scan_iter(f"{get_key_prefix()}:ADMIN:*", count=200))

    def login_failures(self) -> int:
        """IPs with recent failed admin logins."""
        return sum(1 for _ in get_redis().scan_iter(f"{get_key_prefix()}:LOGINFAIL:*", count=200))

    # ── bans (server mode) ──────────────────────────────────────────

    @property
    def _bans(self) -> str:
        return f"{get_key_prefix()}:BANS"

    def ban(self, ip: str, reason: str = "") -> None:
        if not ip or len(ip) > 64:
            raise ForbiddenError("Not a valid address")
        get_redis().hset(self._bans, ip, json.dumps({"at": time.time(), "reason": reason[:200]}))
        logger.warning(f"banned {ip}")

    def unban(self, ip: str) -> None:
        get_redis().hdel(self._bans, ip)

    def bans(self) -> list[dict]:
        out = []
        for ip, raw in get_redis().hgetall(self._bans).items():
            try:
                info = json.loads(raw)
            except ValueError:
                info = {}
            out.append({"ip": ip, "at": info.get("at"), "reason": info.get("reason", "")})
        return sorted(out, key=lambda b: -(b["at"] or 0))

    def is_banned(self, ip: str) -> bool:
        return bool(ip) and bool(get_redis().hexists(self._bans, ip))

    # ── rate limiting ───────────────────────────────────────────────

    def hit(self, bucket: str, ip: str, limit: int | None = None) -> None:
        """
        Count one request; refuse when the per-minute budget is spent (server mode only).

        Raises:
            RateLimitedError: over ``limit`` (default ``SERVER_RATE_LIMIT_PER_MINUTE``) this minute.
        """
        if not self.server_mode:
            return
        limit = limit or get_settings().server.rate_limit_per_minute
        minute = int(time.time() // 60)
        key = f"{get_key_prefix()}:RATE:{bucket}:{ip}:{minute}"
        r = get_redis()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 90)
        n = pipe.execute()[0]
        if int(n) > limit:
            raise RateLimitedError("Too many requests — slow down a little and try again in a minute")


access_service = AccessService()
