"""V1 acceptance, one review, and full refund.

The V1 delivery window offers exactly three mutually exclusive outcomes:
accept, one review (which buys a second grading round), or a full refund.
These tests pin down ownership, the state machine, the one-review rule and
the accept/review/refund exclusivity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import (
    Appeal,
    FileObject,
    GradingJob,
    GradingRound,
    Order,
    QuoteSession,
    Refund,
)
from tests.server.conftest import (
    authenticate,
    deliver_v1_order,
    pay_for_new_order,
    register_worker,
    worker_headers,
)


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def v1_order(authenticated_client: TestClient) -> str:
    return deliver_v1_order(authenticated_client)["order_id"]


def test_v1_review_creates_exactly_one_v2_job(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    response = authenticated_client.post(
        f"/api/v1/orders/{v1_order}/review",
        json={"text": "第2题下界证明判断有误"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["state"] == "v2_queued"

    with session_factory() as session:
        jobs = session.scalars(
            select(GradingJob).where(GradingJob.order_id == v1_order)
        ).all()
        rounds = session.scalars(
            select(GradingRound).where(GradingRound.order_id == v1_order)
        ).all()
        appeal = session.scalar(select(Appeal).where(Appeal.order_id == v1_order))
        order = session.get(Order, v1_order)

    assert {job.round_number for job in jobs} == {1, 2}
    round_two = [job for job in jobs if job.round_number == 2]
    assert len(round_two) == 1
    assert round_two[0].state == JobState.QUEUED
    assert round_two[0].lease_version == 0
    assert round_two[0].attempt_count == 0
    assert round_two[0].worker_id is None
    assert {record.round_number for record in rounds} == {1, 2}
    assert appeal is not None
    assert appeal.text == "第2题下界证明判断有误"
    assert order.state == OrderState.V2_QUEUED
    assert order.current_round_number == 2


def test_v1_review_reuses_the_immutable_source_files(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Round 2 grades the same PDFs; a review never uploads a replacement."""
    delivered = deliver_v1_order(authenticated_client)
    order_id = delivered["order_id"]

    with session_factory() as session:
        order = session.get(Order, order_id)
        quote = session.get(QuoteSession, order.quote_session_id)
        before = (quote.source_file_id, quote.reference_file_id)
        files_before = count(session, FileObject)

    assert (
        authenticated_client.post(
            f"/api/v1/orders/{order_id}/review", json={"text": "请复核第3 题"}
        ).status_code
        == 202
    )

    with session_factory() as session:
        order = session.get(Order, order_id)
        quote = session.get(QuoteSession, order.quote_session_id)
        after = (quote.source_file_id, quote.reference_file_id)
        files_after = count(session, FileObject)
        round_two = session.scalar(
            select(GradingRound).where(
                GradingRound.order_id == order_id,
                GradingRound.round_number == 2,
            )
        )
        round_one = session.scalar(
            select(GradingRound).where(
                GradingRound.order_id == order_id,
                GradingRound.round_number == 1,
            )
        )

    assert after == before
    assert files_after == files_before, "a review must not create new files"
    # Round 2 grades against the same standard and note as round 1.
    assert round_two.grading_standard == round_one.grading_standard
    assert round_two.note == round_one.note
    assert round_two.result_json_file_id is None
    assert round_two.result_pdf_file_id is None
    assert round_two.delivered_at is None


def test_v1_review_is_allowed_only_once(
    authenticated_client: TestClient,
    v1_order: str,
) -> None:
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/review", json={"text": "第一次复核"}
        ).status_code
        == 202
    )
    second = authenticated_client.post(
        f"/api/v1/orders/{v1_order}/review", json={"text": "第二次复核"}
    )

    assert second.status_code == 409


def test_v1_accept_closes_the_order(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    response = authenticated_client.post(f"/api/v1/orders/{v1_order}/accept")

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "accepted"

    with session_factory() as session:
        order = session.get(Order, v1_order)

    assert order.state == OrderState.ACCEPTED
    # An accepted order has no further actions.
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/review", json={"text": "太晚了"}
        ).status_code
        == 409
    )
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/refund", json={"reason": "grading_disputed"}
        ).status_code
        == 409
    )


