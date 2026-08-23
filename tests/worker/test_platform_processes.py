from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from worker.platforms import (
    MacOSPlatform,
    LinuxPlatform,
    WindowsPlatform,
    current_platform,
)


class TestAdapterSelection:
    @pytest.mark.parametrize(
        ("system", "expected"),
        [
            ("Darwin", MacOSPlatform),
            ("Linux", LinuxPlatform),
            ("Windows", WindowsPlatform),
        ],
    )
    def test_selects_native_adapter(
        self, monkeypatch: pytest.MonkeyPatch, system: str, expected: type
    ) -> None:
        monkeypatch.setattr(platform, "system", lambda: system)
        adapter = current_platform()
        assert isinstance(adapter, expected)

    def test_unknown_platform_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Plan9")
        with pytest.raises(NotImplementedError, match="Plan9"):
            current_platform()


class TestPlatformInterface:
    def test_adapter_exposes_start_terminate_status(self) -> None:
        adapter = MacOSPlatform()
        for method in ("start_process", "terminate_tree", "service_status"):
            assert hasattr(adapter, method), f"adapter missing {method}"


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS smoke test")
class TestMacOSProcessTreeTermination:
    """Spawn a child that spawns a long-lived grandchild, terminate the
    tree, and assert both PIDs exit within 15 seconds.

    The grandchild outlives the child if it is not killed as part of the
    process group, so this test catches the regression where terminate_tree
    only signals the direct child.
    """

    def test_terminates_child_and_grandchild(self, tmp_path: Path) -> None:
        adapter = MacOSPlatform()
        grandchild_marker = tmp_path / "grandchild.pid"
        # A small program whose child stays alive after the parent dies.
        # The grandchild writes its PID to a marker file then sleeps.
        grandchild_program = tmp_path / "grandchild.py"
        grandchild_program.write_text(
            "import os, sys, time\n"
            "pid = os.getpid()\n"
            f"open({str(grandchild_marker)!r}, 'w').write(str(pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        parent_program = tmp_path / "parent.py"
        parent_program.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(grandchild_program)!r}])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )

        process = adapter.start_process(
            [sys.executable, str(parent_program)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Wait for the grandchild to write its PID.
            deadline = time.time() + 10
            while not grandchild_marker.is_file() and time.time() < deadline:
                time.sleep(0.05)
            assert grandchild_marker.is_file(), "grandchild did not start"
            grandchild_pid = int(grandchild_marker.read_text().strip())

            adapter.terminate_tree(process, timeout_seconds=10)

            # Both PIDs must be gone within 15 seconds of the terminate call.
            deadline = time.time() + 15
            while time.time() < deadline:
                if not _pid_alive(process.pid) and not _pid_alive(grandchild_pid):
                    break
                time.sleep(0.1)
            assert not _pid_alive(process.pid), "parent still alive"
            assert not _pid_alive(grandchild_pid), "grandchild still alive"
        finally:
            try:
                adapter.terminate_tree(process, timeout_seconds=5)
            except Exception:
                pass


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux smoke test")
class TestLinuxProcessTreeTermination:
    def test_terminates_child_and_grandchild(self, tmp_path: Path) -> None:
        adapter = LinuxPlatform()
        grandchild_marker = tmp_path / "grandchild.pid"
        grandchild_program = tmp_path / "grandchild.py"
        grandchild_program.write_text(
            "import os, sys, time\n"
            "pid = os.getpid()\n"
            f"open({str(grandchild_marker)!r}, 'w').write(str(pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        parent_program = tmp_path / "parent.py"
        parent_program.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(grandchild_program)!r}])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )

        process = adapter.start_process(
            [sys.executable, str(parent_program)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 10
            while not grandchild_marker.is_file() and time.time() < deadline:
                time.sleep(0.05)
            assert grandchild_marker.is_file(), "grandchild did not start"
            grandchild_pid = int(grandchild_marker.read_text().strip())

            adapter.terminate_tree(process, timeout_seconds=10)

            deadline = time.time() + 15
            while time.time() < deadline:
                if not _pid_alive(process.pid) and not _pid_alive(grandchild_pid):
                    break
                time.sleep(0.1)
            assert not _pid_alive(process.pid), "parent still alive"
            assert not _pid_alive(grandchild_pid), "grandchild still alive"
        finally:
            try:
                adapter.terminate_tree(process, timeout_seconds=5)
            except Exception:
                pass


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows smoke test")
class TestWindowsProcessTreeTermination:
    def test_terminates_child_and_grandchild(self, tmp_path: Path) -> None:
        adapter = WindowsPlatform()
        # Windows uses a Job Object; the assertion shape is the same.
        grandchild_marker = tmp_path / "grandchild.pid"
        grandchild_program = tmp_path / "grandchild.py"
        grandchild_program.write_text(
            "import os, sys, time\n"
            "pid = os.getpid()\n"
            f"open({str(grandchild_marker)!r}, 'w').write(str(pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        parent_program = tmp_path / "parent.py"
        parent_program.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(grandchild_program)!r}])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )

        process = adapter.start_process(
            [sys.executable, str(parent_program)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 10
            while not grandchild_marker.is_file() and time.time() < deadline:
                time.sleep(0.05)
            assert grandchild_marker.is_file(), "grandchild did not start"
            grandchild_pid = int(grandchild_marker.read_text().strip())

            adapter.terminate_tree(process, timeout_seconds=10)

            deadline = time.time() + 15
            while time.time() < deadline:
                if not _pid_alive(process.pid) and not _pid_alive(grandchild_pid):
                    break
                time.sleep(0.1)
            assert not _pid_alive(process.pid), "parent still alive"
            assert not _pid_alive(grandchild_pid), "grandchild still alive"
        finally:
            try:
                adapter.terminate_tree(process, timeout_seconds=5)
            except Exception:
                pass


class TestStartProcessNeverUsesShell:
    def test_start_process_does_not_accept_shell_true(self) -> None:
        # The adapter signature must not expose shell=True; codex_runner
        # builds command lists and the worker must never shell out.
        adapter = MacOSPlatform()
        sig = adapter.start_process.__doc__ or ""
        # Defensive: the docstring says no shell, and the signature has no
        # shell parameter.
        import inspect

        params = inspect.signature(adapter.start_process).parameters
        assert "shell" not in params


def _pid_alive(pid: int) -> bool:
    """Return True if the PID is still running on POSIX."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
