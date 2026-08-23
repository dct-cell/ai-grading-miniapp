from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.adapters.auth import AuthProvider
from server.models import MiniappSession, User


SESSION_TTL = timedelta(days=30)
PUBLIC_ID_PREFIX = "u-"
_PUBLIC_ID_RANDOM_BYTES = 4
_PUBLIC_ID_ATTEMPTS = 8


class LoginRejected(ValueError):
    """The external provider refused to exchange the supplied login code."""


@dataclass(frozen=True)
class IssuedSession:
    user: User
    raw_token: str
    expires_at: datetime


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _generate_public_id(session: Session) -> str:
    for _ in range(_PUBLIC_ID_ATTEMPTS):
        candidate = PUBLIC_ID_PREFIX + secrets.token_hex(_PUBLIC_ID_RANDOM_BYTES)
        taken = session.scalar(select(User.id).where(User.public_id == candidate))
        if taken is None:
            return candidate
    raise RuntimeError("could not allocate a unique public id")


def _get_or_create_user(session: Session, openid: str) -> User:
    """Resolve the account for an external identity, tolerating races.

    Two simultaneous first logins both see no row and both insert. The loser
    hits the users.openid unique constraint; it must recover by reading the
    winner's row instead of surfacing a 500.
    """
    user = session.scalar(select(User).where(User.openid == openid))
    if user is not None:
        return user

    savepoint = session.begin_nested()
    try:
        user = User(openid=openid, public_id=_generate_public_id(session))
        session.add(user)
        savepoint.commit()
        return user
    except IntegrityError:
        savepoint.rollback()

    existing = session.scalar(select(User).where(User.openid == openid))
    if existing is None:
        raise
    return existing


def login(session: Session, provider: AuthProvider, code: str) -> IssuedSession:
    try:
        identity = provider.exchange_code(code)
    except ValueError as error:
        raise LoginRejected(str(error)) from None

    user = _get_or_create_user(session, identity.openid)

    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    session.add(
        MiniappSession(
            user_id=user.id,
            token_hash=hash_session_token(raw_token),
            expires_at=expires_at,
        )
    )
    session.commit()
    return IssuedSession(user=user, raw_token=raw_token, expires_at=expires_at)


def resolve_user(session: Session, raw_token: str) -> User | None:
    if not raw_token:
        return None
    record = session.scalar(
        select(MiniappSession).where(
            MiniappSession.token_hash == hash_session_token(raw_token)
        )
    )
    if record is None or record.revoked_at is not None:
        return None
    if record.expires_at <= datetime.now(timezone.utc):
        return None
    return session.get(User, record.user_id)
