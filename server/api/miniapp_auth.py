from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from server.api.dependencies import CurrentUser, DatabaseSession
from server.schemas.auth import LoginRequest, LoginResponse, UserView
from server.services.sessions import LoginRejected, login


router = APIRouter(prefix="/api/v1", tags=["miniapp-auth"])


@router.post("/auth/login", response_model=LoginResponse)
def login_with_code(
    payload: LoginRequest,
    session: DatabaseSession,
    request: Request,
) -> LoginResponse:
    """Exchange a fake development code or a real production wx.login code."""
    try:
        issued = login(session, request.app.state.auth_provider, payload.code)
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
