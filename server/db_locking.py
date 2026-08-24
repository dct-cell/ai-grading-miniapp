from __future__ import annotations

from typing import TypeVar

from sqlalchemy.orm import Session


T = TypeVar("T")


def lock_row(session: Session, model: type[T], primary_key: object) -> T | None:
    """Load one ORM row with FOR UPDATE where the backend supports it."""
    if session.get_bind().dialect.name == "sqlite":
        return session.get(model, primary_key)
    return session.get(model, primary_key, with_for_update=True)
