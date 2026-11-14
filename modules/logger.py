"""
Colorized logger with a SUCCESS level, optional JSON output and redaction.

Usage:
    from modules.logger import get_logger

    logger = get_logger(__name__)
    logger.info("hello")
    logger.success("done")          # custom level 25 (between INFO and WARNING)

Environment:
    LOG_LEVEL    DEBUG | INFO | WARNING | ERROR | CRITICAL   (default INFO)
    LOG_FORMAT   text | json                                  (default text)
    LOG_DIR      where log files go                           (default <repo>/logs)
    LOG_TO_FILE  true | false — also write rotating files     (default true)
    LOG_ROLE     api | worker | app — names this process' files (default app)

Files (all in LOG_DIR, 5 MB x 5 rotation):
    <role>.log          everything this process logs (app code, uvicorn / huey)
    <role>.errors.log   WARNING and above only
    access.log          HTTP requests (uvicorn.access, API only)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import colorlog

SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


def _success(self: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    """Log at SUCCESS level (25)."""
    if self.isEnabledFor(SUCCESS_LEVEL):
        self._log(SUCCESS_LEVEL, message, args, **kwargs)


logging.Logger.success = _success  # type: ignore[attr-defined]


class RedactionFilter(logging.Filter):
    """Mask passwords, tokens, API keys and bearer credentials in messages."""

    _PATTERNS = [
        (re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=***"),
        (re.compile(r"(?i)(secret|token|api_key|apikey)\s*[=:]\s*\S+"), r"\1=***"),
        (re.compile(r"(?i)(Bearer\s+)\S+"), r"\1***"),
        # a header / assignment ("Cookie: SID=…"), not the word inside a URL such as /api/cookies
        (re.compile(r"(?i)\b((?:set-)?cookie\s*[:=]\s*)\S+"), r"\1***"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat, repl in self._PATTERNS:
            msg = pat.sub(repl, msg)
        record.msg, record.args = msg, None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "pid": record.process,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 5
_FILE_LOCK = threading.Lock()
_FILE_HANDLERS: dict[str, logging.Handler] = {}
#: Third-party loggers whose output belongs in the same files (uvicorn / huey configure these themselves).
_SERVER_LOGGERS = ("uvicorn", "huey")  # their children (uvicorn.error, huey.consumer…) propagate to these


def log_dir() -> Path:
    """Directory that holds every log file: ``LOG_DIR`` or ``<repo>/logs``."""
    env = os.getenv("LOG_DIR", "").strip()
    return Path(env) if env else Path(__file__).resolve().parent.parent / "logs"


def log_role() -> str:
    """Process role used in file names (``LOG_ROLE``: api | worker | app)."""
    return re.sub(r"[^A-Za-z0-9_-]", "", os.getenv("LOG_ROLE", "app")) or "app"


def file_logging_enabled() -> bool:
    """``LOG_TO_FILE`` (default on)."""
    return os.getenv("LOG_TO_FILE", "true").strip().lower() not in ("0", "false", "no", "off")


def _file_handler(filename: str, level: int = logging.NOTSET) -> logging.Handler:
    """One shared rotating handler per file and process (created lazily; the file appears on first write)."""
    path = log_dir() / filename
    key = str(path)
    with _FILE_LOCK:
        h = _FILE_HANDLERS.get(key)
        if h is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=True)
            h.setLevel(level)
            if os.getenv("LOG_FORMAT", "text").lower() == "json":
                h.setFormatter(JsonFormatter())
            else:
                h.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)-8s [%(process)d] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
            h.addFilter(RedactionFilter())
            _FILE_HANDLERS[key] = h
        return h


def _role_file_handlers() -> list[logging.Handler]:
    if not file_logging_enabled():
        return []
    role = log_role()
    try:
        return [_file_handler(f"{role}.log"), _file_handler(f"{role}.errors.log", logging.WARNING)]
    except OSError:  # read-only / missing volume: keep logging to the console
        return []


def attach_server_logs() -> None:
    """
    Send uvicorn / huey output (and HTTP access lines) to the log directory too.

    Those libraries install their own handlers when the server starts, so call this from the
    API lifespan and the Huey startup hook (idempotent).
    """
    if not file_logging_enabled():
        return
    handlers = _role_file_handlers()
    for name in _SERVER_LOGGERS:
        lg = logging.getLogger(name)
        for h in handlers:
            if h not in lg.handlers:
                lg.addHandler(h)
    try:
        access = logging.getLogger("uvicorn.access")
        h = _file_handler("access.log")
        if h not in access.handlers:
            access.addHandler(h)
    except OSError:
        pass


def reset_file_logging() -> None:
    """Close and forget every file handler (tests / changing LOG_DIR)."""
    with _FILE_LOCK:
        for h in _FILE_HANDLERS.values():
            h.close()
        _FILE_HANDLERS.clear()


def get_logger(
    name: str,
    *,
    level: str | None = None,
    json_output: bool | None = None,
    show_time: bool = True,
) -> logging.Logger:
    """
    Build (once per name) a configured logger.

    Args:
        name: Usually ``__name__``.
        level: Overrides ``LOG_LEVEL``.
        json_output: Overrides ``LOG_FORMAT``.
        show_time: Include timestamps in text mode.
    """
    logger = logging.getLogger(name)
    if getattr(logger, "_ytdl_configured", False):
        return logger

    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    use_json = (
        json_output
        if json_output is not None
        else os.getenv("LOG_FORMAT", "text").lower() == "json"
    )

    handler = logging.StreamHandler()
    if use_json:
        handler.setFormatter(JsonFormatter())
    else:
        fmt = (
            "%(log_color)s"
            + ("%(asctime)s " if show_time else "")
            + "%(levelname)-8s%(reset)s %(name)s: %(message)s"
        )
        handler.setFormatter(
            colorlog.ColoredFormatter(
                fmt,
                datefmt="%H:%M:%S",
                log_colors={
                    "DEBUG": "cyan",
                    "INFO": "white",
                    "SUCCESS": "bold_green",
                    "WARNING": "yellow",
                    "ERROR": "red",
                    "CRITICAL": "bold_red",
                },
            )
        )
    handler.addFilter(RedactionFilter())
    logger.handlers = [handler, *_role_file_handlers()]
    logger.setLevel(getattr(logging, lvl, logging.INFO))
    logger.propagate = False
    logger._ytdl_configured = True  # type: ignore[attr-defined]
    return logger
