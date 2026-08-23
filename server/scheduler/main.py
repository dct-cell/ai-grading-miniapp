"""The single scheduler process.

Exactly one scheduler may own the system's deadlines. Two would double-process
every acceptance window and re-drive every failed refund concurrently.

On MySQL that is enforced with a named advisory lock (``GET_LOCK``), which is
held for the lifetime of one database session and released automatically if the
process dies — including a hard kill, since the server drops the lock when the
connection goes away. SQLite has no equivalent primitive, so the lock degrades
to a no-op and reports ``enforced = False``: honest degradation for the
single-process local setup, rather than a guard that silently protects nothing.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Final, Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from server.scheduler.tasks import SchedulerTasks


SCHEDULER_LOCK_NAME: Final[str] = "grader-scheduler"
DEFAULT_INTERVAL_SECONDS: Final[int] = 20

logger = logging.getLogger("server.scheduler")


class SchedulerLock:
    """A cross-process mutex for scheduling, where the backend supports one."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        name: str = SCHEDULER_LOCK_NAME,
        timeout_seconds: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._name = name
        self._timeout_seconds = timeout_seconds
        self._enforced = False

    @property
    def enforced(self) -> bool:
        """Whether the last acquisition was backed by a real database lock."""
        return self._enforced

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        """Hold the lock for the duration of the block.

        Yields True when scheduling may proceed. The session stays open for the
        whole block because a MySQL advisory lock belongs to its connection:
        returning the connection to the pool would release the lock and let a
        second scheduler start.
        """
        with self._session_factory() as session:
            dialect = session.get_bind().dialect.name
            if dialect != "mysql":
                # No advisory locks here. Allow a single local process to run
                # and record that nothing is actually being enforced.
                self._enforced = False
                logger.warning(
                    "scheduler lock is not enforced on %s; run exactly one "
                    "scheduler process",
                    dialect,
                )
                yield True
                return

            acquired = session.scalar(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": self._name, "timeout": self._timeout_seconds},
            )
            if acquired != 1:
                self._enforced = True
                yield False
                return

            self._enforced = True
            try:
                yield True
            finally:
                # Release explicitly so a long-lived pooled connection cannot
                # keep the lock after this process is finished with it.
                session.execute(
                    text("SELECT RELEASE_LOCK(:name)"), {"name": self._name}
                )


async def scheduler_loop(
    tasks: SchedulerTasks,
    *,
    stop: asyncio.Event | None = None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Run every due task on a fixed interval until asked to stop.

    Cooperative shutdown matters for deploys: the loop finishes the cycle it is
    in, then exits and releases the advisory lock, so the next process can take
    over without an overlap window.
    """
    stop_event = stop or asyncio.Event()
    while not stop_event.is_set():
        reports = tasks.run_due()
        for report in reports.values():
            if report.failed:
                logger.error(
                    "scheduler task %s failed: %s", report.name, report.error
                )
        if stop_event.is_set():
            break
        if interval_seconds <= 0:
            # Yield control so a cooperative test loop can make progress.
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            continue


def main() -> None:  # pragma: no cover - process entry point
    """Start the scheduler, refusing to run if another already holds the lock."""
    from server.config import ServerSettings
    from server.db import create_session_factory
    from server.adapters.payments import FakePaymentGateway

    logging.basicConfig(level=logging.INFO)
    settings = ServerSettings()
    session_factory = create_session_factory(settings.database_url)
    tasks = SchedulerTasks(
        session_factory,
        settings=settings,
        gateway=FakePaymentGateway(),
    )
    lock = SchedulerLock(session_factory)
    with lock.acquire() as acquired:
        if not acquired:
            logger.error(
                "another scheduler already holds %s; exiting",
                SCHEDULER_LOCK_NAME,
            )
            return
        asyncio.run(scheduler_loop(tasks))


if __name__ == "__main__":  # pragma: no cover
    main()
