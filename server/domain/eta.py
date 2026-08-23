"""Completion estimates across a fleet of Workers.

Several Workers drain one FIFO queue, each holding exactly one order at a time.
The estimate is a min-heap simulation of that: repeatedly hand the next queued
job to whichever Worker frees up first.

Kept pure — no database, no FastAPI — so the arithmetic is testable on its own
and the caller decides what counts as a "ready" Worker or a "queued" job.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Iterable, Sequence


#: Fraction added either side of the point estimate. Page counts, model latency
#: and Worker speed all vary, so a bare number would imply precision we do not
#: have; the mini-program shows the range instead.
UNCERTAINTY_MARGIN: Final[float] = 0.2


@dataclass(frozen=True)
class EtaRange:
    """A completion window, in minutes from now and as absolute instants."""

    earliest_minutes: int
    latest_minutes: int
    earliest_at: datetime
    latest_at: datetime


def estimate_finish_times(
    *,
    now: datetime,
    worker_available_minutes: Sequence[int],
    queued: Iterable[tuple[str, int]],
    minutes_per_page: int,
) -> dict[str, int]:
    """Minutes from ``now`` until each queued job is expected to finish.

    ``worker_available_minutes`` holds one entry per *ready* Worker: how long
    until it can start something new (0 if idle, otherwise the remaining time on
    its current job). Callers exclude offline and disabled Workers, so an empty
    sequence means no capacity at all.

    ``queued`` is the FIFO queue as ``(job_id, page_count)`` pairs.

    Returns an empty mapping when there is no ready Worker: with no capacity
    there is no honest estimate, and reporting zero would be worse than
    reporting nothing.
    """
    if not worker_available_minutes:
        return {}

    # Each heap entry is a Worker's next free moment. Popping the smallest is
    # what makes the simulation match the real "first free Worker takes the
    # head of the queue" behaviour.
    free_at = list(worker_available_minutes)
    heapq.heapify(free_at)

    finish_times: dict[str, int] = {}
    for job_id, page_count in queued:
        starts_at = heapq.heappop(free_at)
        finishes_at = starts_at + page_count * minutes_per_page
        finish_times[job_id] = finishes_at
        heapq.heappush(free_at, finishes_at)
    return finish_times


def estimate_ranges(
    *,
    now: datetime,
    worker_available_minutes: Sequence[int],
    queued: Iterable[tuple[str, int]],
    minutes_per_page: int,
    margin: float = UNCERTAINTY_MARGIN,
) -> dict[str, EtaRange]:
    """Turn point estimates into windows with an uncertainty margin."""
    finish_times = estimate_finish_times(
        now=now,
        worker_available_minutes=worker_available_minutes,
        queued=queued,
        minutes_per_page=minutes_per_page,
    )
    ranges: dict[str, EtaRange] = {}
    for job_id, minutes in finish_times.items():
        slack = int(minutes * margin)
        earliest = max(0, minutes - slack)
        latest = minutes + slack
        ranges[job_id] = EtaRange(
            earliest_minutes=earliest,
            latest_minutes=latest,
            earliest_at=now + timedelta(minutes=earliest),
            latest_at=now + timedelta(minutes=latest),
        )
    return ranges
