"""Admin authentication: Argon2id passwords, opaque server-side sessions.

This module replaces the Phase 05 shared-key seam outright. Nothing here
consults ``settings.admin_shared_key``, so a leaked key now authenticates
nothing at all.

Three deliberate choices worth keeping:

*Opaque sessions, not signed tokens.* A signed token is a cached authorisation
decision, and "this admin is still enabled" is precisely the decision we cannot
afford to cache: disabling an account has to take effect on the next request.
Every request therefore re-reads the session row and the admin row.

*The session token is stored hashed; the CSRF token is derived.* Only SHA-256 of
the session token is stored, so a database reader cannot mint a session. SHA-256
is right here and Argon2id would be wrong: this is a 32-byte random secret, not
a low-entropy password, so there is no guessing attack for a slow KDF to
frustrate. The CSRF token is an HMAC over the session's stored hash, keyed by
``session_secret``. Deriving rather than storing keeps it stable across page
reloads and second tabs, and still leaves it unguessable without the key.

*Failures are uniform.* A wrong password and an unknown username produce the
same status and the same body, and both pay the Argon2id verification cost, so
neither the response nor its timing reveals whether an account exists.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher, Type
from argon2.exceptions import Argon2Error, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import AdminSession, AdminUser


SESSION_TTL = timedelta(hours=12)
#: Five failures per username+IP inside this window earn a 429.
RATE_LIMIT_WINDOW = timedelta(minutes=15)
MAX_FAILED_LOGINS = 5
_TOKEN_BYTES = 32

_HASHER = PasswordHasher(type=Type.ID)

#: A well-formed Argon2id hash of a value no password can equal, used to pay the
#: verification cost when the username does not exist. Without this, an unknown
#: username would return noticeably faster than a wrong password and leak which
#: accounts are real.
_DUMMY_HASH = _HASHER.hash("no such admin")


class LoginFailed(Exception):
    """Authentication failed. Deliberately carries no reason.

    The caller maps every instance to one identical 401 so that neither the
    status nor the body distinguishes an unknown username from a bad password.
    """


class AccountDisabled(Exception):
    """The credentials were correct but the account is disabled."""


class RateLimited(Exception):
    """Too many recent failures for this username and address."""


@dataclass(frozen=True)
class IssuedAdminSession:
    admin: AdminUser
    raw_token: str
    expires_at: datetime


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def tokens_match(provided: str, expected_hash: str) -> bool:
    """Compare a presented token against a stored hash in constant time.

    Hashing first means ``compare_digest`` always sees two equal-length digests,
    so neither the token's value nor its length leaks through timing.
    """
    return hmac.compare_digest(hash_token(provided), expected_hash)


def derive_csrf_token(session_token_hash: str, *, session_secret: str) -> str:
    """Derive this session's CSRF token from its stored hash.

    Derived rather than stored so that it stays stable for the life of the
    session: a page reload or a second tab asks for it again and must receive
    the same value, otherwise one tab's mutation would invalidate the other's.

    It is keyed by ``session_secret``, so knowing the hash from a database dump
    is not enough to forge it. It is *not* equal to the session token, so
    leaking it does not leak the credential.
    """
    return hmac.new(
        session_secret.encode("utf-8"),
        b"admin-csrf:" + session_token_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def csrf_token_matches(
    provided: str,
    session_token_hash: str,
    *,
    session_secret: str,
) -> bool:
    expected = derive_csrf_token(session_token_hash, session_secret=session_secret)
    # Hash both sides before comparing, exactly as `tokens_match` does.
    # `compare_digest` raises TypeError on a non-ASCII `str`, and this value comes
    # straight from an attacker-controlled header, so comparing the raw strings
    # would turn a 403 refusal into an unhandled 500.
    return hmac.compare_digest(hash_token(provided), hash_token(expected))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LoginRateLimiter:
    """Counts recent failures per (username, address).

    Deliberately in-process: the service runs as a single application process,
    and a durable store would be a bigger change than the threat justifies. It
    is keyed on the pair, not the address alone, so one admin failing to log in
    cannot lock a colleague out from the same office.
    """

    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], list[datetime]] = {}

    def _recent(self, key: tuple[str, str], now: datetime) -> list[datetime]:
        cutoff = now - RATE_LIMIT_WINDOW
        recent = [stamp for stamp in self._failures.get(key, ()) if stamp > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def check(self, username: str, address: str, *, now: datetime | None = None) -> None:
        now = now or _now()
        if len(self._recent((username, address), now)) >= MAX_FAILED_LOGINS:
            raise RateLimited

    def record_failure(
        self, username: str, address: str, *, now: datetime | None = None
    ) -> None:
        now = now or _now()
        key = (username, address)
        self._failures.setdefault(key, []).append(now)
        self._recent(key, now)

    def reset(self, username: str, address: str) -> None:
        """Clear the counter for this pair only.

        Deliberately *not* every address for this username: an attacker whose
        five attempts are spent would otherwise be handed a fresh five each time
        the real admin signs in from the office, and a real admin signs in
        several times a day. That would multiply the online guessing rate rather
        than cap it.
        """
        self._failures.pop((username, address), None)


def login(
    session: Session,
    *,
    username: str,
    password: str,
    address: str,
    limiter: LoginRateLimiter,
) -> IssuedAdminSession:
    """Verify a password and issue a fresh session.

    The rate limit is checked before the password so that a throttled caller is
    refused even when it finally guesses correctly; otherwise the limit would
    not actually slow an online guessing attack down.
    """
    limiter.check(username, address)

    admin = session.scalar(select(AdminUser).where(AdminUser.username == username))
    stored_hash = admin.password_hash if admin is not None else _DUMMY_HASH

    try:
        _HASHER.verify(stored_hash, password)
        verified = admin is not None
    except (VerifyMismatchError, Argon2Error):
        verified = False

    if not verified:
        limiter.record_failure(username, address)
        raise LoginFailed

    assert admin is not None  # narrowed by `verified`
    if admin.disabled_at is not None:
        # Not a rate-limited event: the password was right, so counting it would
        # let a disabled admin's own attempts throttle nobody but themselves.
        raise AccountDisabled

    limiter.reset(username, address)
    _revoke_live_sessions(session, admin.id)

    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = _now() + SESSION_TTL
    session.add(
        AdminSession(
            admin_id=admin.id,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
            last_seen_at=_now(),
        )
    )
    session.commit()
    return IssuedAdminSession(
        admin=admin,
        raw_token=raw_token,
        expires_at=expires_at,
    )


def _revoke_live_sessions(session: Session, admin_id: str) -> None:
    """Rotate: logging in again invalidates the previous session token."""
    now = _now()
    records = session.scalars(
        select(AdminSession).where(
            AdminSession.admin_id == admin_id,
            AdminSession.revoked_at.is_(None),
        )
    ).all()
    for record in records:
        record.revoked_at = now


@dataclass(frozen=True)
class ResolvedAdmin:
    admin: AdminUser
    session_record: AdminSession


def resolve(session: Session, raw_token: str | None) -> ResolvedAdmin | None:
    """Resolve a cookie value to a live admin, or ``None``.

    Returns ``None`` for every failure mode — unknown, revoked, expired, or
    belonging to a disabled admin — because the caller must not be able to tell
    them apart.
    """
    if not raw_token:
        return None
    record = session.scalar(
        select(AdminSession).where(AdminSession.token_hash == hash_token(raw_token))
    )
    if record is None or record.revoked_at is not None:
        return None
    if record.expires_at <= _now():
        return None
    admin = session.get(AdminUser, record.admin_id)
    if admin is None or admin.disabled_at is not None:
        # Disabling an account takes effect on the very next request, which is
        # the whole reason this is an opaque session rather than a signed token.
        return None

    record.last_seen_at = _now()
    session.commit()
    return ResolvedAdmin(admin=admin, session_record=record)


def logout(session: Session, record: AdminSession) -> None:
    record.revoked_at = _now()
    session.commit()
