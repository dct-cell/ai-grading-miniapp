from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    public_id: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    user: UserView
