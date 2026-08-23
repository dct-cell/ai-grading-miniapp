"""Linux process management via POSIX process groups.

Same strategy as :mod:`worker.platforms.macos`: ``setsid()`` on start,
``killpg(SIGTERM)`` → wait → ``killpg(SIGKILL)`` on terminate. Kept as a
separate module so platform-specific diagnostics can diverge later
(e.g. cgroup-based telemetry) without touching the macOS path.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import IO, Any, Sequence

from worker.platforms.base import PlatformAdapter, binary_available


class LinuxPlatform(PlatformAdapter):
    """POSIX process adapter used on Linux."""

    def start_process(
        self,
        argv: Sequence[str],
        *,
        stdout: int | IO[Any] | None = None,
        stderr: int | IO[Any] | None = None,
        **popen_kwargs: Any,
    ) -> subprocess.Popen:
        if "shell" in popen_kwargs:
            raise TypeError("start_process never uses shell=True")
        popen_kwargs.pop("shell", None)
        return subprocess.Popen(
            list(argv),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            **popen_kwargs,
        )

    def terminate_tree(
        self, process: subprocess.Popen, *, timeout_seconds: int = 10
    ) -> None:
        if process.poll() is not None:
            return
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        _signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + max(0, timeout_seconds)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() is None:
            _signal_group(pgid, signal.SIGKILL)
            _wait_reap(process, timeout_seconds=5)

    def service_status(self, name: str) -> bool:
        return binary_available(name)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _wait_reap(process: subprocess.Popen, *, timeout_seconds: int) -> None:
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        pass
