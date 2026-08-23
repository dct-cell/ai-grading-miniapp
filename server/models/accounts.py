from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, UTCDateTime


def _uuid_string() -> str:
    return str(uuid4())


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    openid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)


class MiniappSession(TimestampMixin, Base):
    __tablename__ = "miniapp_sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
