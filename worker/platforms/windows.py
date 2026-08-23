"""Windows process management via Job Objects.

Each process is attached to a Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. When the Worker exits or closes
the job handle, the kernel kills every process in the job — including
descendants the child spawned later. ``terminate_tree`` escalates from
``TerminateProcess`` on the parent (which cascades through the job) to
``kill()`` if the graceful path stalls.

Windows-specific imports are deferred to call time so this module can be
imported on non-Windows hosts for adapter-selection tests.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import IO, Any, Sequence

from worker.platforms.base import PlatformAdapter

# Windows creation flag for a new process group. Defined here because
# ``subprocess.CREATE_NEW_PROCESS_GROUP`` only exists on Windows builds.
_CREATE_NEW_PROCESS_GROUP = 0x00000200


class WindowsPlatform(PlatformAdapter):
    """Windows adapter using Job Objects for tree termination."""

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
        creationflags = int(popen_kwargs.pop("creationflags", 0))
        creationflags |= _CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            list(argv),
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            **popen_kwargs,
        )
        _attach_to_job_object(process)
        return process

    def terminate_tree(
        self, process: subprocess.Popen, *, timeout_seconds: int = 10
    ) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=max(0, timeout_seconds))
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        _close_job_handle(process)

    def service_status(self, name: str) -> bool:
        return shutil.which(name) is not None


def _attach_to_job_object(process: subprocess.Popen) -> None:
    """Attach the process to a new Job Object so descendants die with it.

    Imports ``ctypes`` lazily so macOS can import this module for the
    adapter-selection tests without a Windows toolchain.
    """
    import ctypes  # local import: Windows-only at call time
    from ctypes import wintypes

    JobObjectExtendedLimitInformation = 7
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    PROCESS_ALL_ACCESS = 0x1F0FFF

    class _IOCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job_handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    proc_handle = kernel32.OpenProcessW(PROCESS_ALL_ACCESS, False, process.pid)
    if not proc_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ok = kernel32.AssignProcessToJobObject(job_handle, proc_handle)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(proc_handle)
    # Stash the job handle on the Popen so it stays alive for the
    # process lifetime and can be closed explicitly in terminate_tree.
    process._job_handle = job_handle  # type: ignore[attr-defined]


def _close_job_handle(process: subprocess.Popen) -> None:
    handle = getattr(process, "_job_handle", None)
    if handle is None:
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(handle)
    process._job_handle = None  # type: ignore[attr-defined]
