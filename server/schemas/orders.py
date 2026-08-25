from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from server.domain.progress import ProgressStage
from server.domain.service_tiers import ServiceTier
from server.schemas.quotes import GradingStandard


class OrderRoundView(BaseModel):
    round_number: int
    service_tier: ServiceTier
    state: str
    progress_stage: ProgressStage | None = None
    delivered_at: datetime | None


class OrderSummaryView(BaseModel):
    id: str
    state: str
    category: str
    service_tier: ServiceTier
    service_tier_label: str
    grading_standard: GradingStandard
    page_count: int
    paid_amount_cents: int
    current_round_number: int
    progress_stage: ProgressStage | None = None
    created_at: datetime


class OrderProgressView(BaseModel):
    id: str
    state: str
    current_round_number: int
    progress_stage: ProgressStage | None = None


class OrderProgressPageView(BaseModel):
    items: list[OrderProgressView]


class OrderEtaView(BaseModel):
    """A completion window. The client displays this, never a local countdown.

    None on the order means there is nothing honest to show: no pending work, or
    no Worker ready to pick it up.
    """

    earliest_minutes: int
    latest_minutes: int
    earliest_at: datetime
    latest_at: datetime


class OrderDetailView(OrderSummaryView):
    note: str
    rounds: list[OrderRoundView]
    #: Server-authoritative list of what the owner may still do. The
    #: mini-program renders its buttons from this and never computes refund
    #: eligibility itself; every action re-checks the same conditions inside
    #: its own transaction, so this list is advisory, not an authorisation.
    available_actions: list[str]
    appeal_text: str | None = None
    acceptance_deadline: datetime | None = None
    eta: OrderEtaView | None = None


class OrderPageView(BaseModel):
    items: list[OrderSummaryView]
    next_cursor: str | None
