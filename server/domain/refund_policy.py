"""User refund routing policy.

A user-requested refund either executes immediately or waits for an Admin
decision. The rule is deliberately a pure function of four numbers so that
its edges can be pinned down by a table test: the amount ceiling is a
necessary condition, while a low monthly count *or* a low projected
cumulative ratio is sufficient alongside it.

Only user-requested refunds feed these facts. Technical refunds issued by an
Admin bypass this policy entirely and never contribute to the monthly count,
the projected numerator or the denominator — otherwise our own operational
failures would push a user towards manual review.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final


MAX_AUTOMATIC_AMOUNT_CENTS: Final[int] = 5000
MAX_AUTOMATIC_MONTHLY_COUNT: Final[int] = 4
MAX_AUTOMATIC_RATIO: Final[Decimal] = Decimal("0.30")


class RefundRoute(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True)
class RefundFacts:
    """Everything the policy is allowed to consider.

    monthly_user_refund_count is counted over Asia/Shanghai calendar month
    boundaries by the caller; the policy itself is timezone-free.
    """

    order_amount_cents: int
    monthly_user_refund_count: int
    lifetime_paid_cents: int
    lifetime_user_refunded_cents: int


def decide_refund_route(facts: RefundFacts) -> RefundRoute:
    within_amount = facts.order_amount_cents <= MAX_AUTOMATIC_AMOUNT_CENTS
    within_count = facts.monthly_user_refund_count < MAX_AUTOMATIC_MONTHLY_COUNT
    within_ratio = _within_ratio(facts)
    if within_amount and (within_count or within_ratio):
        return RefundRoute.AUTOMATIC
    return RefundRoute.MANUAL


def _within_ratio(facts: RefundFacts) -> bool:
    """Compare the projected refund ratio against the ceiling exactly.

    Decimal, not float: at these magnitudes binary floating point turns an
    exact 0.30 into 0.30000000000000004 and would send a compliant user to
    manual review. A zero denominator leaves the ratio undefined, so it can
    never be the branch that authorises an automatic refund.
    """
    if facts.lifetime_paid_cents <= 0:
        return False
    projected = facts.lifetime_user_refunded_cents + facts.order_amount_cents
    projected_ratio = Decimal(projected) / Decimal(facts.lifetime_paid_cents)
    return projected_ratio <= MAX_AUTOMATIC_RATIO
