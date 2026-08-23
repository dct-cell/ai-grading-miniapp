from __future__ import annotations

import sys
from collections.abc import Sequence

import anyio
from pydantic import ValidationError

from worker.client import WorkerClient
from worker.config import WorkerSettings
from worker.runtime.daemon import WorkerDaemon
from worker.runtime.doctor import CheckResult, Doctor
from worker.runtime.fake_grader import FakeGrader
from worker.runtime.legacy_codex import LegacyCodexRuntime


COMMANDS = (
    "register",
    "doctor",
    "run",
    "run-once",
    "status",
    "drain",
)

_USAGE = "usage: python -m worker.cli {" + "|".join(COMMANDS) + "}"


def _load_settings() -> WorkerSettings:
    return WorkerSettings()


def _daemon(settings: WorkerSettings) -> WorkerDaemon:
    runtime = (
        FakeGrader()
        if settings.runtime_mode == "fake"
        else LegacyCodexRuntime(
            runner_mode="real",
            codex_bin=settings.codex_bin,
            timeout_seconds=settings.grading_timeout_seconds,
        )
    )
    return WorkerDaemon(
        client=WorkerClient(settings),
        runtime=runtime,
        workspace_root=settings.workspace_root,
        renew_interval_seconds=settings.renew_interval_seconds,
    )


def _doctor(settings: WorkerSettings, argv: Sequence[str] | None = None) -> int:
    """Run the worker environment doctor.

    Prints the local configuration (without the shared key) followed by
    the eight capability checks. Returns 0 when every check passes and
    1 otherwise so installers and CI can gate on the exit code.

    ``--full`` additionally runs the golden-PDF check that exercises the
    real runtime end-to-end. The default run stays cheap so CI without
    Codex or XeLaTeX installed can still validate the worker install.
    """
    full = bool(argv and "--full" in argv)
    print(f"server_base_url: {settings.server_base_url}")
    print(f"installation_id: {settings.installation_id}")
    print(f"worker_id: {settings.worker_id or '(unregistered)'}")
    print(f"workspace_root: {settings.workspace_root}")
    print(f"worker_version: {settings.worker_version}")
    print(f"shared_key: configured ({len(settings.shared_key)} chars, not shown)")
    print(f"max_codex_sessions_per_job: {settings.max_codex_sessions_per_job}")
    print(f"runtime: {settings.runtime_mode}")
    report = Doctor(settings, full=full).run()
    print(report.to_human())
    if not report.ok:
        return 1
    if full and not report.checks.get("golden_pdf", CheckResult("golden_pdf", True, "")).ok:
        return 1
    return 0


def _register(settings: WorkerSettings) -> int:
    body = anyio.run(WorkerClient(settings).register)
    print(f"worker_id: {body['worker_id']}")
    print(f"heartbeat_interval_seconds: {body['heartbeat_interval_seconds']}")
    print(f"lease_seconds: {body['lease_seconds']}")
    return 0


def _status(settings: WorkerSettings) -> int:
    body = anyio.run(WorkerClient(settings).heartbeat)
    print(f"worker_id: {body['worker_id']}")
    print(f"status: {body['status']}")
    print(f"current_job_id: {body['current_job_id'] or '(idle)'}")
    return 0


def _run_once(settings: WorkerSettings) -> int:
    processed = anyio.run(_daemon(settings).run_one_poll)
    print("processed one job" if processed else "no job was available")
    return 0


def _run(settings: WorkerSettings) -> int:
    anyio.run(_daemon(settings).run_forever)
    return 0


def _drain(settings: WorkerSettings) -> int:
    """Ask a running daemon to stop after its current job.

    Phase 03 has no supervisor socket yet, so this reports intent rather than
    signalling another process.
    """
    del settings
    print("drain requested: the daemon stops polling after the current job")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print(_USAGE, file=sys.stderr)
        return 2
    command = arguments[0]
    if command not in COMMANDS:
        print(f"unknown command: {command}\n{_USAGE}", file=sys.stderr)
        return 2

    try:
        settings = _load_settings()
    except ValidationError as error:
        print(f"invalid worker configuration:\n{error}", file=sys.stderr)
        return 2

    handlers = {
        "register": _register,
        "doctor": lambda s: _doctor(s, arguments[1:]),
        "run": _run,
        "run-once": _run_once,
        "status": _status,
        "drain": _drain,
    }
    return handlers[command](settings)


if __name__ == "__main__":
    raise SystemExit(main())
