"""Admin login, session introspection and logout.

The raw session token never appears in a response body — only in a ``Set-Cookie``
header the browser cannot read from script. The SPA therefore has nothing to
store, which is why no Admin token is ever placed in ``localStorage``.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from server.api.admin_dependencies import (
    SESSION_COOKIE_NAME,
    CurrentAdminSession,
    Settings,
)
from server.api.dependencies import DatabaseSession
from server.config import Environment
from server.services.admin_sessions import (
    AccountDisabled,
    LoginFailed,
    LoginRateLimiter,
    RateLimited,
    derive_csrf_token,
    login,
    logout,
)


router = APIRouter(prefix="/admin/api/v1/auth", tags=["admin-auth"])

#: One identical failure for every cause, so the response cannot be used to
#: enumerate usernames.
_LOGIN_FAILED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="用户名或密码不正确。",
)
_ACCOUNT_DISABLED = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="该管理员账号已停用。",
)
_RATE_LIMITED = HTTPException(
    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    detail="登录尝试过于频繁，请稍后再试。",
)

def _limiter(request: Request) -> LoginRateLimiter:
    """Return this application's login rate limiter.

    Held on ``app.state`` rather than at module scope so that each application
    instance owns its own counters. A module-level limiter would be shared by
    every app in the process, which would let one test's failed logins throttle
    an unrelated one — and, more importantly, would outlive a reconfigured app
    in any future multi-tenant or embedded use.
    """
    limiter = getattr(request.app.state, "admin_login_limiter", None)
    if limiter is None:
        limiter = LoginRateLimiter()
        request.app.state.admin_login_limiter = limiter
    return limiter


class LoginBody(BaseModel):
    model_config = {"extra": "forbid"}

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class SessionView(BaseModel):
    admin_id: str
    username: str
    csrf_token: str


def _client_address(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


#: Environments served over plain http, where a Secure cookie would be refused
#: by the browser and by the test client. Everything else gets Secure.
_INSECURE_TRANSPORT_ENVIRONMENTS = frozenset(
    {Environment.DEVELOPMENT, Environment.TEST}
)


def _set_session_cookie(
    response: Response,
    *,
    raw_token: str,
    settings,
) -> None:
    """Attach the session cookie with every protective attribute.

    ``Secure`` is omitted for development and tests because a browser refuses a
    Secure cookie over plain http, which would make local development
    impossible; staging and production always get it. ``Path=/admin`` keeps the
    cookie off the mini-program and Worker routes, and ``SameSite=Strict`` stops
    it riding along on any cross-site navigation.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        httponly=True,
        samesite="strict",
        path="/admin",
        secure=settings.environment not in _INSECURE_TRANSPORT_ENVIRONMENTS,
    )


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
def admin_login(
    payload: LoginBody,
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: Settings,
) -> Response:
    try:
        issued = login(
            session,
            username=payload.username,
            password=payload.password,
            address=_client_address(request),
            limiter=_limiter(request),
        )
    except RateLimited:
        raise _RATE_LIMITED from None
    except AccountDisabled:
        raise _ACCOUNT_DISABLED from None
    except LoginFailed:
        raise _LOGIN_FAILED from None

    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookie(result, raw_token=issued.raw_token, settings=settings)
    return result


@router.get("/session", response_model=SessionView)
def read_session(resolved: CurrentAdminSession, settings: Settings) -> SessionView:
    """Return who is signed in, plus the CSRF token for later mutations.

    The CSRF token is returned in the body on purpose: a cross-site attacker can
    make the browser *send* the cookie but cannot read this response, so echoing
    the value back in a header proves the request came from our own page.

    It is derived from the session, so a page reload or a second tab receives
    the same value instead of invalidating the other's in-flight mutations.
    """
    return SessionView(
        admin_id=resolved.admin.id,
        username=resolved.admin.username,
        csrf_token=derive_csrf_token(
            resolved.session_record.token_hash,
            session_secret=settings.session_secret,
        ),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def admin_logout(
    resolved: CurrentAdminSession,
    session: DatabaseSession,
) -> Response:
    logout(session, resolved.session_record)
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    result.delete_cookie(SESSION_COOKIE_NAME, path="/admin")
    return result
