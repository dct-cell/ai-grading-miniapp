from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy.orm import Session

from server.adapters.pdf import PdfValidationError, inspect_pdf
from server.adapters.files import LocalFileStore
from server.models import FileObject


FILE_TTL = timedelta(hours=24)


class FileKind(StrEnum):
    SOURCE = "source"
    REFERENCE = "reference"


class FileState(StrEnum):
    TEMPORARY = "temporary"
    RETAINED = "retained"
    #: The bytes are gone from disk. The row survives as a tombstone so the
    #: scheduler can distinguish "already collected" from "never existed" and
    #: stays idempotent across runs.
    DELETED = "deleted"


@dataclass(frozen=True)
class StoredPdf:
    file_object: FileObject
    page_count: int


def store_temporary_pdf(
    *,
    session: Session,
    store: LocalFileStore,
    owner_user_id: str,
    kind: FileKind,
    stream: BinaryIO,
    max_pages: int,
    ttl: timedelta = FILE_TTL,
) -> StoredPdf:
    """Write an untrusted PDF, validate it, and register a temporary row.

    A validation failure removes the written object and registers nothing.
    """
    file_id = str(uuid4())
    stored = store.put_temporary(file_id, stream)
    try:
        info = inspect_pdf(store.resolve(stored.relative_path), max_pages=max_pages)
    except PdfValidationError:
        store.delete(stored.relative_path)
        raise

    record = FileObject(
        id=file_id,
        owner_user_id=owner_user_id,
        kind=kind,
        relative_path=stored.relative_path,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        state=FileState.TEMPORARY,
        expires_at=datetime.now(timezone.utc) + ttl,
    )
    session.add(record)
    session.flush()
    return StoredPdf(file_object=record, page_count=info.page_count)


def promote_to_retained(record: FileObject) -> None:
    """Mark a paid file as retained.

    Promotion is deliberately database-only: the bytes stay at the path chosen
    when they were written. Moving the file here would escape the payment
    transaction, so a rolled-back commit would leave the row and the disk
    permanently disagreeing. `FileObject.state` is the authoritative lifecycle.
    """
    record.state = FileState.RETAINED
