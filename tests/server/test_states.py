from types import MappingProxyType

import pytest

from server.domain.states import JOB_TRANSITIONS, ORDER_TRANSITIONS, JobState, OrderState
from server.domain.states import require_job_transition, require_order_transition


EXPECTED_ORDER_EDGES = frozenset(
    {
        (OrderState.AWAITING_PAYMENT, OrderState.V1_QUEUED),
        (OrderState.V1_QUEUED, OrderState.V1_RUNNING),
        (OrderState.V1_QUEUED, OrderState.REFUND_PENDING),
        (OrderState.V1_RUNNING, OrderState.V1_DELIVERED),
        (OrderState.V1_RUNNING, OrderState.REFUND_PENDING),
        (OrderState.V1_DELIVERED, OrderState.ACCEPTED),
        (OrderState.V1_DELIVERED, OrderState.V2_QUEUED),
        (OrderState.V1_DELIVERED, OrderState.REFUND_PENDING),
        (OrderState.V2_QUEUED, OrderState.V2_RUNNING),
        (OrderState.V2_QUEUED, OrderState.REFUND_PENDING),
        (OrderState.V2_RUNNING, OrderState.V2_DELIVERED),
        (OrderState.V2_RUNNING, OrderState.REFUND_PENDING),
        (OrderState.V2_DELIVERED, OrderState.ACCEPTED),
        (OrderState.V2_DELIVERED, OrderState.REFUND_PENDING),
        (OrderState.REFUND_PENDING, OrderState.REFUNDED),
        (OrderState.REFUND_PENDING, OrderState.ACCEPTED),
    }
)

EXPECTED_JOB_EDGES = frozenset(
    {
        (JobState.QUEUED, JobState.LEASED),
        (JobState.QUEUED, JobState.CANCELLED),
        (JobState.LEASED, JobState.RUNNING),
        (JobState.LEASED, JobState.QUEUED),
        (JobState.LEASED, JobState.WORKER_EXCEPTION),
        (JobState.RUNNING, JobState.UPLOADING),
        (JobState.RUNNING, JobState.WORKER_EXCEPTION),
        (JobState.RUNNING, JobState.CANCELLED),
        (JobState.UPLOADING, JobState.SUCCEEDED),
        (JobState.UPLOADING, JobState.WORKER_EXCEPTION),
    }
)


def test_transition_graphs_are_deeply_immutable() -> None:
    assert isinstance(ORDER_TRANSITIONS, MappingProxyType)
    assert isinstance(JOB_TRANSITIONS, MappingProxyType)
    assert all(isinstance(targets, frozenset) for targets in ORDER_TRANSITIONS.values())
    assert all(isinstance(targets, frozenset) for targets in JOB_TRANSITIONS.values())

    with pytest.raises(TypeError):
        ORDER_TRANSITIONS[OrderState.AWAITING_PAYMENT] = frozenset()
    with pytest.raises(TypeError):
        JOB_TRANSITIONS[JobState.QUEUED] = frozenset()
    with pytest.raises(AttributeError):
        getattr(ORDER_TRANSITIONS[OrderState.AWAITING_PAYMENT], "add")(
            OrderState.REFUND_PENDING
        )
    with pytest.raises(AttributeError):
        getattr(JOB_TRANSITIONS[JobState.QUEUED], "add")(JobState.SUCCEEDED)


def test_order_transition_graph_is_exact() -> None:
    actual_edges = frozenset(
        (current, target)
        for current, targets in ORDER_TRANSITIONS.items()
        for target in targets
    )

    assert actual_edges == EXPECTED_ORDER_EDGES
    assert len(actual_edges) == 16
    assert OrderState.REFUNDED not in ORDER_TRANSITIONS
    assert OrderState.ACCEPTED not in ORDER_TRANSITIONS


def test_job_transition_graph_is_exact() -> None:
    actual_edges = frozenset(
        (current, target)
        for current, targets in JOB_TRANSITIONS.items()
        for target in targets
    )

    assert actual_edges == EXPECTED_JOB_EDGES
    assert len(actual_edges) == 10
    assert JobState.SUCCEEDED not in JOB_TRANSITIONS
    assert JobState.WORKER_EXCEPTION not in JOB_TRANSITIONS
    assert JobState.CANCELLED not in JOB_TRANSITIONS


def test_all_order_state_pairs_follow_the_transition_graph() -> None:
    for current in OrderState:
        for target in OrderState:
            if (current, target) in EXPECTED_ORDER_EDGES:
                require_order_transition(current, target)
            else:
                with pytest.raises(ValueError):
                    require_order_transition(current, target)


def test_all_job_state_pairs_follow_the_transition_graph() -> None:
    for current in JobState:
        for target in JobState:
            if (current, target) in EXPECTED_JOB_EDGES:
                require_job_transition(current, target)
            else:
                with pytest.raises(ValueError):
                    require_job_transition(current, target)


def test_invalid_transition_messages_are_exact() -> None:
    with pytest.raises(
        ValueError,
        match="^invalid order transition: v2_delivered -> v2_queued$",
    ):
        require_order_transition(OrderState.V2_DELIVERED, OrderState.V2_QUEUED)
    with pytest.raises(
        ValueError,
        match="^invalid job transition: succeeded -> queued$",
    ):
        require_job_transition(JobState.SUCCEEDED, JobState.QUEUED)


def test_v1_can_enter_review_or_refund() -> None:
    require_order_transition(OrderState.V1_DELIVERED, OrderState.V2_QUEUED)
    require_order_transition(OrderState.V1_DELIVERED, OrderState.REFUND_PENDING)


def test_v2_cannot_create_third_round() -> None:
    with pytest.raises(ValueError, match="invalid order transition"):
        require_order_transition(OrderState.V2_DELIVERED, OrderState.V2_QUEUED)


def test_running_job_can_enter_worker_exception() -> None:
    require_job_transition(JobState.RUNNING, JobState.WORKER_EXCEPTION)
