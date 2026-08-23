"""Admin authentication: the third credential domain, now cookie-based.

Phase 05 authenticated admins with a static shared key plus an ``X-Admin-ID``
header. Phase 07 replaces that path entirely — there is no longer any code that
reads ``settings.admin_shared_key``, so a leaked key authenticates nothing.

Two properties carried over deliberately from Phase 05:

* This is its own domain. A mini-program session token or a Worker shared key
  presented here authenticates nothing, and an Admin cookie is meaningless on
  ``/api/v1/*`` and ``/worker/v1/*``. Tests assert both directions.
* Every decision is attributable to a real row in ``admin_users``, so
  ``AuditLog.actor_id`` names a person. Audit rows written under Phase 05 stay
  meaningful because the actor id has not changed meaning.

Like the Worker control plane, these routes are registered in every
environment: refund approvals have to work in production. They are real
endpoints, not fake adapters, so ``FAKE_ADAPTER_ENVIRONMENTS`` does not gate
them.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from server.api.dependencies import get_session, get_settings
from server.config import ServerSettings
from server.models import AdminUser
from server.services.admin_sessions import (
    ResolvedAdmin,
    csrf_token_matches,
    resolve,
)


SESSION_COOKIE_NAME = "grader_admin_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
#: Methods that change state and therefore need CSRF protection. A safe method
#: must never be added here, and a state-changing one must never be omitted.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_ADMIN_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Admin 认证失败。",
)
_ADMIN_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="该请求未通过来源校验。",
)


def resolve_admin_session(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> ResolvedAdmin:
    """Authenticate the cookie, then enforce CSRF on state-changing methods."""
    resolved = resolve(session, request.cookies.get(SESSION_COOKIE_NAME))
    if resolved is None:
        raise _ADMIN_UNAUTHORIZED

    if request.method.upper() in _UNSAFE_METHODS:
        _require_csrf(request, resolved)
    return resolved


def _require_csrf(request: Request, resolved: ResolvedAdmin) -> None:
    """Require a matching Origin *and* a matching CSRF token.

    Both checks are needed. Origin alone would trust a browser that omits the
    header; the token alone would not stop a same-origin subdomain. The token is
    compared in constant time against its stored hash, never with ``==``.
    """
    settings: ServerSettings = request.app.state.settings
    origin = request.headers.get("Origin")
    # Required, not merely checked-when-present: a browser always sends Origin
    # on a state-changing request, so a missing one means the caller is not the
    # Admin SPA. Compared literally, so no lookalike origin can match.
    if origin != settings.admin_origin:
        raise _ADMIN_FORBIDDEN

    provided = request.headers.get(CSRF_HEADER_NAME)
    if not provided:
        raise _ADMIN_FORBIDDEN
    if not csrf_token_matches(
        provided,
        resolved.session_record.token_hash,
        session_secret=settings.session_secret,
    ):
        raise _ADMIN_FORBIDDEN


def current_admin(
    resolved: Annotated[ResolvedAdmin, Depends(resolve_admin_session)],
) -> AdminUser:
    return resolved.admin


CurrentAdmin = Annotated[AdminUser, Depends(current_admin)]
CurrentAdminSession = Annotated[ResolvedAdmin, Depends(resolve_admin_session)]
Settings = Annotated[ServerSettings, Depends(get_settings)]
