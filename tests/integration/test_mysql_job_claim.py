"""Real-MySQL proof that the queue claim is atomic under true concurrency.

SQLite silently ignores `FOR UPDATE` and permits only one writer, so the
row-lock behaviour cannot be demonstrated there. This module runs only when
GRADER_TEST_MYSQL_URL points at a disposable MySQL 8 database.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from server.domain.states import JobState, OrderState
from server.models import (
    FileObject,
    GradingJob,
    GradingRound,
    Order,
    PriceRule,
    QuoteSession,
    User,
    Worker,
)
from server.models.base import Base
from server.services.leases import LeaseService
from server.services.workers import WorkerStatus


MYSQL_URL_VARIABLE = "GRADER_TEST_MYSQL_URL"
MYSQL_URL = os.getenv(MYSQL_URL_VARIABLE)

pytestmark = pytest.mark.skipif(
    not MYSQL_URL,
    reason=f"{MYSQL_URL_VARIABLE} is not set; a real MySQL 8 database is required",
)

WORKER_COUNT = 3
JOB_COUNT = 3


@pytest.fixture
def mysql_session_factory():
    engine = create_engine(MYSQL_URL, pool_pre_ping=True, pool_size=WORKER_COUNT + 2)
    with engine.begin() as connection:
        assert connection.execute(text("select 1")).scalar() == 1
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed(session_factory) -> tuple[list[str], list[str]]:
    now = datetime.now(timezone.utc)
    worker_ids: list[str] = []
    job_ids: list[str] = []
    with session_factory() as session:
        user = User(openid=f"fake:mysql-{uuid.uuid4()}", public_id=f"u-{uuid.uuid4().hex[:8]}")
        session.add(user)
        rule = PriceRule(cents_per_page=1000, effective_from=now)
        session.add(rule)
        session.flush()

        for index in range(JOB_COUNT):
            source = FileObject(
                owner_user_id=user.id,
                kind="source",
                relative_path=f"temporary/mysql-{index}.pdf",
                sha256=f"{index:064x}",
                size_bytes=1024,
                state="retained",
                expires_at=now + timedelta(days=1),
            )
            session.add(source)
            session.flush()
            quote = QuoteSession(
                owner_user_id=user.id,
                source_file_id=source.id,
                reference_file_id=None,
                price_rule_id=rule.id,
                grading_standard="imo",
                note="",
                page_count=2,
                quoted_amount_cents=2000,
                expires_at=now + timedelta(days=1),
                consumed_at=now,
            )
            session.add(quote)
            session.flush()
            order = Order(
                quote_session_id=quote.id,
                state=OrderState.V1_QUEUED,
                paid_amount_cents=2000,
                current_round_number=1,
            )
            session.add(order)
            session.flush()
            session.add(
                GradingRound(
                    order_id=order.id,
                    round_number=1,
                    grading_standard="imo",
                    note="",
                )
            )
            job = GradingJob(
                order_id=order.id,
                round_number=1,
                state=JobState.QUEUED,
                queued_at=now + timedelta(seconds=index),
                lease_version=0,
                attempt_count=0,
            )
            session.add(job)
            session.flush()
            job_ids.append(job.id)

        for index in range(WORKER_COUNT + 1):
            worker = Worker(
                installation_id=f"install-mysql-{index}",
                device_name=f"mysql-worker-{index}",
                platform="linux",
                architecture="x86_64",
                worker_version="3.0.0",
                capabilities={},
                status=WorkerStatus.ONLINE,
                last_heartbeat_at=now,
            )
            session.add(worker)
            session.flush()
            worker_ids.append(worker.worker_id)
        session.commit()
    return worker_ids, job_ids


def test_three_workers_claim_three_distinct_jobs(mysql_session_factory) -> None:
    worker_ids, job_ids = _seed(mysql_session_factory)
    service = LeaseService(mysql_session_factory)
    barrier = threading.Barrier(WORKER_COUNT)
    results: dict[str, object] = {}

    def claim(worker_id: str) -> None:
        barrier.wait()
        bundle = service.try_lease(worker_id)
        results[worker_id] = None if bundle is None else bundle.job_id

    threads = [
        threading.Thread(target=claim, args=(worker_id,))
        for worker_id in worker_ids[:WORKER_COUNT]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    claimed = [value for value in results.values() if value is not None]
    assert len(claimed) == WORKER_COUNT
    assert len(set(claimed)) == WORKER_COUNT
    assert set(claimed) == set(job_ids)

    fourth = service.try_lease(worker_ids[WORKER_COUNT])
    assert fourth is None

    with mysql_session_factory() as session:
        jobs = session.scalars(select(GradingJob)).all()
    assert {job.state for job in jobs} == {JobState.LEASED}
    assert all(job.lease_version == 1 for job in jobs)
    assert len({job.worker_id for job in jobs}) == WORKER_COUNT


def test_a_second_claim_by_the_same_worker_finds_nothing(mysql_session_factory) -> None:
    worker_ids, _ = _seed(mysql_session_factory)
    service = LeaseService(mysql_session_factory)

    first = service.try_lease(worker_ids[0])
    second = service.try_lease(worker_ids[0])

    assert first is not None
    assert second is None
