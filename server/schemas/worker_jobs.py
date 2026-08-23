from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class BundleFileView(BaseModel):
    file_id: str
    kind: str
    sha256: str
    size_bytes: int
    download_token: str


class TaskBundleView(BaseModel):
    job_id: str
    order_id: str
    round_number: int
    lease_version: int
    service_tier: str
    grading_standard: str
    league_scope: str | None
    note: str
    page_count: int
    source_file: BundleFileView
    reference_file: BundleFileView | None
    ack_deadline: datetime
    lease_expires_at: datetime
    lease_seconds: int
    ack_seconds: int
