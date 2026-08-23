from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


MAX_INSTALLATION_ID_CHARS = 64
MAX_NAME_CHARS = 128
MAX_VERSION_CHARS = 64


class WorkerRegistrationRequest(BaseModel):
    """Facts the installer reports about its host.

    The server never accepts a client-supplied worker_id; extra fields are
    ignored so a forged identity cannot ride along with a registration.
    """

    installation_id: str = Field(min_length=1, max_length=MAX_INSTALLATION_ID_CHARS)
    device_name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    platform: str = Field(min_length=1, max_length=MAX_VERSION_CHARS)
    architecture: str = Field(min_length=1, max_length=32)
    worker_version: str = Field(min_length=1, max_length=MAX_VERSION_CHARS)
    codex_version: str | None = Field(default=None, max_length=MAX_VERSION_CHARS)
    tex_version: str | None = Field(default=None, max_length=MAX_VERSION_CHARS)
    capabilities: dict[str, object] = Field(default_factory=dict)


class WorkerRegistrationResponse(BaseModel):
    worker_id: str
    heartbeat_interval_seconds: int
    lease_seconds: int
    long_poll_seconds: int
    minimum_worker_version: str


class WorkerHeartbeatRequest(BaseModel):
    phase: str | None = Field(default=None, max_length=64)
    metrics: dict[str, object] = Field(default_factory=dict)


class WorkerHeartbeatResponse(BaseModel):
    worker_id: str
    status: str
    current_job_id: str | None
    lease_expires_at: datetime | None = None
    heartbeat_interval_seconds: int
    lease_seconds: int
    long_poll_seconds: int
