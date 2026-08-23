from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from worker.config import WorkerSettings


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

    @classmethod
    def from_bundle(cls, bundle: dict[str, Any]) -> LeasedTask:
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
            source_file_id=bundle["source_file"]["file_id"],
            source_download_token=bundle["source_file"]["download_token"],
            reference_file_id=None if reference is None else reference["file_id"],
            reference_download_token=(
                None if reference is None else reference["download_token"]
            ),
        )


class WorkerClient:
    """Outbound-only HTTPS client. The server never calls back into the host."""

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._worker_id = settings.worker_id

    @property
    def worker_id(self) -> str | None:
        return self._worker_id

    def auth_headers(self) -> dict[str, str]:
        """Build the two-part Worker credential.

        The bearer key alone cannot act as a Worker; the server also requires a
        registered X-Worker-ID, which does not exist until registration.
        """
        headers = {"Authorization": f"Bearer {self._settings.shared_key}"}
        if self._worker_id:
            headers["X-Worker-ID"] = self._worker_id
        return headers

    def _url(self, path: str) -> str:
        return f"{self._settings.server_base_url}{path}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._settings.request_timeout_seconds)

    async def register(self) -> dict[str, Any]:
        payload = {
            "installation_id": self._settings.installation_id,
            "device_name": self._settings.device_name or self._settings.installation_id,
            "platform": _platform_name(),
            "architecture": _architecture_name(),
            "worker_version": self._settings.worker_version,
        }
        async with self._client() as client:
            response = await client.post(
                self._url("/worker/v1/register"),
                json=payload,
                headers={"Authorization": f"Bearer {self._settings.shared_key}"},
            )
        response.raise_for_status()
        body = response.json()
        self._worker_id = body["worker_id"]
        return body

    async def heartbeat(self, *, phase: str | None = None) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                self._url("/worker/v1/heartbeat"),
                json={"phase": phase},
                headers=self.auth_headers(),
            )
        response.raise_for_status()
        return response.json()

    async def lease(self, *, wait_seconds: int | None = None) -> LeasedTask | None:
        wait = self._settings.poll_wait_seconds if wait_seconds is None else wait_seconds
        async with self._client() as client:
            response = await client.post(
                self._url("/worker/v1/jobs/lease"),
                headers={**self.auth_headers(), "Prefer": f"wait={wait}"},
                timeout=wait + self._settings.request_timeout_seconds,
            )
        if response.status_code == httpx.codes.NO_CONTENT:
            return None
        response.raise_for_status()
        return LeasedTask.from_bundle(response.json())

    async def download_bundle(self, task: LeasedTask, workspace: Path) -> Path:
        """Materialise the task inputs into a staging directory.

        Downloads the source PDF and (if present) the reference PDF via
        the approved ``GET /worker/v1/jobs/{job_id}/bundle/{kind}``
        endpoint. Files land in ``workspace/staging/`` so
        ``prepare_workspace`` can copy them into the canonical ``input/``
        layout the legacy runner expects.

        Instructions never travel over the network here — the note is
        already on the LeasedTask and ``prepare_workspace`` writes it
        to ``input/instructions.txt`` from the bundle.
        """
        staging = workspace / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        await self._download_file(
            task, kind="source", token=task.source_download_token,
            target=staging / "source.pdf",
        )
        if task.reference_download_token is not None:
            await self._download_file(
                task, kind="reference",
                token=task.reference_download_token,
                target=staging / "reference.pdf",
            )
        return staging

    async def _download_file(
        self,
        task: LeasedTask,
        *,
        kind: str,
        token: str,
        target: Path,
    ) -> None:
        async with self._client() as client:
            response = await client.get(
                self._url(
                    f"/worker/v1/jobs/{task.job_id}/bundle/{kind}"
                ),
                headers={
                    **self.auth_headers(),
                    "X-Download-Token": token,
                },
            )
        if response.status_code == httpx.codes.NOT_FOUND and kind == "reference":
            # No reference file on this order — that is a legitimate
            # state, not an error.
            return
        response.raise_for_status()
        target.write_bytes(response.content)

    async def ack(self, task: LeasedTask) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                self._url(f"/worker/v1/jobs/{task.job_id}/ack"),
                json={"lease_version": task.lease_version},
                headers=self.auth_headers(),
            )
        response.raise_for_status()
        return response.json()

    async def renew(self, task: LeasedTask, *, phase: str) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                self._url(f"/worker/v1/jobs/{task.job_id}/renew"),
                json={"lease_version": task.lease_version, "phase": phase},
                headers=self.auth_headers(),
            )
        response.raise_for_status()
        return response.json()

    async def upload_result(self, task: LeasedTask, result) -> dict[str, str]:
        async with self._client() as client:
            grants = await client.post(
                self._url(f"/worker/v1/jobs/{task.job_id}/result/uploads"),
                json={"lease_version": task.lease_version},
                headers=self.auth_headers(),
            )
            grants.raise_for_status()
            tokens = grants.json()

            uploaded: dict[str, str] = {}
            for kind, path in (
                ("result_json", result.result_json_path),
                ("result_pdf", result.result_pdf_path),
            ):
                payload = path.read_bytes()
                response = await client.put(
                    self._url(f"/worker/v1/jobs/{task.job_id}/result/{kind}"),
                    content=payload,
                    headers={
                        **self.auth_headers(),
                        "X-Upload-Token": tokens[kind]["upload_token"],
                        "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                        "Content-Type": "application/octet-stream",
                    },
                )
                response.raise_for_status()
                uploaded[f"{kind}_file_id"] = response.json()["file_id"]
        return uploaded

    async def commit_result(
        self,
        task: LeasedTask,
        uploads: dict[str, str],
    ) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                self._url(f"/worker/v1/jobs/{task.job_id}/result/commit"),
                json={"lease_version": task.lease_version, **uploads},
                headers=self.auth_headers(),
            )
        response.raise_for_status()
        return response.json()


def _platform_name() -> str:
    import sys

    return sys.platform


def _architecture_name() -> str:
    import platform

    return platform.machine()
