from __future__ import annotations

from pydantic import BaseModel, Field


class PrepayRequestBody(BaseModel):
    quote_id: str = Field(min_length=1, max_length=64)


class PrepayView(BaseModel):
    payment_id: str
    prepay_id: str
    amount_cents: int
    client_payload: dict[str, str]


class FakeCallbackBody(BaseModel):
    fake_transaction_id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
