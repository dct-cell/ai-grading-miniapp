from __future__ import annotations

import hashlib
import os
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx

from worker.config import WorkerSettings


_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_RETRY_DELAYS = (1.0, 2.0)
_FILE_CHUNK_BYTES = 1024 * 1024


class WorkerClientError(RuntimeError):
    """Base error for protocol outcomes the daemon must classify."""


class WorkerAuthenticationError(WorkerClientError):
    """The shared key or Worker identity requires operator intervention."""


class LeaseLost(WorkerClientError):
    """The task was cancelled, refunded or fenced by the server."""


class DownloadDigestMismatch(WorkerClientError):
    """A streamed bundle did not match the immutable server metadata."""


@dataclass(frozen=True)
class LeasedTask:
    """One claimed grading job plus its fencing token."""

    job_id: str
    order_id: str
    round_number: int
    lease_version: int
    service_tier: str
    grading_standard: str
    league_scope: str | None
    note: str
    page_count: int
    source_file_id: str
    source_download_token: str
    reference_file_id: str | None
    reference_download_token: str | None
    source_sha256: str | None = None
    reference_sha256: str | None = None

    @classmethod
    def from_bundle(cls, bundle: dict[str, Any]) -> LeasedTask:
        source = bundle["source_file"]
        reference = bundle.get("reference_file")
        return cls(
            job_id=bundle["job_id"],
            order_id=bundle["order_id"],
            round_number=bundle["round_number"],
            lease_version=bundle["lease_version"],
            service_tier=bundle["service_tier"],
            grading_standard=bundle["grading_standard"],
            league_scope=bundle.get("league_scope"),
            note=bundle["note"],
            page_count=bundle["page_count"],
            source_file_id=source["file_id"],
            source_download_token=source["download_token"],
            reference_file_id=None if reference is None else reference["file_id"],
            reference_download_token=(
                None if reference is None else reference["download_token"]
            ),
            source_sha256=source.get("sha256"),
            reference_sha256=None if reference is None else reference.get("sha256"),
        )


