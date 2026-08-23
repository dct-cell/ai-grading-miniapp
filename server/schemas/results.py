from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UploadGrantView(BaseModel):
    upload_token: str
    max_bytes: int


class BeginUploadsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lease_version: int = Field(ge=0)


class StagedResultView(BaseModel):
    file_id: str
    kind: str
    sha256: str
    size_bytes: int


class CommitResultRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lease_version: int = Field(ge=0)
    result_json_file_id: str = Field(min_length=1, max_length=36)
    result_pdf_file_id: str = Field(min_length=1, max_length=36)


class CommitResultView(BaseModel):
    status: str
    order_id: str
    round_number: int
