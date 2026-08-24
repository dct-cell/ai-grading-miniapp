"""One local process supervising several independently fenced Worker lanes."""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import anyio

from worker.client import WorkerClient
from worker.config import WorkerSettings
from worker.runtime.daemon import WorkerDaemon


DaemonFactory = Callable[[WorkerSettings, WorkerClient], WorkerDaemon]


@dataclass(frozen=True)
class LaneSettings:
    index: int
    total: int
    settings: WorkerSettings


@dataclass(frozen=True)
class WorkerLane:
    index: int
    total: int
    settings: WorkerSettings
    client: WorkerClient
    daemon: WorkerDaemon
    registration: dict[str, object]


def derive_lane_settings(settings: WorkerSettings) -> tuple[LaneSettings, ...]:
    """Derive stable virtual identities without changing the Server schema."""
    total = settings.max_concurrent_jobs
    base_device = settings.device_name or settings.installation_id
    lanes: list[LaneSettings] = []
    for offset in range(total):
        index = offset + 1
        suffix = "" if index == 1 else f"-slot-{index:02d}"
        installation_id = (
            settings.installation_id
            if not suffix
            else settings.installation_id[: 64 - len(suffix)] + suffix
        )
        lane_settings = settings.model_copy(
            update={
                "installation_id": installation_id,
                "worker_id": settings.worker_id if index == 1 else None,
                "device_name": f"{base_device} [slot {index}/{total}]"[:128],
                "workspace_root": settings.workspace_root / f"lane-{index:02d}",
            }
        )
        lanes.append(LaneSettings(index=index, total=total, settings=lane_settings))
    return tuple(lanes)


@asynccontextmanager
async def registered_lanes(
    settings: WorkerSettings,
    daemon_factory: DaemonFactory,
) -> AsyncIterator[tuple[WorkerLane, ...]]:
    """Register every lane, own all HTTP sessions, and close them together."""
    definitions = derive_lane_settings(settings)
    async with AsyncExitStack() as stack:
        clients: list[WorkerClient] = []
        registrations: list[dict[str, object] | None] = [None] * len(definitions)
        for lane in definitions:
            client = WorkerClient(
                lane.settings,
                capabilities={
                    "concurrency_slot": lane.index,
                    "concurrency_slots": lane.total,
                    "supervisor_installation_id": settings.installation_id,
                },
            )
            clients.append(await stack.enter_async_context(client))

        async def register_one(position: int) -> None:
            registrations[position] = await clients[position].register()

        async with anyio.create_task_group() as group:
            for position in range(len(definitions)):
                group.start_soon(register_one, position)

        lanes: list[WorkerLane] = []
        for position, definition in enumerate(definitions):
            registration = registrations[position]
            assert registration is not None
            daemon = daemon_factory(definition.settings, clients[position])
            daemon.cleanup_stale_workspaces(
                older_than_seconds=definition.settings.grading_timeout_seconds + 600
            )
            lanes.append(
                WorkerLane(
                    index=definition.index,
                    total=definition.total,
                    settings=definition.settings,
                    client=clients[position],
                    daemon=daemon,
                    registration=registration,
                )
            )
        yield tuple(lanes)


async def poll_once(lanes: tuple[WorkerLane, ...]) -> int:
    results = [False] * len(lanes)

    async def poll(position: int) -> None:
        results[position] = await lanes[position].daemon.run_one_poll()

    async with anyio.create_task_group() as group:
        for position in range(len(lanes)):
            group.start_soon(poll, position)
    return sum(results)


async def run_forever(lanes: tuple[WorkerLane, ...]) -> None:
    async with anyio.create_task_group() as group:
        for lane in lanes:
            group.start_soon(lane.daemon.run_forever)


def request_drain(lanes: tuple[WorkerLane, ...]) -> None:
    for lane in lanes:
        lane.daemon.request_drain()
