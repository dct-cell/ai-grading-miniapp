from __future__ import annotations

import logging
import random
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

import anyio
import httpx

from worker.client import (
    DownloadDigestMismatch,
    LeaseLost,
    LeasedTask,
    WorkerAuthenticationError,
)
from worker.runtime.contracts import TaskBundle
from worker.runtime.legacy_codex import RuntimeExecutionError
from worker.runtime.workspace import cleanup_transient_artifacts, prepare_workspace


logger = logging.getLogger(__name__)

_LEASE_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)


class LeaseRenewer:
    """Keeps a lease alive for as long as the grading body runs.

    The renewal loop is cancelled on exit, so a committed or failed job stops
    renewing immediately rather than holding a lease it no longer owns. The
    runtime can update the phase the renewer reports via ``update_phase`` so
    the server sees the current grading stage without progress triggering
    extra renewals.
    """

    def __init__(self, client, task, *, interval_seconds: float) -> None:
        self._client = client
        self._task = task
        self._interval_seconds = interval_seconds
        self._task_group: anyio.abc.TaskGroup | None = None
        self._phase = "grading"

    def update_phase(self, phase: str) -> None:
        """Update the phase the next renewal reports.

        The renewer polls on a timer; this only stamps the phase so the next
        tick reports the latest stage. It does not trigger an extra renewal.
        """
        if phase:
            self._phase = phase

    async def _loop(self) -> None:
        while True:
            await anyio.sleep(self._interval_seconds)
            await self._client.renew(self._task, phase=self._phase)

    async def __aenter__(self) -> LeaseRenewer:
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._loop)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool | None:
        assert self._task_group is not None
        self._task_group.cancel_scope.cancel()
        try:
            return await self._task_group.__aexit__(exc_type, exc, traceback)
        except BaseExceptionGroup as group:
            # A task group reports the body's failure as a group. Unwrap a lone
            # exception so operators see the real grading error rather than an
            # opaque ExceptionGroup.
            if len(group.exceptions) == 1:
                raise group.exceptions[0] from None
            raise


def _build_bundle(task: LeasedTask, workspace: Path) -> TaskBundle:
    """Convert a leased task plus its downloaded files into a TaskBundle.

    The Worker client downloads source/reference PDFs into
    ``workspace/staging/`` before the runtime runs; the bundle points at those
    staging paths and ``prepare_workspace`` copies them into the canonical
    ``input/`` layout. The server has already frozen the tier and league scope,
    so the Worker forwards both values without guessing.
    """
    staging = workspace / "staging"
    reference_path = staging / "reference.pdf"
    return TaskBundle(
        job_id=task.job_id,
        order_id=task.order_id,
        round_number=task.round_number,
        service_tier=task.service_tier,
        grading_standard=task.grading_standard,
        league_scope=task.league_scope,
        source_pdf=str(staging / "source.pdf"),
        reference_pdf=str(reference_path) if reference_path.is_file() else None,
        page_count=task.page_count,
        note=task.note,
    )


