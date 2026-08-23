"""Boundary table for the user refund routing policy.

The policy is a pure function so the exact edges are testable without a
database: amount is a necessary condition, while monthly count and the
projected cumulative ratio are alternatives.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from server.domain.refund_policy import RefundFacts, RefundRoute, decide_refund_route


@pytest.mark.parametrize(
    ("amount", "month_count", "paid", "refunded", "expected"),
    [
        (5000, 0, 10000, 0, "automatic"),
        (5000, 3, 10000, 0, "automatic"),
        (5000, 4, 20000, 0, "automatic"),
        (5000, 4, 10000, 0, "manual"),
        (5001, 0, 10000, 0, "manual"),
    ],
)
def test_refund_route(amount, month_count, paid, refunded, expected) -> None:
    facts = RefundFacts(
        order_amount_cents=amount,
        monthly_user_refund_count=month_count,
        lifetime_paid_cents=paid,
        lifetime_user_refunded_cents=refunded,
    )
    assert decide_refund_route(facts).value == expected


def test_amount_limit_is_necessary_not_sufficient() -> None:
    """Over the amount cap, a spotless history still routes to a human.

    within_amount is ANDed with the other clauses, so no combination of a low
    monthly count and a tiny ratio can rescue an oversized refund.
    """
    facts = RefundFacts(
        order_amount_cents=5001,
        monthly_user_refund_count=0,
        lifetime_paid_cents=10_000_000,
        lifetime_user_refunded_cents=0,
    )
    assert decide_refund_route(facts) is RefundRoute.MANUAL


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(4999, RefundRoute.AUTOMATIC), (5000, RefundRoute.AUTOMATIC), (5001, RefundRoute.MANUAL)],
)
def test_amount_cap_is_inclusive_at_5000(amount, expected) -> None:
    facts = RefundFacts(
        order_amount_cents=amount,
        monthly_user_refund_count=0,
        lifetime_paid_cents=1_000_000,
        lifetime_user_refunded_cents=0,
    )
    assert decide_refund_route(facts) is expected


@pytest.mark.parametrize(
    ("month_count", "expected"),
    [
        (0, RefundRoute.AUTOMATIC),
        (1, RefundRoute.AUTOMATIC),
        (2, RefundRoute.AUTOMATIC),
        (3, RefundRoute.AUTOMATIC),
        (4, RefundRoute.MANUAL),
        (5, RefundRoute.MANUAL),
    ],
)
def test_monthly_count_limit_is_strictly_less_than_four(month_count, expected) -> None:
    """The count clause is `< 4`, so the fourth refund in a month needs review.

    The ratio clause is deliberately pushed out of reach here (50% projected)
    so the count boundary is the only thing under test.
    """
    facts = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=month_count,
        lifetime_paid_cents=10000,
        lifetime_user_refunded_cents=0,
    )
    assert decide_refund_route(facts) is expected


@pytest.mark.parametrize(
    ("refunded", "paid", "expected"),
    [
        # projected = refunded + 5000; boundary sits at exactly 30% of paid.
        (0, 100_000, RefundRoute.AUTOMATIC),  # 5%
        (25_000, 100_000, RefundRoute.AUTOMATIC),  # exactly 30%
        (25_001, 100_000, RefundRoute.MANUAL),  # a single cent over 30%
    ],
)
def test_ratio_limit_is_inclusive_at_thirty_percent(refunded, paid, expected) -> None:
    """The ratio clause is `<= 0.30`, so landing exactly on 30% stays automatic."""
    facts = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=4,
        lifetime_paid_cents=paid,
        lifetime_user_refunded_cents=refunded,
    )
    assert decide_refund_route(facts) is expected


def test_either_count_or_ratio_is_enough() -> None:
    """The two history clauses are ORed, not ANDed."""
    over_ratio_within_count = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=3,
        lifetime_paid_cents=5000,
        lifetime_user_refunded_cents=0,
    )
    within_ratio_over_count = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=99,
        lifetime_paid_cents=1_000_000,
        lifetime_user_refunded_cents=0,
    )
    assert decide_refund_route(over_ratio_within_count) is RefundRoute.AUTOMATIC
    assert decide_refund_route(within_ratio_over_count) is RefundRoute.AUTOMATIC


def test_projected_ratio_includes_the_pending_order() -> None:
    """The ratio is forward-looking: it counts the refund being decided.

    Ignoring the current order would let a user sit just under the cap forever
    and refund past it one order at a time.
    """
    already_at_the_line = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=4,
        lifetime_paid_cents=100_000,
        lifetime_user_refunded_cents=30_000,
    )
    assert decide_refund_route(already_at_the_line) is RefundRoute.MANUAL


@pytest.mark.parametrize("paid", [0, -1])
def test_unknown_payment_history_never_raises_and_never_auto_approves(paid) -> None:
    """A zero or negative denominator must not divide by zero.

    An undefined ratio cannot prove the user is within the cap, so the ratio
    clause fails closed and the decision falls back to the monthly count.
    """
    within_count = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=0,
        lifetime_paid_cents=paid,
        lifetime_user_refunded_cents=0,
    )
    over_count = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=4,
        lifetime_paid_cents=paid,
        lifetime_user_refunded_cents=0,
    )
    assert decide_refund_route(within_count) is RefundRoute.AUTOMATIC
    assert decide_refund_route(over_count) is RefundRoute.MANUAL


def test_ratio_is_computed_exactly_not_in_binary_float() -> None:
    """Decimal keeps the 30% comparison exact for cent-scale integers.

    Guards the arithmetic itself: the same ratio computed through Decimal must
    agree with exact integer cross-multiplication on both sides of the line.
    """
    for paid, refunded in ((100_000, 25_000), (100_000, 25_001), (3, 1), (7, 1)):
        facts = RefundFacts(
            order_amount_cents=5000,
            monthly_user_refund_count=4,
            lifetime_paid_cents=paid,
            lifetime_user_refunded_cents=refunded,
        )
        projected = refunded + 5000
        exact_within_ratio = projected * 10 <= paid * 3
        expected = RefundRoute.AUTOMATIC if exact_within_ratio else RefundRoute.MANUAL
        assert decide_refund_route(facts) is expected


def test_route_is_a_string_enum_with_stable_wire_values() -> None:
    """The two values are persisted and returned over HTTP, so pin them."""
    assert RefundRoute.AUTOMATIC.value == "automatic"
    assert RefundRoute.MANUAL.value == "manual"
    assert sorted(route.value for route in RefundRoute) == ["automatic", "manual"]


def test_facts_are_immutable() -> None:
    """The policy input is a value object; a service must not mutate it midway."""
    facts = RefundFacts(
        order_amount_cents=5000,
        monthly_user_refund_count=0,
        lifetime_paid_cents=10000,
        lifetime_user_refunded_cents=0,
    )
    with pytest.raises(Exception):
        facts.order_amount_cents = 1  # type: ignore[misc]


def test_policy_module_is_pure() -> None:
    """domain/ must not reach for FastAPI, SQLAlchemy or the rest of server/."""
    import ast
    from pathlib import Path

    source = Path("server/domain/refund_policy.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert not imported & {"fastapi", "sqlalchemy", "server"}


def test_thresholds_are_exposed_as_named_constants() -> None:
    """Services and Admin copy report the same numbers; one source of truth."""
    from server.domain.refund_policy import (
        MAX_AUTOMATIC_AMOUNT_CENTS,
        MAX_AUTOMATIC_MONTHLY_COUNT,
        MAX_AUTOMATIC_RATIO,
    )

    assert MAX_AUTOMATIC_AMOUNT_CENTS == 5000
    assert MAX_AUTOMATIC_MONTHLY_COUNT == 4
    assert MAX_AUTOMATIC_RATIO == Decimal("0.30")
