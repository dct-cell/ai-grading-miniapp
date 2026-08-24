from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from worker.client import LeasedTask, WorkerAuthenticationError, WorkerClient
from worker.config import WorkerSettings
from worker.runtime.contracts import RuntimeResult
from worker.runtime.testsupport import build_minimal_pdf


SHARED_KEY = "worker-key-" + "w" * 32


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        server_base_url="https://grader.example.com",
        shared_key=SHARED_KEY,
        installation_id="retry-worker",
        worker_id="worker-1",
        workspace_root=tmp_path,
    )


def _task() -> LeasedTask:
    return LeasedTask(
        job_id="job-1",
        order_id="order-1",
        round_number=1,
        lease_version=1,
        service_tier="annotated_review",
        grading_standard="imo",
        league_scope=None,
        note="",
        page_count=1,
        source_file_id="source-1",
        source_download_token="download-1",
        reference_file_id=None,
        reference_download_token=None,
    )


@pytest.mark.anyio
async def test_worker_client_reuses_one_http_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"worker_id": "worker-1", "status": "online", "current_job_id": None},
        )

    client = WorkerClient(_settings(tmp_path))

    def factory() -> httpx.AsyncClient:
        nonlocal calls
        calls += 1
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(client, "_client", factory)
    await client.heartbeat()
    await client.heartbeat()
    await client.aclose()

    assert calls == 1


@pytest.mark.anyio
async def test_upload_retries_only_the_idempotent_http_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"result_json": 0, "result_pdf": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/result/uploads"):
            return httpx.Response(
                200,
                json={
                    "result_json": {"upload_token": "json-token", "max_bytes": 999999},
                    "result_pdf": {"upload_token": "pdf-token", "max_bytes": 999999},
                },
            )
        kind = path.rsplit("/", 1)[-1]
        await request.aread()
        attempts[kind] += 1
        if kind == "result_json" and attempts[kind] == 1:
            return httpx.Response(503, json={"detail": "temporary"})
        return httpx.Response(201, json={"file_id": f"{kind}-id"})

    async def no_pause(_attempt: int) -> None:
        return None

    json_path = tmp_path / "grading.json"
    pdf_path = tmp_path / "annotated.pdf"
    manifest_path = tmp_path / "manifest.json"
    json_path.write_bytes(b'{"score":0}')
    pdf_path.write_bytes(build_minimal_pdf(1))
    manifest_path.write_bytes(b"{}")
    result = RuntimeResult(
        manifest_path=manifest_path,
        result_json_path=json_path,
        result_pdf_path=pdf_path,
        result_json_sha256="0" * 64,
        result_pdf_sha256="0" * 64,
        output_page_count=1,
    )
    client = WorkerClient(_settings(tmp_path))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    monkeypatch.setattr(client, "_retry_pause", no_pause)

    uploaded = await client.upload_result(_task(), result)
    await client.aclose()

    assert uploaded == {
        "result_json_file_id": "result_json-id",
        "result_pdf_file_id": "result_pdf-id",
    }
    assert attempts == {"result_json": 2, "result_pdf": 1}


@pytest.mark.anyio
async def test_authentication_failure_is_never_retried(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"detail": "no"})

    client = WorkerClient(_settings(tmp_path))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001

    with pytest.raises(WorkerAuthenticationError):
        await client.heartbeat()
    await client.aclose()

    assert calls == 1
