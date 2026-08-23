from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from server.config import ServerSettings
from server.models import User
from server.services.sessions import resolve_user


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="未登录或登录已过期。",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_settings(request: Request) -> ServerSettings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def current_miniapp_user(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> User:
    token = _bearer_token(request)
    if token is None:
        raise _UNAUTHORIZED
    user = resolve_user(session, token)
    if user is None:
        raise _UNAUTHORIZED
    return user


CurrentUser = Annotated[User, Depends(current_miniapp_user)]
DatabaseSession = Annotated[Session, Depends(get_session)]
Settings = Annotated[ServerSettings, Depends(get_settings)]
