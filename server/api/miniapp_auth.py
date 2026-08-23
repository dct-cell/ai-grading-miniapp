from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from server.adapters.auth import FakeAuthProvider
from server.api.dependencies import CurrentUser, DatabaseSession
from server.schemas.auth import LoginRequest, LoginResponse, UserView
from server.services.sessions import LoginRejected, login


router = APIRouter(prefix="/api/v1", tags=["miniapp-auth"])
fake_router = APIRouter(prefix="/api/v1", tags=["miniapp-auth-fake"])


@fake_router.post("/auth/login", response_model=LoginResponse)
def login_with_code(
    payload: LoginRequest,
    session: DatabaseSession,
) -> LoginResponse:
    """Test-account login. Never registered in production.

    FakeAuthProvider accepts any `test-` prefixed code, so exposing this in
    production would let anyone mint a session for an arbitrary identity.
    """
    try:
        issued = login(session, FakeAuthProvider(), payload.code)
    except LoginRejected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="登录凭证无效。",
        ) from None

    remaining = issued.expires_at - datetime.now(timezone.utc)
    return LoginResponse(
        access_token=issued.raw_token,
        token_type="Bearer",
        expires_in=int(remaining.total_seconds()),
        user=UserView.model_validate(issued.user),
    )


@router.get("/me", response_model=UserView)
def me(user: CurrentUser) -> UserView:
    return UserView.model_validate(user)
