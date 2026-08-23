from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.models import FileObject, PriceRule, QuoteSession
from server.services.files import FileKind, FileState
from tests.server.conftest import (
    authenticate,
    create_quote,
    make_encrypted_pdf_bytes,
    make_pdf_bytes,
)


def test_quote_counts_source_pages_and_uses_versioned_price(
    authenticated_client: TestClient,
    two_page_pdf: bytes,
) -> None:
    response = authenticated_client.post(
        "/api/v1/quotes",
        files={"source_pdf": ("answers.pdf", two_page_pdf, "application/pdf")},
        data={
            "service_tier": "annotated_review",
            "grading_standard": "imo",
            "note": "重点检查下界证明",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["page_count"] == 2
    assert body["cents_per_page"] == 500
    assert body["amount_cents"] == 1000
    assert body["expires_in_seconds"] == 86400
    assert body["grading_standard"] == "imo"
    assert body["note"] == "重点检查下界证明"


def test_quote_snapshots_the_price_rule_and_amount(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    body = create_quote(authenticated_client, pages=3)

    with session_factory() as session:
        quote = session.get(QuoteSession, body["id"])
        assert quote is not None
        rule = session.get(PriceRule, quote.price_rule_id)

    assert rule is not None
    assert rule.cents_per_page == 500
    assert quote.quoted_amount_cents == 1500
    assert quote.page_count == 3
    assert quote.consumed_at is None


def test_repricing_does_not_change_an_existing_quote(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = create_quote(authenticated_client, pages=2)
    with session_factory() as session:
        rule = session.get(PriceRule, session.get(QuoteSession, first["id"]).price_rule_id)
        rule.retired_at = datetime.now(timezone.utc)
        session.add(rule)
        session.commit()

    second = create_quote(authenticated_client, pages=2)

    assert first["amount_cents"] == 1000
    with session_factory() as session:
        unchanged = session.get(QuoteSession, first["id"])
        assert unchanged.quoted_amount_cents == 1000
        assert second["amount_cents"] == 1000
        assert (
            session.get(QuoteSession, second["id"]).price_rule_id
            != unchanged.price_rule_id
        )


def test_quote_freezes_the_grading_standard_and_note(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    body = create_quote(
        authenticated_client,
        grading_standard="league_second_round",
        note="第三题只看后半部分",
    )

    with session_factory() as session:
        quote = session.get(QuoteSession, body["id"])

    assert quote.grading_standard == "league_second_round"
    assert quote.note == "第三题只看后半部分"


def test_quote_records_the_authenticated_owner(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    user = authenticate(authenticated_client, "test-owner")
    body = create_quote(authenticated_client)

    with session_factory() as session:
        quote = session.get(QuoteSession, body["id"])
        source = session.get(FileObject, quote.source_file_id)

    assert quote.owner_user_id == user["id"]
    assert source.owner_user_id == user["id"]


def test_quote_expires_files_and_session_in_24_hours(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    body = create_quote(authenticated_client, reference_pages=1)
    expected = datetime.now(timezone.utc) + timedelta(hours=24)

    with session_factory() as session:
        quote = session.get(QuoteSession, body["id"])
        source = session.get(FileObject, quote.source_file_id)
        reference = session.get(FileObject, quote.reference_file_id)

    for moment in (quote.expires_at, source.expires_at, reference.expires_at):
        assert abs((moment - expected).total_seconds()) < 60
    assert datetime.fromisoformat(body["expires_at"]) == quote.expires_at


def test_reference_pdf_is_stored_but_never_priced(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    body = create_quote(authenticated_client, pages=2, reference_pages=5)

    assert body["page_count"] == 2
    assert body["amount_cents"] == 1000
    with session_factory() as session:
        quote = session.get(QuoteSession, body["id"])
        reference = session.get(FileObject, quote.reference_file_id)

    assert reference.kind == FileKind.REFERENCE
    assert reference.state == FileState.TEMPORARY


def test_quote_without_reference_stores_only_the_source_file(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    create_quote(authenticated_client)

    with session_factory() as session:
        files = session.scalars(select(FileObject)).all()

    assert [record.kind for record in files] == [FileKind.SOURCE]


def test_quote_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/api/v1/quotes",
        files={"source_pdf": ("answers.pdf", make_pdf_bytes(1), "application/pdf")},
        data={"service_tier": "annotated_review", "grading_standard": "imo"},
    )

    assert response.status_code == 401


def test_quote_rejects_unknown_grading_standards(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = authenticated_client.post(
        "/api/v1/quotes",
        files={"source_pdf": ("answers.pdf", make_pdf_bytes(1), "application/pdf")},
        data={"service_tier": "annotated_review", "grading_standard": "gaokao"},
    )

    assert response.status_code == 422
    with session_factory() as session:
        assert session.scalars(select(QuoteSession)).all() == []
        assert session.scalars(select(FileObject)).all() == []


def test_quote_rejects_encrypted_pdf_without_persisting_anything(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    response = authenticated_client.post(
        "/api/v1/quotes",
        files={
            "source_pdf": (
                "answers.pdf",
                make_encrypted_pdf_bytes(),
                "application/pdf",
            )
        },
        data={"service_tier": "annotated_review", "grading_standard": "imo"},
    )

    assert response.status_code == 400
    assert "加密" in response.json()["detail"]
    with session_factory() as session:
        assert session.scalars(select(FileObject)).all() == []
    assert list(settings.data_dir.rglob("*.pdf")) == []
    assert list(settings.data_dir.rglob("*.part")) == []


def test_quote_rejects_corrupt_pdf(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = authenticated_client.post(
        "/api/v1/quotes",
        files={"source_pdf": ("answers.pdf", b"not a pdf", "application/pdf")},
        data={"service_tier": "annotated_review", "grading_standard": "imo"},
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert session.scalars(select(FileObject)).all() == []


def test_quote_rejects_pdf_over_the_page_limit(tmp_path) -> None:
    from tests.server.conftest import build_client, build_settings

    settings = build_settings(tmp_path, max_pdf_pages=2)
    with build_client(settings) as client:
        authenticate(client)
        response = client.post(
            "/api/v1/quotes",
            files={"source_pdf": ("answers.pdf", make_pdf_bytes(3), "application/pdf")},
            data={"service_tier": "annotated_review", "grading_standard": "imo"},
        )

    assert response.status_code == 400
    assert "最多支持 2 页" in response.json()["detail"]
    assert list(settings.data_dir.rglob("*.pdf")) == []


def test_quote_rejects_pdf_over_the_size_limit(tmp_path) -> None:
    from tests.server.conftest import build_client, build_settings

    settings = build_settings(tmp_path, max_pdf_bytes=1024)
    with build_client(settings) as client:
        authenticate(client)
        response = client.post(
            "/api/v1/quotes",
            files={"source_pdf": ("answers.pdf", make_pdf_bytes(4), "application/pdf")},
            data={"service_tier": "annotated_review", "grading_standard": "imo"},
        )

    assert response.status_code == 400
    assert list(settings.data_dir.rglob("*.pdf")) == []
    assert list(settings.data_dir.rglob("*.part")) == []


def test_reference_pdf_failure_removes_the_already_stored_source(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    response = authenticated_client.post(
        "/api/v1/quotes",
        files={
            "source_pdf": ("answers.pdf", make_pdf_bytes(1), "application/pdf"),
            "reference_pdf": ("reference.pdf", b"broken", "application/pdf"),
        },
        data={"service_tier": "annotated_review", "grading_standard": "imo"},
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert session.scalars(select(FileObject)).all() == []
        assert session.scalars(select(QuoteSession)).all() == []
    assert list(settings.data_dir.rglob("*.pdf")) == []
    assert list(settings.data_dir.rglob("*.part")) == []


def test_quote_note_defaults_to_empty_and_is_length_limited(
    authenticated_client: TestClient,
) -> None:
    body = create_quote(authenticated_client)
    assert body["note"] == ""

    response = authenticated_client.post(
        "/api/v1/quotes",
        files={"source_pdf": ("answers.pdf", make_pdf_bytes(1), "application/pdf")},
        data={
            "service_tier": "annotated_review",
            "grading_standard": "imo",
            "note": "长" * 2001,
        },
    )
    assert response.status_code == 422


def test_owner_can_read_their_own_quote(authenticated_client: TestClient) -> None:
    created = create_quote(authenticated_client)

    response = authenticated_client.get(f"/api/v1/quotes/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_user_cannot_read_another_users_quote(client: TestClient) -> None:
    authenticate(client, "test-alice")
    alice_quote = create_quote(client)
    authenticate(client, "test-bob")

    response = client.get(f"/api/v1/quotes/{alice_quote['id']}")

    assert response.status_code == 404
    assert alice_quote["id"] not in response.text


def test_unknown_quote_returns_404(authenticated_client: TestClient) -> None:
    response = authenticated_client.get("/api/v1/quotes/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "报价不存在或已失效。"


@pytest.mark.parametrize("standard", ["league_second_round", "cmo", "imo"])
def test_all_supported_grading_standards_are_accepted(
    authenticated_client: TestClient,
    standard: str,
) -> None:
    assert create_quote(authenticated_client, grading_standard=standard)[
        "grading_standard"
    ] == standard
