"""Netscape ``cookies.txt`` parsing and normalisation (pure helpers, no I/O)."""

from __future__ import annotations

import time
from dataclasses import dataclass

HEADER = "# Netscape HTTP Cookie File"
_HTTPONLY = "#HttpOnly_"
#: Cookie names that only exist for a signed-in Google/YouTube session.
LOGIN_NAMES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"})


@dataclass(frozen=True)
class Cookie:
    """One cookie line."""

    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int  # unix seconds; 0 = session cookie
    name: str
    value: str
    http_only: bool = False

    def to_line(self) -> str:
        """Serialise as a tab-separated Netscape line."""
        domain = (_HTTPONLY if self.http_only else "") + self.domain
        return "\t".join(
            [domain, _b(self.include_subdomains), self.path, _b(self.secure), str(self.expires), self.name, self.value]
        )

    def expired(self, now: float | None = None) -> bool:
        """True for a persistent cookie whose expiry has passed."""
        return self.expires > 0 and self.expires < (now if now is not None else time.time())


def _b(v: bool) -> str:
    return "TRUE" if v else "FALSE"


def _bool(s: str) -> bool | None:
    s = s.strip().upper()
    return True if s == "TRUE" else False if s == "FALSE" else None


def parse_netscape(text: str) -> tuple[list[Cookie], int]:
    """
    Parse Netscape-format cookie text.

    Tolerates CRLF, ``#HttpOnly_`` prefixes, comments, blank lines and editors that
    turned the tabs into runs of spaces.

    Args:
        text: Raw pasted or file content.

    Returns:
        ``(cookies, bad_lines)`` — the parsed cookies and the number of non-comment
        lines that were not valid cookie rows.
    """
    cookies: list[Cookie] = []
    bad = 0
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        http_only = line.startswith(_HTTPONLY)
        if http_only:
            line = line[len(_HTTPONLY):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split(None, 6)
        if len(parts) < 7:
            bad += 1
            continue
        domain, sub, path, secure, expires, name, value = (p.strip() for p in parts[:7])
        sub_b, secure_b = _bool(sub), _bool(secure)
        try:
            exp = int(float(expires))
        except ValueError:
            bad += 1
            continue
        if sub_b is None or secure_b is None or not domain or not name:
            bad += 1
            continue
        cookies.append(Cookie(domain, sub_b, path or "/", secure_b, max(exp, 0), name, value, http_only))
    return cookies, bad


def render_netscape(cookies: list[Cookie]) -> str:
    """Canonical file content: header line, then one tab-separated row per cookie, LF endings."""
    return HEADER + "\n" + "\n".join(c.to_line() for c in cookies) + "\n"


def domain_matches(domain: str, roots: tuple[str, ...]) -> bool:
    """True when ``domain`` (with or without a leading dot) is one of ``roots`` or a subdomain of one."""
    d = domain.lstrip(".").lower()
    return any(d == r or d.endswith("." + r) for r in roots)