def test_v1_refund_ends_review_path(
    authenticated_client: TestClient,
    v1_order: str,
) -> None:
    response = authenticated_client.post(
        f"/api/v1/orders/{v1_order}/refund",
        json={"reason": "uploaded_wrong_pdf"},
    )

    assert response.status_code in {200, 202}, response.text
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/review",
            json={"text": "再次提交"},
        ).status_code
        == 409
    )


def test_v1_refund_requests_the_full_paid_amount(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    """A refund is always the whole order; the client cannot choose an amount."""
    response = authenticated_client.post(
        f"/api/v1/orders/{v1_order}/refund",
        json={"reason": "uploaded_wrong_pdf", "amount_cents": 1},
    )

    assert response.status_code in {200, 202}, response.text
    with session_factory() as session:
        order = session.get(Order, v1_order)
        refunds = session.scalars(select(Refund)).all()

    assert len(refunds) == 1
    assert refunds[0].amount_cents == order.paid_amount_cents
    assert refunds[0].source == "user"
    assert response.json()["amount_cents"] == order.paid_amount_cents


def test_refund_never_skips_refund_pending(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    """Even an automatic refund passes through REFUND_PENDING.

    The state machine has no V1_DELIVERED -> REFUNDED edge, so an
    implementation that jumped straight to the terminal state would have to
    bypass require_order_transition.
    """
    authenticated_client.post(
        f"/api/v1/orders/{v1_order}/refund", json={"reason": "uploaded_wrong_pdf"}
    )

    with session_factory() as session:
        order = session.get(Order, v1_order)

    assert order.state in {OrderState.REFUND_PENDING, OrderState.REFUNDED}


def test_only_the_owner_can_act_on_an_order(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A second account must not even learn the order exists."""
    authenticate(client, code="test-owner-parent")
    order_id = deliver_v1_order(client)["order_id"]

    authenticate(client, code="test-other-parent")
    for path, payload in (
        (f"/api/v1/orders/{order_id}/accept", None),
        (f"/api/v1/orders/{order_id}/review", {"text": "不是我的订单"}),
        (f"/api/v1/orders/{order_id}/refund", {"reason": "grading_disputed"}),
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 404, (path, response.text)

    with session_factory() as session:
        assert count(session, Appeal) == 0
        assert count(session, Refund) == 0
        assert session.get(Order, order_id).state == OrderState.V1_DELIVERED


def test_aftersales_requires_authentication(
    authenticated_client: TestClient,
    v1_order: str,
) -> None:
    unauthenticated = TestClient(authenticated_client.app)
    for path, payload in (
        (f"/api/v1/orders/{v1_order}/accept", None),
        (f"/api/v1/orders/{v1_order}/review", {"text": "匿名"}),
        (f"/api/v1/orders/{v1_order}/refund", {"reason": "grading_disputed"}),
    ):
        assert unauthenticated.post(path, json=payload).status_code == 401, path


def test_actions_are_rejected_before_delivery(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A queued order has nothing to accept or review yet."""
    order_id = pay_for_new_order(authenticated_client)

    assert (
        authenticated_client.post(f"/api/v1/orders/{order_id}/accept").status_code == 409
    )
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{order_id}/review", json={"text": "还没批完"}
        ).status_code
        == 409
    )

    with session_factory() as session:
        assert session.get(Order, order_id).state == OrderState.V1_QUEUED


def test_refund_is_allowed_while_still_grading(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A user who uploaded the wrong PDF should not have to wait for delivery."""
    order_id = pay_for_new_order(authenticated_client)

    response = authenticated_client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": "uploaded_wrong_pdf"}
    )

    assert response.status_code in {200, 202}, response.text
    with session_factory() as session:
        order = session.get(Order, order_id)
        job = session.scalar(
            select(GradingJob).where(GradingJob.order_id == order_id)
        )
    assert order.state in {OrderState.REFUND_PENDING, OrderState.REFUNDED}
    assert job.state == JobState.CANCELLED
    assert job.ack_deadline is None
    assert job.lease_expires_at is None


def test_refunded_queued_job_cannot_block_the_next_order(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first_order = pay_for_new_order(authenticated_client)
    second_order = pay_for_new_order(authenticated_client)
    refunded = authenticated_client.post(
        f"/api/v1/orders/{first_order}/refund",
        json={"reason": "uploaded_wrong_pdf"},
    )
    assert refunded.status_code in {200, 202}, refunded.text

    worker_id = register_worker(
        authenticated_client,
        installation_id="install-after-queued-refund",
    )["worker_id"]
    leased = authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )

    assert leased.status_code == 200, leased.text
    assert leased.json()["order_id"] == second_order
    with session_factory() as session:
        first_job = session.scalar(
            select(GradingJob).where(GradingJob.order_id == first_order)
        )
    assert first_job.state == JobState.CANCELLED


def test_actions_are_rejected_after_the_acceptance_deadline(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    """The three-day window is enforced on the server, not in the client."""
    with session_factory() as session:
        order = session.get(Order, v1_order)
        order.acceptance_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(order)
        session.commit()

    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/review", json={"text": "过期复核"}
        ).status_code
        == 409
    )
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/refund", json={"reason": "grading_disputed"}
        ).status_code
        == 409
    )
    # Accepting late is harmless — the scheduler would have done it anyway.
    assert (
        authenticated_client.post(f"/api/v1/orders/{v1_order}/accept").status_code == 200
    )


def test_review_text_is_required_and_bounded(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/review", json={"text": "   "}
        ).status_code
        == 422
    )
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/review", json={"text": "x" * 2001}
        ).status_code
        == 422
    )

    with session_factory() as session:
        assert count(session, Appeal) == 0
        assert session.get(Order, v1_order).state == OrderState.V1_DELIVERED


def test_unknown_refund_reason_is_rejected(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    response = authenticated_client.post(
        f"/api/v1/orders/{v1_order}/refund", json={"reason": "give-me-money"}
    )

    assert response.status_code == 422
    with session_factory() as session:
        assert count(session, Refund) == 0


def test_refund_closes_the_review_path_but_not_the_reverse(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Sequential ordering follows the locked state machine, not symmetry.

    Once a refund is pending, review is gone: REFUND_PENDING has no outgoing
    review edge, and refunding then re-grading for free would be a loophole.

    The reverse is deliberately allowed. ORDER_TRANSITIONS contains
    V2_QUEUED -> REFUND_PENDING, and the roadmap states that any non-terminal
    order may enter REFUND_PENDING — a user who asked for a re-grade and is
    still unhappy must not be trapped without a refund.
    """
    refund_first = deliver_v1_order(authenticated_client)["order_id"]
    assert authenticated_client.post(
        f"/api/v1/orders/{refund_first}/refund",
        json={"reason": "grading_disputed"},
    ).status_code in {200, 202}
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{refund_first}/review", json={"text": "退款后复核"}
        ).status_code
        == 409
    )

    review_first = deliver_v1_order(authenticated_client)["order_id"]
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{review_first}/review", json={"text": "先复核"}
        ).status_code
        == 202
    )
    assert authenticated_client.post(
        f"/api/v1/orders/{review_first}/refund",
        json={"reason": "grading_disputed"},
    ).status_code in {200, 202}

    with session_factory() as session:
        # The refunded-first order never bought a second round.
        assert (
            session.scalar(select(Appeal).where(Appeal.order_id == refund_first)) is None
        )
        assert {
            job.round_number
            for job in session.scalars(
                select(GradingJob).where(GradingJob.order_id == refund_first)
            ).all()
        } == {1}


