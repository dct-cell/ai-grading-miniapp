from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from server.services.aftersales import MAX_APPEAL_TEXT_LENGTH, RefundReason


class ReviewRequestBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_APPEAL_TEXT_LENGTH)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, text: str) -> str:
        """A review must say what to re-examine; whitespace is not a reason."""
        stripped = text.strip()
        if not stripped:
            raise ValueError("请填写复核理由。")
        return stripped


class RefundRequestBody(BaseModel):
    """The reason is recorded for support; the amount is never client-supplied."""

    reason: RefundReason
    details: str | None = Field(default=None, max_length=MAX_APPEAL_TEXT_LENGTH)


class OrderActionView(BaseModel):
    order_id: str
    state: str
    amount_cents: int | None = None
    refund_id: str | None = None
    appeal_id: str | None = None
