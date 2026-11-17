"""Log files: one directory, per-role files, errors split out, redaction, server loggers attached once."""

from __future__ import annotations

import logging
import uuid

import pytest

from modules.logger import (
    QuietHealthAccessFilter,
    attach_server_logs,
    get_logger,
    log_dir,
    quiet_health_access,
    reset_file_logging,
)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_TO_FILE", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LOG_ROLE", "worker")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    reset_file_logging()
    yield tmp_path / "logs"
    reset_file_logging()


def _flush():
    for h in logging.getLogger().handlers:
        h.flush()


def _fresh():
    return get_logger(f"t.{uuid.uuid4().hex[:8]}")


def test_log_dir_defaults_to_the_repo_logs_folder(monkeypatch):
    monkeypatch.delenv("LOG_DIR", raising=False)
    assert log_dir().name == "logs" and (log_dir().parent / "main.py").exists()


def test_records_go_to_role_file_and_warnings_to_errors_file(logs):
    lg = _fresh()
    lg.info("hello info")
    lg.error("something broke")
    for h in lg.handlers:
        h.flush()
    main = (logs / "worker.log").read_text(encoding="utf-8")
    errors = (logs / "worker.errors.log").read_text(encoding="utf-8")
    assert "hello info" in main and "something broke" in main
    assert "something broke" in errors and "hello info" not in errors


def test_secrets_are_masked_in_files(logs):
    lg = _fresh()
    lg.warning("login password=hunter2 token=abc123")
    for h in lg.handlers:
        h.flush()
    text = (logs / "worker.log").read_text(encoding="utf-8")
    assert "hunter2" not in text and "abc123" not in text and "password=***" in text


def test_disabled_writes_nothing(logs, monkeypatch):
    monkeypatch.setenv("LOG_TO_FILE", "false")
    lg = _fresh()
    lg.error("console only")
    assert not logs.exists() or not list(logs.glob("*.log"))
    assert len(lg.handlers) == 1


def test_server_loggers_are_attached_once_and_access_has_its_own_file(logs):
    names = ("uvicorn", "huey", "uvicorn.access")
    before = {n: list(logging.getLogger(n).handlers) for n in names}
    try:
        attach_server_logs()
        attach_server_logs()  # idempotent
        for n in ("uvicorn", "huey"):
            added = [h for h in logging.getLogger(n).handlers if h not in before[n]]
            assert len(added) == 2, n  # <role>.log + <role>.errors.log
        # successful health checks are filtered (UI polls them); a normal request still lands in access.log
        access = logging.getLogger("uvicorn.access")
        access.warning('127.0.0.1:1 - "GET /api/health HTTP/1.1" 200 OK')
        access.warning('127.0.0.1:1 - "GET /api/config HTTP/1.1" 200 OK')
        for h in access.handlers:
            h.flush()
        text = (logs / "access.log").read_text(encoding="utf-8")
        assert "GET /api/health" not in text
        assert "GET /api/config" in text
    finally:
        for n in names:
            lg = logging.getLogger(n)
            lg.handlers = before[n]


def test_unwritable_directory_falls_back_to_console(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("LOG_TO_FILE", "true")
    monkeypatch.setenv("LOG_DIR", str(blocker / "sub"))  # a path below a regular file can never be created
    reset_file_logging()
    lg = _fresh()
    lg.info("still works")
    assert len(lg.handlers) == 1
    reset_file_logging()


def test_cookie_header_is_masked_but_cookie_urls_are_not(logs):
    lg = _fresh()
    lg.warning("Cookie: SID=secretvalue")
    lg.warning("GET /api/cookies HTTP/1.1 200")
    for h in lg.handlers:
        h.flush()
    text = (logs / "worker.log").read_text(encoding="utf-8")
    assert "secretvalue" not in text and "/api/cookies" in text


def test_no_duplicate_lines_for_child_loggers(logs):
    before = list(logging.getLogger("uvicorn").handlers)
    try:
        attach_server_logs()
        child = logging.getLogger("uvicorn.error")
        child.propagate = True
        child.warning("once only")
        for h in logging.getLogger("uvicorn").handlers:
            h.flush()
        assert (logs / "worker.log").read_text(encoding="utf-8").count("once only") == 1
    finally:
        logging.getLogger("uvicorn").handlers = before


def test_quiet_health_access_drops_only_successful_health_checks():
    f = QuietHealthAccessFilter()
    class R:
        def __init__(self, msg): self._msg = msg
        def getMessage(self): return self._msg
    assert f.filter(R('127.0.0.1:1 - "GET /api/health HTTP/1.1" 200 OK')) is False
    assert f.filter(R('127.0.0.1:1 - "GET /api/health HTTP/1.1" 503 Service Unavailable')) is True
    assert f.filter(R('127.0.0.1:1 - "GET /api/config HTTP/1.1" 200 OK')) is True
    quiet_health_access()
    quiet_health_access()  # idempotent
    assert sum(1 for x in logging.getLogger("uvicorn.access").filters if isinstance(x, QuietHealthAccessFilter)) == 1
