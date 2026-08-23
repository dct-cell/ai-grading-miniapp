"""The scheduler must be a singleton.

Two schedulers running at once would double-process every deadline. On MySQL
that is enforced with a named advisory lock. SQLite has no such primitive, so
the guard degrades to a documented no-op — safe for the single-process local
setup, but it must fail loudly rather than pretend to protect production.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from server.scheduler.main import (
    SCHEDULER_LOCK_NAME,
    SchedulerLock,
    scheduler_loop,
)


MYSQL_URL = os.environ.get("GRADER_TEST_MYSQL_URL")
requires_mysql = pytest.mark.skipif(
    not MYSQL_URL,
    reason="GRADER_TEST_MYSQL_URL is not set; a real MySQL 8 database is required",
)


def test_lock_name_is_stable() -> None:
    """Renaming the lock would let an old and a new process both run."""
    assert SCHEDULER_LOCK_NAME == "grader-scheduler"


def test_sqlite_degrades_to_a_single_process_lock(
    session_factory: sessionmaker[Session],
) -> None:
    """SQLite cannot enforce mutual exclusion across processes.

    The lock still acquires so local development works, but it reports that it
    is not enforced so an operator is never misled into running two.
    """
    lock = SchedulerLock(session_factory)

    with lock.acquire() as acquired:
        assert acquired is True
        assert lock.enforced is False


def test_sqlite_lock_does_not_claim_to_block_a_second_holder(
    session_factory: sessionmaker[Session],
) -> None:
    """Two SQLite locks both succeed; the degradation must be honest."""
    first = SchedulerLock(session_factory)
    second = SchedulerLock(session_factory)

    with first.acquire() as first_acquired:
        with second.acquire() as second_acquired:
            assert first_acquired is True
            assert second_acquired is True
    assert first.enforced is False


@requires_mysql
def test_mysql_advisory_lock_blocks_a_second_scheduler() -> None:
    """On MySQL the second process must be refused the lock.

    This is the case that actually matters: it proves a duplicate scheduler
    cannot own scheduling at the same time as the live one.
    """
    engine = create_engine(MYSQL_URL, pool_pre_ping=True)
    factory = sessionmaker(bind=engine)
    try:
        first = SchedulerLock(factory)
        second = SchedulerLock(factory, timeout_seconds=0)

        with first.acquire() as first_acquired:
            assert first_acquired is True
            assert first.enforced is True
            with second.acquire() as second_acquired:
                assert second_acquired is False, (
                    "a second scheduler must not hold the lock"
                )

        # Once released, a new process may take it.
        third = SchedulerLock(factory, timeout_seconds=0)
        with third.acquire() as third_acquired:
            assert third_acquired is True
    finally:
        engine.dispose()


@requires_mysql
def test_mysql_lock_is_released_when_the_loop_exits() -> None:
    """A crashed scheduler must not wedge the lock for the next one."""
    engine = create_engine(MYSQL_URL, pool_pre_ping=True)
    factory = sessionmaker(bind=engine)
    try:
        lock = SchedulerLock(factory)
        with pytest.raises(RuntimeError, match="boom"):
            with lock.acquire() as acquired:
                assert acquired is True
                raise RuntimeError("boom")

        probe = SchedulerLock(factory, timeout_seconds=0)
        with probe.acquire() as reacquired:
            assert reacquired is True, "the lock must be freed on failure"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_scheduler_loop_stops_when_asked(
    session_factory: sessionmaker[Session],
    client,
) -> None:
    """The loop is cooperative so a deploy can shut it down cleanly."""
    import asyncio

    from server.adapters.payments import FakePaymentGateway
    from server.scheduler.tasks import SchedulerTasks

    tasks = SchedulerTasks(
        session_factory,
        settings=client.app.state.settings,
        gateway=FakePaymentGateway(),
    )
    stop = asyncio.Event()
    cycles: list[int] = []

    original_run_due = tasks.run_due

    def counting_run_due():
        cycles.append(1)
        if len(cycles) >= 2:
            stop.set()
        return original_run_due()

    tasks.run_due = counting_run_due# type: ignore[method-assign]

    await asyncio.wait_for(
        scheduler_loop(tasks, stop=stop, interval_seconds=0),
        timeout=5,
    )

    assert len(cycles) >= 2


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
