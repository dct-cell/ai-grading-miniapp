"""Cross-Worker completion estimates.

The estimator answers "when will my order be graded?" when several Workers are
draining one FIFO queue. It is a pure function so the scheduling arithmetic can
be pinned down exactly, without a database or a live Worker fleet.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.domain.eta import (
    UNCERTAINTY_MARGIN,
    EtaRange,
    estimate_finish_times,
    estimate_ranges,
)


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def test_eta_assigns_queue_to_earliest_available_worker() -> None:
    """The plan's worked example: two Workers, three jobs, 10 min per page.

    Worker A is free now, Worker B finishes its current job in 20 minutes.
    Job "a" (3 pages) starts on A and finishes at 30. Job "b" (1 page) starts on
    B at 20 and finishes at 30. Job "c" (4 pages) goes to whichever frees up
    first — a tie at 30 — and finishes at 70.
    """
    finish = estimate_finish_times(
        now=NOW,
        worker_available_minutes=[0, 20],
        queued=[("a", 3), ("b", 1), ("c", 4)],
        minutes_per_page=10,
    )

    assert finish["a"] == 30
    assert finish["b"] == 30
    assert finish["c"] == 70


def test_a_single_worker_serialises_the_whole_queue() -> None:
    finish = estimate_finish_times(
        now=NOW,
        worker_available_minutes=[0],
        queued=[("a", 1), ("b", 1), ("c", 1)],
        minutes_per_page=10,
    )

    assert finish == {"a": 10, "b": 20, "c": 30}


def test_adding_a_worker_shortens_the_tail() -> None:
    """ETA must recalculate when the fleet size changes."""
    queued = [("a", 2), ("b", 2), ("c", 2), ("d", 2)]
    one = estimate_finish_times(
        now=NOW, worker_available_minutes=[0], queued=queued, minutes_per_page=10
    )
    two = estimate_finish_times(
        now=NOW, worker_available_minutes=[0, 0], queued=queued, minutes_per_page=10
    )

    assert one["d"] == 80
    assert two["d"] == 40
    assert two["d"] < one["d"]


def test_no_ready_worker_yields_no_estimate() -> None:
    """With the whole fleet offline there is no honest number to show.

    Returning zero or "any moment now" would be worse than showing nothing.
    """
    finish = estimate_finish_times(
        now=NOW,
        worker_available_minutes=[],
        queued=[("a", 3)],
        minutes_per_page=10,
    )

    assert finish == {}


def test_busy_workers_are_scheduled_after_their_current_job() -> None:
    """A Worker's remaining work delays everything it picks up next."""
    finish = estimate_finish_times(
        now=NOW,
        worker_available_minutes=[45],
        queued=[("a", 1)],
        minutes_per_page=10,
    )

    assert finish["a"] == 55


def test_queue_order_is_fifo() -> None:
    """Earlier jobs must never be scheduled behind later ones."""
    finish = estimate_finish_times(
        now=NOW,
        worker_available_minutes=[0, 0],
        queued=[("first", 5), ("second", 1)],
        minutes_per_page=10,
    )

    # Both start immediately on separate Workers, so the short one lands first;
    # what matters is that "first" was assigned to the earliest slot.
    assert finish["first"] == 50
    assert finish["second"] == 10


def test_zero_page_job_still_takes_a_slot() -> None:
    finish = estimate_finish_times(
        now=NOW,
        worker_available_minutes=[0],
        queued=[("a", 0), ("b", 1)],
        minutes_per_page=10,
    )

    assert finish["a"] == 0
    assert finish["b"] == 10


def test_estimate_ranges_add_an_uncertainty_margin() -> None:
    """A point estimate would imply precision the model does not have."""
    ranges = estimate_ranges(
        now=NOW,
        worker_available_minutes=[0],
        queued=[("a", 3)],
        minutes_per_page=10,
    )

    assert set(ranges) == {"a"}
    window = ranges["a"]
    assert isinstance(window, EtaRange)
    assert window.earliest_minutes == 30 - int(30 * UNCERTAINTY_MARGIN)
    assert window.latest_minutes == 30 + int(30 * UNCERTAINTY_MARGIN)
    assert window.earliest_minutes <= window.latest_minutes


def test_ranges_are_absolute_timestamps_too() -> None:
    """The mini-program shows a server range, not a local countdown."""
    ranges = estimate_ranges(
        now=NOW,
        worker_available_minutes=[0],
        queued=[("a", 1)],
        minutes_per_page=10,
    )

    window = ranges["a"]
    assert window.earliest_at >= NOW
    assert window.latest_at >= window.earliest_at
    assert (window.latest_at - NOW).total_seconds() / 60 == window.latest_minutes


def test_margin_is_twenty_percent() -> None:
    assert UNCERTAINTY_MARGIN == 0.2


def test_estimator_module_is_pure() -> None:
    """domain/ must not reach for FastAPI, SQLAlchemy or the rest of server/."""
    import ast
    from pathlib import Path

    source = Path("server/domain/eta.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert not imported & {"fastapi", "sqlalchemy", "server"}


@pytest.mark.parametrize("minutes_per_page", [1, 7, 10, 25])
def test_total_work_is_conserved_across_workers(minutes_per_page: int) -> None:
    """Whatever the fleet, the last finish is at least the critical path.

    Guards against an off-by-one in the heap that would quietly promise a
    faster turnaround than a single job actually takes.
    """
    queued = [("a", 3), ("b", 4), ("c", 5)]
    finish = estimate_finish_times(
        now=NOW,
        worker_available_minutes=[0, 0, 0, 0],
        queued=queued,
        minutes_per_page=minutes_per_page,
    )

    longest_single_job = max(pages for _, pages in queued) * minutes_per_page
    assert max(finish.values()) >= longest_single_job
