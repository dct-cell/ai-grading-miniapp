"""End-to-end proof that the Worker protocol works against the real server.

Everything else in tests/worker uses an in-memory fake server, which cannot
catch a mismatch between what the daemon sends and what the server accepts.
Here the real FastAPI application is driven over ASGI, so the whole Phase 03
contract — register, lease, ack, upload, commit — is exercised for real.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.domain.states import JobState, OrderState
from server.models import FileObject, GradingJob, Worker
from server.services.results import RESULT_STAGING_DIRECTORY
from tests.server.conftest import (
    SHARED_KEY,
    authenticate,
    build_client,
    build_settings,
    create_quote,
)
from worker.client import WorkerClient
from worker.config import WorkerSettings
from worker.runtime.daemon import WorkerDaemon
from worker.runtime.fake_grader import FakeGrader
from worker.supervisor import poll_once, registered_lanes


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(tmp_path: Path):
    settings = build_settings(tmp_path)
    with build_client(settings) as client:
        yield client, settings


def queue_paid_order(client: TestClient, *, pages: int = 2) -> str:
    authenticate(client)
    quote = create_quote(client, pages=pages)
    prepay = client.post(
        "/api/v1/payments/prepay", json={"quote_id": quote["id"]}
    ).json()
    assert (
        client.post(
            "/callbacks/fake/pay",
            json={"fake_transaction_id": prepay["prepay_id"], "status": "SUCCESS"},
        ).status_code
        == 204
    )
    return client.get("/api/v1/orders").json()["items"][0]["id"]


def build_worker_client(app, settings, tmp_path: Path) -> WorkerClient:
    worker_settings = WorkerSettings(
        server_base_url="http://127.0.0.1:8000",
        shared_key=SHARED_KEY,
        installation_id="install-e2e",
        workspace_root=tmp_path / "worker-workspace",
        # Diagnostics and tests use Prefer: wait=0 so an empty queue answers at
        # once instead of holding the full 25-second long poll.
        poll_wait_seconds=1,
    )
    client = WorkerClient(worker_settings)
    transport = httpx.ASGITransport(app=app)
    client._client = lambda: httpx.AsyncClient(  # noqa: SLF001
        transport=transport, base_url="http://127.0.0.1:8000"
    )
    return client


@pytest.mark.anyio
async def test_a_worker_delivers_a_paid_order_end_to_end(
    server,
    tmp_path: Path,
) -> None:
    client, settings = server
    order_id = queue_paid_order(client, pages=2)
    worker_client = build_worker_client(client.app, settings, tmp_path)
    workspace_root = tmp_path / "worker-workspace"

    registration = await worker_client.register()
    daemon = WorkerDaemon(
        client=worker_client,
        runtime=FakeGrader(),
        workspace_root=workspace_root,
    )
    processed = await daemon.run_one_poll()

    assert processed is True
    assert registration["worker_id"]
    detail = client.get(f"/api/v1/orders/{order_id}").json()
    assert detail["state"] == OrderState.V1_DELIVERED
    assert detail["rounds"][0]["state"] == JobState.SUCCEEDED
    assert detail["rounds"][0]["delivered_at"] is not None

    session_factory = client.app.state.session_factory
    with session_factory() as session:
        job = session.scalars(select(GradingJob)).one()
        worker = session.get(Worker, registration["worker_id"])
        results = session.scalars(
            select(FileObject).where(
                FileObject.kind.in_({"result_json", "result_pdf"})
            )
        ).all()
    assert job.state == JobState.SUCCEEDED
    assert worker.current_job_id is None
    assert len(results) == 2
    for record in results:
        assert (settings.data_dir / record.relative_path).is_file()
        assert RESULT_STAGING_DIRECTORY not in record.relative_path

    # The Worker keeps no student data and leaves no staged bytes behind.
    assert [path for path in workspace_root.rglob("*") if path.is_file()] == []
    staging = settings.data_dir / RESULT_STAGING_DIRECTORY
    assert [path for path in staging.rglob("*") if path.is_file()] == []


@pytest.mark.anyio
async def test_a_second_poll_finds_an_empty_queue(server, tmp_path: Path) -> None:
    client, settings = server
    queue_paid_order(client)
    worker_client = build_worker_client(client.app, settings, tmp_path)
    await worker_client.register()
    daemon = WorkerDaemon(
        client=worker_client,
        runtime=FakeGrader(),
        workspace_root=tmp_path / "worker-workspace",
    )

    assert await daemon.run_one_poll() is True
    assert await daemon.run_one_poll() is False


@pytest.mark.anyio
async def test_two_workers_deliver_two_orders_without_collision(
    server,
    tmp_path: Path,
) -> None:
    client, settings = server
    first_order = queue_paid_order(client)
    second_order = queue_paid_order(client)
    assert first_order != second_order

    delivered: list[str] = []
    for index in range(2):
        worker_settings = WorkerSettings(
            server_base_url="http://127.0.0.1:8000",
            shared_key=SHARED_KEY,
            installation_id=f"install-e2e-{index}",
            workspace_root=tmp_path / f"workspace-{index}",
            poll_wait_seconds=1,
        )
        worker_client = WorkerClient(worker_settings)
        transport = httpx.ASGITransport(app=client.app)
        worker_client._client = lambda transport=transport: httpx.AsyncClient(  # noqa: SLF001
            transport=transport, base_url="http://127.0.0.1:8000"
        )
        await worker_client.register()
        daemon = WorkerDaemon(
            client=worker_client,
            runtime=FakeGrader(),
            workspace_root=worker_settings.workspace_root,
        )
        assert await daemon.run_one_poll() is True

    for order_id in (first_order, second_order):
        detail = client.get(f"/api/v1/orders/{order_id}").json()
        delivered.append(detail["state"])
    assert delivered == [OrderState.V1_DELIVERED, OrderState.V1_DELIVERED]

    session_factory = client.app.state.session_factory
    with session_factory() as session:
        jobs = session.scalars(select(GradingJob)).all()
    assert {job.state for job in jobs} == {JobState.SUCCEEDED}
    assert len({job.worker_id for job in jobs}) == 2


@pytest.mark.anyio
async def test_one_supervisor_runs_three_virtual_workers_in_parallel(
    server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = server
    order_ids = [queue_paid_order(client) for _ in range(3)]
    transport = httpx.ASGITransport(app=client.app)

    def asgi_client(_self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        )

    monkeypatch.setattr(WorkerClient, "_client", asgi_client)
    worker_settings = WorkerSettings(
        server_base_url="http://127.0.0.1:8000",
        shared_key=SHARED_KEY,
        installation_id="install-supervisor",
        worker_id="",
        workspace_root=tmp_path / "parallel-workspaces",
        poll_wait_seconds=1,
        runtime_mode="fake",
        max_concurrent_jobs=3,
    )

    async with registered_lanes(
        worker_settings,
        lambda lane_settings, worker_client: WorkerDaemon(
            client=worker_client,
            runtime=FakeGrader(),
            workspace_root=lane_settings.workspace_root,
        ),
    ) as lanes:
        assert await poll_once(lanes) == 3

    assert {
        client.get(f"/api/v1/orders/{order_id}").json()["state"]
        for order_id in order_ids
    } == {OrderState.V1_DELIVERED}
    with client.app.state.session_factory() as session:
        workers = session.scalars(
            select(Worker).where(Worker.installation_id.like("install-supervisor%"))
        ).all()
        jobs = session.scalars(select(GradingJob)).all()
    assert len(workers) == 3
    assert {worker.capabilities["concurrency_slots"] for worker in workers} == {3}
    assert len({job.worker_id for job in jobs}) == 3
