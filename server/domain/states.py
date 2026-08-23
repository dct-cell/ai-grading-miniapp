from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping


class OrderState(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    V1_QUEUED = "v1_queued"
    V1_RUNNING = "v1_running"
    V1_DELIVERED = "v1_delivered"
    V2_QUEUED = "v2_queued"
    V2_RUNNING = "v2_running"
    V2_DELIVERED = "v2_delivered"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    ACCEPTED = "accepted"


class JobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    UPLOADING = "uploading"
    SUCCEEDED = "succeeded"
    WORKER_EXCEPTION = "worker_exception"
    CANCELLED = "cancelled"


ORDER_TRANSITIONS: Final[Mapping[OrderState, frozenset[OrderState]]] = MappingProxyType(
    {
        OrderState.AWAITING_PAYMENT: frozenset({OrderState.V1_QUEUED}),
        OrderState.V1_QUEUED: frozenset(
            {OrderState.V1_RUNNING, OrderState.REFUND_PENDING}
        ),
        OrderState.V1_RUNNING: frozenset(
            {OrderState.V1_DELIVERED, OrderState.REFUND_PENDING}
        ),
        OrderState.V1_DELIVERED: frozenset(
            {
                OrderState.ACCEPTED,
                OrderState.V2_QUEUED,
                OrderState.REFUND_PENDING,
            }
        ),
        OrderState.V2_QUEUED: frozenset(
            {OrderState.V2_RUNNING, OrderState.REFUND_PENDING}
        ),
        OrderState.V2_RUNNING: frozenset(
            {OrderState.V2_DELIVERED, OrderState.REFUND_PENDING}
        ),
        OrderState.V2_DELIVERED: frozenset(
            {OrderState.ACCEPTED, OrderState.REFUND_PENDING}
        ),
        OrderState.REFUND_PENDING: frozenset(
            {OrderState.REFUNDED, OrderState.ACCEPTED}
        ),
    }
)


JOB_TRANSITIONS: Final[Mapping[JobState, frozenset[JobState]]] = MappingProxyType(
    {
        JobState.QUEUED: frozenset({JobState.LEASED, JobState.CANCELLED}),
        JobState.LEASED: frozenset(
            {
                JobState.RUNNING,
                JobState.QUEUED,
                JobState.WORKER_EXCEPTION,
            }
        ),
        JobState.RUNNING: frozenset(
            {
                JobState.UPLOADING,
                JobState.WORKER_EXCEPTION,
                JobState.CANCELLED,
            }
        ),
        JobState.UPLOADING: frozenset(
            {JobState.SUCCEEDED, JobState.WORKER_EXCEPTION}
        ),
    }
)


_EMPTY_ORDER_TARGETS: Final[frozenset[OrderState]] = frozenset()
_EMPTY_JOB_TARGETS: Final[frozenset[JobState]] = frozenset()


def require_order_transition(current: OrderState, target: OrderState) -> None:
    if target not in ORDER_TRANSITIONS.get(current, _EMPTY_ORDER_TARGETS):
        raise ValueError(f"invalid order transition: {current} -> {target}")


def require_job_transition(current: JobState, target: JobState) -> None:
    if target not in JOB_TRANSITIONS.get(current, _EMPTY_JOB_TARGETS):
        raise ValueError(f"invalid job transition: {current} -> {target}")
