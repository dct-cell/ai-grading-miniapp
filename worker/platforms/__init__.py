"""Platform adapter selection.

``current_platform()`` returns the adapter matching the host OS. The
Worker daemon uses this to own child processes (Codex, XeLaTeX, build
scripts) without relying on ``shell=True`` or best-effort kills that
leave grandchildren running.
"""
from __future__ import annotations

import platform as _platform

from worker.platforms.base import PlatformAdapter
from worker.platforms.linux import LinuxPlatform
from worker.platforms.macos import MacOSPlatform
from worker.platforms.windows import WindowsPlatform

__all__ = [
    "PlatformAdapter",
    "MacOSPlatform",
    "LinuxPlatform",
    "WindowsPlatform",
    "current_platform",
]


def current_platform() -> PlatformAdapter:
    """Return the adapter for the current host OS.

    Raises ``NotImplementedError`` on unsupported platforms so the
    Worker fails fast instead of silently degrading to a kill that
    leaves grandchildren alive.
    """
    system = _platform.system()
    if system == "Darwin":
        return MacOSPlatform()
    if system == "Linux":
        return LinuxPlatform()
    if system == "Windows":
        return WindowsPlatform()
    raise NotImplementedError(f"unsupported platform: {system}")
