from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from server.domain.service_tiers import ServiceTier


GradingStandard = Literal["league_second_round", "cmo", "imo"]

MAX_NOTE_CHARS = 2000


class QuoteView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    page_count: int
    cents_per_page: int
    amount_cents: int
    expires_at: datetime
    expires_in_seconds: int
    service_tier: ServiceTier
    service_tier_label: str
    grading_standard: GradingStandard
    note: str


class ServiceTierView(BaseModel):
    id: ServiceTier
    label: str
    description: str
    delivery_label: str
    cents_per_page: int
    enabled: bool


class ServiceTierListView(BaseModel):
    items: list[ServiceTierView]
