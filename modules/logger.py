"""
Colorized logger with a SUCCESS level, optional JSON output and redaction.

Usage:
    from modules.logger import get_logger

    logger = get_logger(__name__)
    logger.info("hello")
    logger.success("done")          # custom level 25 (between INFO and WARNING)

Environment:
    LOG_LEVEL   DEBUG | INFO | WARNING | ERROR | CRITICAL   (default INFO)
    LOG_FORMAT  text | json                                  (default text)
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
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
        (re.compile(r"(?i)(cookie:?\s*)\S+"), r"\1***"),
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
    logger.handlers = [handler]
    logger.setLevel(getattr(logging, lvl, logging.INFO))
    logger.propagate = False
    logger._ytdl_configured = True  # type: ignore[attr-defined]
    return logger
