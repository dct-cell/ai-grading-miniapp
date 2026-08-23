from __future__ import annotations

import shutil
from pathlib import Path
from typing import Awaitable, Callable

import anyio

from worker.client import LeasedTask
from worker.runtime.contracts import TaskBundle
from worker.runtime.workspace import prepare_workspace


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
            await self.client.download_bundle(task, workspace)
            await self.client.ack(task)
            async with LeaseRenewer(
                self.client, task, interval_seconds=self.renew_interval_seconds
            ) as renewer:
                bundle = _build_bundle(task, workspace)
                prepare_workspace(workspace, bundle)
                progress = self._build_progress(renewer)
                result = await self.runtime.run(workspace, bundle, progress)
                uploads = await self.client.upload_result(task, result)
                await self.client.commit_result(task, uploads)
        finally:
            shutil.rmtree(self.workspace_root / task.job_id, ignore_errors=True)
        return True

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
        while not self._draining:
            await self.run_one_poll()