def test_a_second_refund_request_is_rejected(
    authenticated_client: TestClient,
    v1_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    """One pending refund per order; a retry must not duplicate the money."""
    assert authenticated_client.post(
        f"/api/v1/orders/{v1_order}/refund", json={"reason": "grading_disputed"}
    ).status_code in {200, 202}
    assert (
        authenticated_client.post(
            f"/api/v1/orders/{v1_order}/refund", json={"reason": "grading_disputed"}
        ).status_code
        == 409
    )

    with session_factory() as session:
        assert count(session, Refund) == 1


def test_concurrent_review_and_refund_leave_one_winner(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """The loser of a genuine interleaving must lose on re-read, not on a guess.

    A check-then-write implementation passes the sequential test above but
    breaks here: this drives the refund through while the review transaction
    is suspended between reading the order and writing the appeal, which is
    exactly the window MySQL would expose. The review must still fail.
    """
    from server.services import aftersales as aftersales_module

    order_id = deliver_v1_order(authenticated_client)["order_id"]
    interleaved: list[str] = []
    original_hook = aftersales_module._after_state_check

    def refund_during_the_review_transaction(action: str) -> None:
        if action != "review" or interleaved:
            return
        interleaved.append(action)
        response = authenticated_client.post(
            f"/api/v1/orders/{order_id}/refund", json={"reason": "grading_disputed"}
        )
        assert response.status_code in {200, 202}, response.text

    monkeypatch.setattr(
        aftersales_module, "_after_state_check", refund_during_the_review_transaction
    )
    review = authenticated_client.post(
        f"/api/v1/orders/{order_id}/review", json={"text": "并发复核"}
    )
    monkeypatch.setattr(aftersales_module, "_after_state_check", original_hook)

    assert interleaved == ["review"], "the interleaving never happened"
    assert review.status_code == 409, review.text

    with session_factory() as session:
        appeals = session.scalars(
            select(Appeal).where(Appeal.order_id == order_id)
        ).all()
        refunds = session.scalars(select(Refund)).all()
        jobs = session.scalars(
            select(GradingJob).where(GradingJob.order_id == order_id)
        ).all()

    assert appeals == []
    assert len(refunds) == 1
    assert {job.round_number for job in jobs} == {1}, "no V2 job may be created"


def test_worker_commit_after_a_refund_is_a_conflict_not_a_crash(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A refund can land while a Worker is still uploading its result.

    Phase 05 made this reachable by allowing a refund during grading. The
    commit must be refused as a lease conflict — the outcome the Worker daemon
    already knows how to handle — rather than raising an unhandled ValueError,
    which would surface as a 500 and strand the job in ``uploading`` forever.
    """
    import hashlib

    from tests.server.conftest import RESULT_JSON, make_pdf_bytes

    order_id = pay_for_new_order(authenticated_client, pages=2)
    worker_id = register_worker(
        authenticated_client, installation_id="install-refund-midflight"
    )["worker_id"]
    job = authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    ).json()
    lease_version = job["lease_version"]
    authenticated_client.post(
        f"/worker/v1/jobs/{job['job_id']}/ack",
        json={"lease_version": lease_version},
        headers=worker_headers(worker_id),
    )
    grants = authenticated_client.post(
        f"/worker/v1/jobs/{job['job_id']}/result/uploads",
        json={"lease_version": lease_version},
        headers=worker_headers(worker_id),
    ).json()
    uploaded = {}
    for kind, payload in (
        ("result_json", RESULT_JSON),
        ("result_pdf", make_pdf_bytes(3)),
    ):
        response = authenticated_client.put(
            f"/worker/v1/jobs/{job['job_id']}/result/{kind}",
            content=payload,
            headers={
                **worker_headers(worker_id),
                "X-Upload-Token": grants[kind]["upload_token"],
                "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                "Content-Type": "application/octet-stream",
            },
        )
        uploaded[f"{kind}_file_id"] = response.json()["file_id"]

    refunded = authenticated_client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": "uploaded_wrong_pdf"}
    )
    assert refunded.status_code in {200, 202}, refunded.text

    commit = authenticated_client.post(
        f"/worker/v1/jobs/{job['job_id']}/result/commit",
        json={"lease_version": lease_version, **uploaded},
        headers=worker_headers(worker_id),
    )

    assert commit.status_code == 409, commit.text
    with session_factory() as session:
        order = session.get(Order, order_id)
    # The refund stands; a late delivery must not resurrect the order.
    assert order.state in {OrderState.REFUND_PENDING, OrderState.REFUNDED}


def test_review_after_refund_does_not_queue_work(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A rejected review must leave no queued job for a Worker to pick up."""
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    authenticated_client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": "uploaded_wrong_pdf"}
    )
    authenticated_client.post(
        f"/api/v1/orders/{order_id}/review", json={"text": "被拒绝的复核"}
    )

    worker_id = register_worker(
        authenticated_client, installation_id="install-after-refund"
    )["worker_id"]
    leased = authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )

    assert leased.status_code == 204, leased.text
    with session_factory() as session:
        queued = session.scalars(
            select(GradingJob).where(GradingJob.state == JobState.QUEUED)
        ).all()
    assert queued == []
