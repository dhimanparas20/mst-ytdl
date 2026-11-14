"""
Process launcher.

    uv run python main.py api       FastAPI server (uvicorn)
    uv run python main.py worker    Huey consumer (does the downloading)
    uv run python main.py dev       both, with reload for the API; Ctrl+C stops all

Settings come from .env (API_HOST / API_PORT / TASK_WORKERS ...).
"""

from __future__ import annotations

import subprocess
import sys

from configs import get_settings

PYTHON = sys.executable


def api_cmd(reload: bool = False) -> list[str]:
    s = get_settings().api
    cmd = [PYTHON, "-m", "uvicorn", "api.main:app", "--host", s.host, "--port", str(s.port),
           "--timeout-graceful-shutdown", "2"]
    if reload:
        cmd += ["--reload", "--reload-dir", "api", "--reload-dir", "services",
                "--reload-dir", "models", "--reload-dir", "configs", "--reload-dir", "modules",
                "--reload-dir", "web"]
    return cmd


def worker_cmd() -> list[str]:
    workers = get_settings().task.workers
    return [PYTHON, "-m", "huey.bin.huey_consumer", "tasks.huey", "-k", "thread", "-w", str(workers)]


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "dev"
    if mode == "api":
        return subprocess.call(api_cmd())
    if mode == "worker":
        return subprocess.call(worker_cmd())
    if mode == "dev":
        procs = [subprocess.Popen(worker_cmd()), subprocess.Popen(api_cmd(reload=True))]
        try:
            return max(p.wait() for p in procs)
        except KeyboardInterrupt:
            return 0
        finally:
            for p in procs:
                if p.poll() is None:
                    p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
