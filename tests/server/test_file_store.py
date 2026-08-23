from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from server.adapters.files import FileStorageError, LocalFileStore
from server.models import FileObject, User
from server.models.base import Base
from server.services.files import (
    FILE_TTL,
    FileKind,
    FileState,
    StoredPdf,
    store_temporary_pdf,
)


def make_pdf_bytes(pages: int = 1) -> bytes:
    buffer = BytesIO()
    document = canvas.Canvas(buffer, pagesize=(595, 842))
    for page_number in range(1, pages + 1):
        document.drawString(72, 780, f"Page {page_number}")
        document.showPage()
    document.save()
    return buffer.getvalue()


def make_encrypted_pdf_bytes() -> bytes:
    source = PdfReader(BytesIO(make_pdf_bytes()))
    writer = PdfWriter()
    for page in source.pages:
        writer.add_page(page)
    writer.encrypt("secret")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def sample_pdf() -> BytesIO:
    return BytesIO(make_pdf_bytes())


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def user(session_factory: sessionmaker[Session]) -> User:
    with session_factory() as session:
        record = User(openid="fake:test-owner", public_id="u-00000001")
        session.add(record)
        session.commit()
        return record


def test_put_temporary_pdf_is_atomic_and_hashed(
    tmp_path: Path,
    sample_pdf: BytesIO,
) -> None:
    store = LocalFileStore(tmp_path)
    payload = sample_pdf.getvalue()

    stored = store.put_temporary("file-1", sample_pdf)

    assert stored.relative_path == "temporary/file-1.pdf"
    assert stored.size_bytes == len(payload)
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert len(stored.sha256) == 64
    assert not list((tmp_path / "staging").glob("*"))
    assert (tmp_path / stored.relative_path).read_bytes() == payload


def test_put_temporary_reports_real_byte_count_not_client_claims(
    tmp_path: Path,
) -> None:
    store = LocalFileStore(tmp_path)
    payload = make_pdf_bytes(3)

    stored = store.put_temporary("file-real-size", BytesIO(payload))

    assert stored.size_bytes == len(payload)
    assert stored.size_bytes == (tmp_path / stored.relative_path).stat().st_size


def test_put_temporary_rejects_payloads_over_the_size_limit(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path, max_bytes=1024)

    with pytest.raises(FileStorageError, match="超过"):
        store.put_temporary("file-too-big", BytesIO(b"%PDF-" + b"0" * 2048))

    assert not list((tmp_path / "staging").glob("*"))
    assert not list((tmp_path / "temporary").glob("*"))


@pytest.mark.parametrize(
    "file_id",
    [
        "../escape",
        "nested/escape",
        "nested\\escape",
        "/absolute",
        "",
        ".",
        "..",
        "file\x00id",
    ],
)
def test_put_temporary_rejects_path_traversal_identifiers(
    tmp_path: Path,
    file_id: str,
) -> None:
    store = LocalFileStore(tmp_path)

    with pytest.raises(FileStorageError):
        store.put_temporary(file_id, BytesIO(make_pdf_bytes()))

    assert list(tmp_path.rglob("*.pdf")) == []
    assert list(tmp_path.rglob("*.part")) == []


def test_resolve_never_escapes_the_storage_root(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)

    with pytest.raises(FileStorageError):
        store.resolve("../../etc/passwd")
    with pytest.raises(FileStorageError):
        store.resolve("/etc/passwd")


def test_failed_write_leaves_no_staging_or_target_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalFileStore(tmp_path)
    original_replace = os.replace

    def failing_replace(source, target) -> None:
        del source, target
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(FileStorageError):
        store.put_temporary("file-broken", BytesIO(make_pdf_bytes()))

    monkeypatch.setattr(os, "replace", original_replace)
    assert list(tmp_path.rglob("*.part")) == []
    assert list(tmp_path.rglob("*.pdf")) == []