class WorkerDaemon:
    """Runs the outbound protocol for at most one job at a time."""

    def __init__(
        self,
        *,
        client,
        runtime,
        workspace_root: Path,
        renew_interval_seconds: float = 20.0,
    ) -> None:
        self.client = client
        self.runtime = runtime
        self.workspace_root = Path(workspace_root)
        self.renew_interval_seconds = renew_interval_seconds
        self._draining = False

    @property
    def draining(self) -> bool:
        return self._draining

    def request_drain(self) -> None:
        """Finish the current job, then stop polling for new work."""
        self._draining = True

    async def run_one_poll(self) -> bool:
        """Lease, grade, upload and commit at most one job.

        Returns True when a job was processed. The workspace is always removed,
        so a failed attempt cannot leak student data or confuse a later lease.
        """
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        task = await self.client.lease()
        if task is None:
            return False

        workspace = self.workspace_root / task.job_id / str(task.lease_version)
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            await self.client.ack(task)
            async with LeaseRenewer(
                self.client, task, interval_seconds=self.renew_interval_seconds
            ) as renewer:
                await self.client.download_bundle(task, workspace)
                bundle = _build_bundle(task, workspace)
                prepare_workspace(workspace, bundle)
                progress = self._build_progress(renewer)
                result = await self.runtime.run(workspace, bundle, progress)
                cleanup_transient_artifacts(workspace)
                uploads = await self.client.upload_result(task, result)
                await self.client.commit_result(task, uploads)
        except WorkerAuthenticationError:
            raise
        except LeaseLost:
            # Refund/cancellation and a genuinely lost fence are both normal
            # terminal outcomes for this local attempt.  The server is already
            # authoritative, so no second failure report is appropriate.
            logger.info("worker lease ended before delivery", extra={"job_id": task.job_id})
        except RuntimeExecutionError as error:
            await self._report_failure(
                task,
                code=error.code,
                message=error.message,
            )
        except DownloadDigestMismatch:
            await self._report_failure(task, code="bundle_invalid")
        except Exception as error:  # noqa: BLE001 - isolate one paid task
            logger.exception("worker task failed", extra={"job_id": task.job_id})
            await self._report_failure(
                task,
                code=(
                    "control_plane_unavailable"
                    if isinstance(error, (httpx.TransportError, httpx.HTTPStatusError))
                    else "worker_exception"
                ),
            )
        finally:
            try:
                shutil.rmtree(self.workspace_root / task.job_id)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    "failed to remove worker workspace",
                    extra={"job_id": task.job_id},
                )
        return True

    async def _report_failure(
        self,
        task: LeasedTask,
        *,
        code: str,
        message: str = "",
    ) -> None:
        try:
            await self.client.fail_job(task, code=code, message=message)
        except LeaseLost:
            return
        except WorkerAuthenticationError:
            raise
        except Exception:  # noqa: BLE001 - lease expiry is the fallback
            logger.exception(
                "failed to report worker task failure",
                extra={"job_id": task.job_id, "failure_code": code},
            )

    def _build_progress(
        self, renewer: LeaseRenewer
    ) -> Callable[[str], Awaitable[None]]:
        """Forward runtime stage updates to the lease renewer.

        The renewer polls on a timer; ``progress`` only stamps the phase so
        the next tick reports the latest stage. It does not trigger an extra
        renewal, so the protocol call order stays ``lease, download, ack,
        upload, commit`` with renewals interleaved on the timer.
        """
        async def _progress(stage: str) -> None:
            renewer.update_phase(stage)

        return _progress

    async def run_forever(self) -> None:
        """Poll until a drain is requested."""
        failures = 0
        while not self._draining:
            try:
                processed = await self.run_one_poll()
            except WorkerAuthenticationError:
                raise
            except (httpx.TransportError, httpx.HTTPStatusError):
                delay = _LEASE_BACKOFF_SECONDS[
                    min(failures, len(_LEASE_BACKOFF_SECONDS) - 1)
                ]
                failures += 1
                await anyio.sleep(delay * random.uniform(0.8, 1.2))
                continue
            failures = 0
            if not processed:
                # A correctly configured long poll already waited 1-25s.  The
                # floor only protects against a proxy returning immediate 204s.
                await anyio.sleep(0.25)

    def cleanup_stale_workspaces(self, *, older_than_seconds: int) -> int:
        """Remove only job directories older than the maximum safe run age."""
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - max(0, older_than_seconds)
        removed = 0
        for job_dir in self.workspace_root.iterdir():
            try:
                if not job_dir.is_dir() or job_dir.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(job_dir)
                removed += 1
            except OSError:
                logger.exception(
                    "failed to remove stale worker workspace",
                    extra={"workspace": str(job_dir)},
                )
        return removed
