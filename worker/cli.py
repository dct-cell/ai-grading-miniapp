from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Sequence

import anyio
from pydantic import ValidationError

from worker.client import WorkerClient
from worker.config import WorkerSettings
from worker.runtime.daemon import WorkerDaemon
from worker.runtime.doctor import Doctor
from worker.runtime.fake_grader import FakeGrader
from worker.runtime.legacy_codex import LegacyCodexRuntime
from worker.supervisor import (
    poll_once,
    registered_lanes,
    request_drain,
    run_forever as run_fleet_forever,
)


COMMANDS = (
    "register",
    "doctor",
    "run",
    "run-once",
    "status",
)

_USAGE = "usage: python -m worker.cli {" + "|".join(COMMANDS) + "}"


def _load_settings() -> WorkerSettings:
    env_file = os.environ.get("GRADER_WORKER_ENV_FILE")
    return WorkerSettings(_env_file=env_file or ".env")


def _daemon(settings: WorkerSettings, client: WorkerClient | None = None) -> WorkerDaemon:
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
        client=client or WorkerClient(settings),
        runtime=runtime,
        workspace_root=settings.workspace_root,
        renew_interval_seconds=settings.renew_interval_seconds,
    )


def _doctor(settings: WorkerSettings, argv: Sequence[str] | None = None) -> int:
    """Run the worker environment doctor.

    Prints the local configuration (without the shared key) followed by
    the eight capability checks. Returns 0 when every check passes and
    1 otherwise so installers and CI can gate on the exit code.

    The doctor stays cheap and deterministic; staging owns real golden runs.
    """
    if argv:
        print(f"unknown doctor option: {argv[0]}", file=sys.stderr)
        return 2
    print(f"server_base_url: {settings.server_base_url}")
    print(f"installation_id: {settings.installation_id}")
    print(f"worker_id: {settings.worker_id or '(unregistered)'}")
    print(f"workspace_root: {settings.workspace_root}")
    print(f"worker_version: {settings.worker_version}")
    print(f"shared_key: configured ({len(settings.shared_key)} chars, not shown)")
    print(f"runtime: {settings.runtime_mode}")
    print(f"max_concurrent_jobs: {settings.max_concurrent_jobs}")
    report = Doctor(settings).run()
    print(report.to_human())
    if not report.ok:
        return 1
    return 0


async def _register_async(settings: WorkerSettings) -> list[dict[str, object]]:
    async with registered_lanes(settings, _daemon) as lanes:
        return [lane.registration for lane in lanes]


def _register(settings: WorkerSettings) -> int:
    bodies = anyio.run(_register_async, settings)
    for index, body in enumerate(bodies, start=1):
        print(f"slot_{index}_worker_id: {body['worker_id']}")
    print(f"heartbeat_interval_seconds: {bodies[0]['heartbeat_interval_seconds']}")
    print(f"lease_seconds: {bodies[0]['lease_seconds']}")
    return 0


async def _status_async(settings: WorkerSettings) -> list[dict[str, object]]:
    async with registered_lanes(settings, _daemon) as lanes:
        statuses: list[dict[str, object] | None] = [None] * len(lanes)

        async def heartbeat(position: int) -> None:
            statuses[position] = await lanes[position].client.heartbeat(phase="idle")

        async with anyio.create_task_group() as group:
            for position in range(len(lanes)):
                group.start_soon(heartbeat, position)
        return [status for status in statuses if status is not None]


def _status(settings: WorkerSettings) -> int:
    bodies = anyio.run(_status_async, settings)
    for index, body in enumerate(bodies, start=1):
        print(f"slot_{index}_worker_id: {body['worker_id']}")
        print(f"slot_{index}_status: {body['status']}")
        print(f"slot_{index}_current_job_id: {body['current_job_id'] or '(idle)'}")
    return 0


async def _run_once_async(settings: WorkerSettings) -> int:
    async with registered_lanes(settings, _daemon) as lanes:
        return await poll_once(lanes)


def _run_once(settings: WorkerSettings) -> int:
    processed = anyio.run(_run_once_async, settings)
    print(
        f"processed {processed} job(s) across {settings.max_concurrent_jobs} slot(s)"
        if processed
        else "no job was available"
    )
    return 0


async def _run_async(settings: WorkerSettings) -> None:
    async with registered_lanes(settings, _daemon) as lanes:
        loop = asyncio.get_running_loop()
        running_task = asyncio.current_task()
        signals = 0

        def request_stop() -> None:
            nonlocal signals
            signals += 1
            if signals == 1:
                request_drain(lanes)
            elif running_task is not None:
                running_task.cancel()

        installed: list[signal.Signals] = []
        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, request_stop)
            except NotImplementedError:
                continue
            installed.append(sig)
        try:
            await run_fleet_forever(lanes)
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)


def _run(settings: WorkerSettings) -> int:
    anyio.run(_run_async, settings)
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
    }
    return handlers[command](settings)


if __name__ == "__main__":
    raise SystemExit(main())
