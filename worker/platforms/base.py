"""Platform adapter contract.

Adapters own process creation and termination. ``start_process`` never
uses ``shell=True`` — callers always pass a ready argument list, so
untrusted input cannot inject shell metacharacters.
"""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import IO, Any, Sequence


class PlatformAdapter(ABC):
    """OS-specific process management contract.

    Each adapter starts a process in a new process group (POSIX) or a
    Job Object (Windows) so that :meth:`terminate_tree` can reliably kill
    the whole descendant tree. Adapters never accept ``shell=True``.
    """

    @abstractmethod
    def start_process(
        self,
        argv: Sequence[str],
        *,
        stdout: int | IO[Any] | None = None,
        stderr: int | IO[Any] | None = None,
        **popen_kwargs: Any,
    ) -> subprocess.Popen:
        """Start ``argv`` in a new process group.

        Never uses ``shell=True``. Descendants spawned by the child
        remain reachable from :meth:`terminate_tree`.
        """

    @abstractmethod
    def terminate_tree(
        self, process: subprocess.Popen, *, timeout_seconds: int = 10
    ) -> None:
        """Terminate ``process`` and all its descendants.

        Sends a graceful signal first, waits up to ``timeout_seconds``,
        then escalates to a forceful kill. Safe to call on an already
        exited process.
        """

    @abstractmethod
    def service_status(self, name: str) -> bool:
        """Return True if the named binary is available on PATH."""


def binary_available(name: str) -> bool:
    """Cross-platform helper for ``service_status`` implementations."""
    return shutil.which(name) is not None
