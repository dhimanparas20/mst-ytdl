"""Link a YouTube account without pasting cookies: a one-time pairing code + the browser extension.

A web page cannot read another site's cookies, and a Google OAuth app would need the operator's own
Google Cloud credentials. So the visitor signs in to YouTube in their own browser, and a small
extension (``extension/``) sends that browser's youtube/google cookies to ``POST /api/connect/<code>``.
The code is the only credential of that call: single use, ``CONNECT_TTL_S`` lifetime, bound to the
visitor (server mode) that asked for it, and rate-limited per IP by the router.

Cookies end up exactly where pasted ones do (``cookie_service.save_visitor`` in server mode,
``cookie_service.save`` in personal mode), so retention and scoping are unchanged.
"""

from __future__ import annotations

import json
import secrets

from configs import get_key_prefix, get_redis, get_settings
from modules import get_logger
from modules.cookies_util import LOGIN_NAMES, parse_netscape
from services.cookies import cookie_service
from services.errors import ForbiddenError, InvalidRequestError, NotFoundError

log = get_logger(__name__)

CONNECT_TTL_S = 600
DONE_TTL_S = 120
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L: the code is typed by hand
_LEN = 8


def normalize(code: str) -> str:
    """User input (``abcd-efgh``, spaces) → the stored form (``ABCDEFGH``)."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def pretty(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


class ConnectService:
    """Pairing codes in Redis (``P:CONNECT:<code>``)."""

    @staticmethod
    def enabled() -> bool:
        return get_settings().server.connect_enabled

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise ForbiddenError("Signing in with the browser extension is turned off on this server — paste your cookies instead.")

    @staticmethod
    def _key(code: str) -> str:
        return f"{get_key_prefix()}:CONNECT:{code}"

    def start(self, owner: str | None) -> dict:
        """New pairing code for ``owner`` (``None`` = personal mode)."""
        self._require_enabled()
        r = get_redis()
        for _ in range(5):
            code = "".join(secrets.choice(_ALPHABET) for _ in range(_LEN))
            if r.set(self._key(code), json.dumps({"owner": owner or "", "state": "waiting"}), nx=True, ex=CONNECT_TTL_S):
                return {"code": pretty(code), "expires_in": CONNECT_TTL_S}
        raise InvalidRequestError("Could not create a code — try again")  # 5 collisions in 31^8: effectively never

    def _load(self, code: str) -> dict:
        raw = get_redis().get(self._key(normalize(code)))
        if not raw:
            raise NotFoundError("This code is wrong or has expired — start again")
        return json.loads(raw)

    def status(self, code: str, owner: str | None) -> dict:
        """``waiting`` or ``done`` (+ ``login`` / ``count``); only the visitor that asked may poll."""
        rec = self._load(code)
        if rec["owner"] != (owner or ""):
            raise NotFoundError("This code is wrong or has expired — start again")
        return {k: v for k, v in rec.items() if k != "owner"}

    def submit(self, code: str, text: str) -> dict:
        """The extension's upload. Raises ``NotFoundError`` for a wrong, expired or used code."""
        self._require_enabled()
        code = normalize(code)
        rec = self._load(code)
        if rec["state"] != "waiting":
            raise NotFoundError("This code was already used — start again")
        owner = rec["owner"]
        cookies, _ = parse_netscape(text)
        if not any(c.name in LOGIN_NAMES and not c.expired() for c in cookies):  # checked first: never overwrite working cookies
            raise InvalidRequestError(
                "These cookies are not signed in to YouTube — sign in to YouTube in this browser first, then connect again.")
        st = cookie_service.save_visitor(owner, text) if owner else cookie_service.save(text)
        get_redis().set(self._key(code), json.dumps({"owner": owner, "state": "done", "login": st.login, "count": st.count}), ex=DONE_TTL_S)
        log.info("account linked through the extension (%d cookies)", st.count)  # never the code, owner or values
        return {"ok": True, "count": st.count}


connect_service = ConnectService()