def test_delete_removes_a_stored_object(tmp_path: Path, sample_pdf: BytesIO) -> None:
    store = LocalFileStore(tmp_path)
    stored = store.put_temporary("file-1", sample_pdf)

    store.delete(stored.relative_path)

    assert not (tmp_path / stored.relative_path).exists()
    store.delete(stored.relative_path)


def test_store_temporary_pdf_records_pages_and_expiry(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    user: User,
) -> None:
    store = LocalFileStore(tmp_path)
    payload = make_pdf_bytes(2)

    with session_factory() as session:
        stored = store_temporary_pdf(
            session=session,
            store=store,
            owner_user_id=user.id,
            kind=FileKind.SOURCE,
            stream=BytesIO(payload),
            max_pages=30,
        )
        session.commit()
        record = session.get(FileObject, stored.file_object.id)

    assert isinstance(stored, StoredPdf)
    assert stored.page_count == 2
    assert record is not None
    assert record.kind == FileKind.SOURCE
    assert record.state == FileState.TEMPORARY
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert record.size_bytes == len(payload)
    assert record.relative_path == f"temporary/{record.id}.pdf"
    assert FILE_TTL == timedelta(hours=24)
    expected = datetime.now(timezone.utc) + FILE_TTL
    assert abs((record.expires_at - expected).total_seconds()) < 60
    assert (tmp_path / record.relative_path).read_bytes() == payload


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not a pdf at all",
        b"%PDF-1.7truncated",
    ],
)
def test_invalid_pdf_leaves_no_file_and_no_row(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    user: User,
    payload: bytes,
) -> None:
    store = LocalFileStore(tmp_path)

    with session_factory() as session:
        with pytest.raises(Exception):
            store_temporary_pdf(
                session=session,
                store=store,
                owner_user_id=user.id,
                kind=FileKind.SOURCE,
                stream=BytesIO(payload),
                max_pages=30,
            )
        session.rollback()

    with session_factory() as session:
        assert session.scalars(select(FileObject)).all() == []
    assert list(tmp_path.rglob("*.pdf")) == []
    assert list(tmp_path.rglob("*.part")) == []


def test_encrypted_pdf_leaves_no_file_and_no_row(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    user: User,
) -> None:
    store = LocalFileStore(tmp_path)

    with session_factory() as session:
        with pytest.raises(Exception, match="加密"):
            store_temporary_pdf(
                session=session,
                store=store,
                owner_user_id=user.id,
                kind=FileKind.SOURCE,
                stream=BytesIO(make_encrypted_pdf_bytes()),
                max_pages=30,
            )
        session.rollback()

    with session_factory() as session:
        assert session.scalars(select(FileObject)).all() == []
    assert list(tmp_path.rglob("*.pdf")) == []
    assert list(tmp_path.rglob("*.part")) == []


def test_over_page_limit_pdf_leaves_no_file_and_no_row(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    user: User,
) -> None:
    store = LocalFileStore(tmp_path)

    with session_factory() as session:
        with pytest.raises(Exception, match="最多支持 2 页"):
            store_temporary_pdf(
                session=session,
                store=store,
                owner_user_id=user.id,
                kind=FileKind.SOURCE,
                stream=BytesIO(make_pdf_bytes(3)),
                max_pages=2,
            )
        session.rollback()

    with session_factory() as session:
        assert session.scalars(select(FileObject)).all() == []
    assert list(tmp_path.rglob("*.pdf")) == []
    assert list(tmp_path.rglob("*.part")) == []


def test_over_size_limit_pdf_leaves_no_file_and_no_row(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    user: User,
) -> None:
    store = LocalFileStore(tmp_path, max_bytes=512)

    with session_factory() as session:
        with pytest.raises(Exception, match="超过"):
            store_temporary_pdf(
                session=session,
                store=store,
                owner_user_id=user.id,
                kind=FileKind.SOURCE,
                stream=BytesIO(make_pdf_bytes(2)),
                max_pages=30,
            )
        session.rollback()

    with session_factory() as session:
        assert session.scalars(select(FileObject)).all() == []
    assert list(tmp_path.rglob("*.pdf")) == []
    assert list(tmp_path.rglob("*.part")) == []
