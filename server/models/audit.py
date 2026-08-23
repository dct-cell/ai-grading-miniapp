from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, UTCDateTime


def _uuid_string() -> str:
    return str(uuid4())


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    #: An Argon2id hash. 255 characters is comfortably more than the ~97 an
    #: Argon2id encoding takes at the library defaults.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class AdminSession(TimestampMixin, Base):
    """An opaque server-side Admin session, mirroring ``MiniappSession``.

    The raw token is shown to the browser exactly once, inside an HttpOnly
    cookie; only its SHA-256 is stored, so a database reader cannot mint a
    session.

    There is deliberately no CSRF column. The CSRF token is *derived* from this
    row's ``token_hash`` (see ``services.admin_sessions.derive_csrf_token``),
    which keeps it stable across page reloads and multiple tabs. Storing a
    single CSRF value per session would instead force every
    ``GET /auth/session`` to rotate it, and a second tab's next mutation would
    then fail with a stale token.
    """

    __tablename__ = "admin_sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    admin_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("admin_users.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class OperationalSetting(TimestampMixin, Base):
    """One editable operational value, keyed by name.

    A key/value table rather than a one-row table with a column per setting:
    adding a knob then needs no migration, and an unset key falls back to the
    environment default, so a fresh deployment behaves exactly as it did before
    Phase 07.

    Values are stored as text and coerced by the service, because the set spans
    integers today and may include strings (a home banner) tomorrow. Only names
    on an explicit allow-list are accepted, so this can never become a way to
    write ``session_secret`` into the database.
    """

    __tablename__ = "operational_settings"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(512), nullable=False)


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