class WorkerClient:
    """One outbound-only HTTPS session shared for the daemon lifetime."""

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        capabilities: dict[str, object] | None = None,
    ) -> None:
        self._settings = settings
        self._worker_id = settings.worker_id
        self._capabilities = dict(capabilities or {})
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> WorkerClient:
        self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def worker_id(self) -> str | None:
        return self._worker_id

    def auth_headers(self) -> dict[str, str]:
        """Build the shared-key plus registered-identity credential."""
        headers = {"Authorization": f"Bearer {self._settings.shared_key}"}
        if self._worker_id:
            headers["X-Worker-ID"] = self._worker_id
        return headers

    def _url(self, path: str) -> str:
        return f"{self._settings.server_base_url}{path}"

    def _client(self) -> httpx.AsyncClient:
        """Factory seam retained for ASGI tests; production calls it once."""
        return httpx.AsyncClient(timeout=self._settings.request_timeout_seconds)

    def _get_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = self._client()
        return self._http

    @staticmethod
    def _raise_protocol_error(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise WorkerAuthenticationError("批改 Worker 凭据无效或已停用。")
        if response.status_code == 409:
            raise LeaseLost("批改任务已取消或租约已失效。")
        response.raise_for_status()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = False,
        timeout: float | httpx.Timeout | None = None,
        content_factory: Callable[[], AsyncIterator[bytes]] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        attempts = 3 if retry else 1
        last_transport: httpx.TransportError | None = None
        for attempt in range(attempts):
            if content_factory is not None:
                kwargs["content"] = content_factory()
            try:
                response = await self._get_client().request(
                    method,
                    self._url(path),
                    timeout=timeout,
                    **kwargs,
                )
            except httpx.TransportError as error:
                last_transport = error
                if attempt + 1 >= attempts:
                    raise
            else:
                if (
                    response.status_code not in _RETRYABLE_STATUSES
                    or attempt + 1 >= attempts
                ):
                    self._raise_protocol_error(response)
                    return response
            await self._retry_pause(attempt)
        assert last_transport is not None
        raise last_transport

    @staticmethod
    async def _retry_pause(attempt: int) -> None:
        base = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
        await anyio.sleep(base * random.uniform(0.8, 1.2))

    async def register(self) -> dict[str, Any]:
        payload = {
            "installation_id": self._settings.installation_id,
            "device_name": self._settings.device_name or self._settings.installation_id,
            "platform": _platform_name(),
            "architecture": _architecture_name(),
            "worker_version": self._settings.worker_version,
            "capabilities": self._capabilities,
        }
        response = await self._request(
            "POST",
            "/worker/v1/register",
            retry=True,
            json=payload,
            headers={"Authorization": f"Bearer {self._settings.shared_key}"},
        )
        body = response.json()
        self._worker_id = body["worker_id"]
        return body

    async def heartbeat(self, *, phase: str | None = None) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/worker/v1/heartbeat",
            retry=True,
            json={"phase": phase},
            headers=self.auth_headers(),
        )
        return response.json()

    async def lease(self, *, wait_seconds: int | None = None) -> LeasedTask | None:
        wait = self._settings.poll_wait_seconds if wait_seconds is None else wait_seconds
        response = await self._request(
            "POST",
            "/worker/v1/jobs/lease",
            headers={**self.auth_headers(), "Prefer": f"wait={wait}"},
            timeout=wait + self._settings.request_timeout_seconds,
        )
        if response.status_code == httpx.codes.NO_CONTENT:
            return None
        return LeasedTask.from_bundle(response.json())

    async def download_bundle(self, task: LeasedTask, workspace: Path) -> Path:
        staging = workspace / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        await self._download_file(
            task,
            kind="source",
            token=task.source_download_token,
            target=staging / "source.pdf",
            expected_sha256=task.source_sha256,
        )
        if task.reference_download_token is not None:
            await self._download_file(
                task,
                kind="reference",
                token=task.reference_download_token,
                target=staging / "reference.pdf",
                expected_sha256=task.reference_sha256,
            )
        return staging

    async def _download_file(
        self,
        task: LeasedTask,
        *,
        kind: str,
        token: str,
        target: Path,
        expected_sha256: str | None,
    ) -> None:
        temporary = target.with_name(f"{target.name}.part")
        for attempt in range(3):
            digest = hashlib.sha256()
            temporary.unlink(missing_ok=True)
            try:
                async with self._get_client().stream(
                    "GET",
                    self._url(f"/worker/v1/jobs/{task.job_id}/bundle/{kind}"),
                    headers={
                        **self.auth_headers(),
                        "X-Download-Token": token,
                    },
                ) as response:
                    if response.status_code == 404 and kind == "reference":
                        return
                    if response.status_code in _RETRYABLE_STATUSES:
                        response.raise_for_status()
                    self._raise_protocol_error(response)
                    async with await anyio.open_file(temporary, "wb") as output:
                        async for chunk in response.aiter_bytes(_FILE_CHUNK_BYTES):
                            digest.update(chunk)
                            await output.write(chunk)
                if expected_sha256 and digest.hexdigest() != expected_sha256.lower():
                    raise DownloadDigestMismatch("下载文件摘要与订单快照不一致。")
                os.replace(temporary, target)
                return
            except (httpx.TransportError, httpx.HTTPStatusError):
                temporary.unlink(missing_ok=True)
                if attempt >= 2:
                    raise
                await self._retry_pause(attempt)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

    async def ack(self, task: LeasedTask) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/worker/v1/jobs/{task.job_id}/ack",
            retry=True,
            json={"lease_version": task.lease_version},
            headers=self.auth_headers(),
        )
        return response.json()

    async def renew(self, task: LeasedTask, *, phase: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/worker/v1/jobs/{task.job_id}/renew",
            retry=True,
            timeout=10.0,
            json={"lease_version": task.lease_version, "phase": phase},
            headers=self.auth_headers(),
        )
        return response.json()

    async def upload_result(self, task: LeasedTask, result) -> dict[str, str]:
        grants = await self._request(
            "POST",
            f"/worker/v1/jobs/{task.job_id}/result/uploads",
            retry=True,
            json={"lease_version": task.lease_version},
            headers=self.auth_headers(),
        )
        tokens = grants.json()
        uploaded: dict[str, str] = {}
        for kind, path in (
            ("result_json", result.result_json_path),
            ("result_pdf", result.result_pdf_path),
        ):
            digest = await anyio.to_thread.run_sync(_sha256_file, path)
            response = await self._request(
                "PUT",
                f"/worker/v1/jobs/{task.job_id}/result/{kind}",
                retry=True,
                content_factory=lambda path=path: _file_chunks(path),
                headers={
                    **self.auth_headers(),
                    "X-Upload-Token": tokens[kind]["upload_token"],
                    "X-Content-SHA256": digest,
                    "Content-Type": "application/octet-stream",
                },
            )
            uploaded[f"{kind}_file_id"] = response.json()["file_id"]
        return uploaded

    async def commit_result(
        self,
        task: LeasedTask,
        uploads: dict[str, str],
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/worker/v1/jobs/{task.job_id}/result/commit",
            retry=True,
            json={"lease_version": task.lease_version, **uploads},
            headers=self.auth_headers(),
        )
        return response.json()

    async def fail_job(
        self,
        task: LeasedTask,
        *,
        code: str,
        message: str = "",
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/worker/v1/jobs/{task.job_id}/fail",
            retry=True,
            json={
                "lease_version": task.lease_version,
                "code": code,
                "message": message[:500],
            },
            headers=self.auth_headers(),
        )
        return response.json()


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    async with await anyio.open_file(path, "rb") as source:
        while chunk := await source.read(_FILE_CHUNK_BYTES):
            yield chunk


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_FILE_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_name() -> str:
    import sys

    return sys.platform


def _architecture_name() -> str:
    import platform

    return platform.machine()
