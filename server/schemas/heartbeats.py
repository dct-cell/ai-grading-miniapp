from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AckRequest(BaseModel):
    """The Worker proves it holds the lease with its fencing token."""

    model_config = ConfigDict(extra="ignore")

    lease_version: int = Field(ge=0)


class RenewRequest(BaseModel):
    """Renewal payload. Any client-supplied expiry is deliberately ignored."""

    model_config = ConfigDict(extra="ignore")

    lease_version: int = Field(ge=0)
    phase: str | None = Field(default=None, max_length=64)


class JobFailureRequest(BaseModel):
    """A terminal Worker failure bound to the current fencing token."""

    model_config = ConfigDict(extra="forbid")

    lease_version: int = Field(ge=0)
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(default="", max_length=500)


class JobFailureView(BaseModel):
    job_id: str
    state: str
    lease_version: int


class LeaseStateView(BaseModel):
    job_id: str
    state: str
    lease_version: int
    lease_expires_at: datetime
    lease_seconds: int


class HeartbeatRenewalRequest(BaseModel):
    """Heartbeat that may piggyback a lease renewal to halve request volume."""

    model_config = ConfigDict(extra="ignore")

    phase: str | None = Field(default=None, max_length=64)
    metrics: dict[str, object] = Field(default_factory=dict)
    job_id: str | None = None
    lease_version: int | None = Field(default=None, ge=0)
