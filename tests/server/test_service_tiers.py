from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.models import GradingRound, Order, QuoteSession
from server.services.grading_result_validation import (
    GradingResultInvalid,
    validate_staged_result,
)
from tests.server.conftest import (
    authenticate,
    build_client,
    build_settings,
    create_quote,
    deliver_round,
    make_pdf_bytes,
    pay_for_new_order,
    register_worker,
    worker_headers,
)


def _summary_result(*, tier: str = "summary_report", score: int = 6) -> dict:
    return {
        "service_tier": tier,
        "grading_standard": "imo",
        "resolved_league_scope": None,
        "title": "数学竞赛题批改",
        "total_score": score,
        "max_score": 7,
        "problems": [
            {
                "label": "第 1 题",
                "score": score,
                "max_score": 7,
                "verdict": "方法正确，论证完整性略有不足。",
                "issues": [] if score == 7 else [
                    {
                        "title": "条件说明不足",
                        "reason": "使用结论前没有核验必要条件。",
                        "deduction": 7 - score,
                    }
                ],
            }
        ],
    }


def _write_pdf(path: Path, *, pages: int, width: float = 595.28, height: float = 841.89) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    with path.open("wb") as stream:
        writer.write(stream)


def test_service_tier_catalog_is_server_authoritative(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/api/v1/service-tiers")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "id": "summary_report",
            "label": "简明评分",
            "description": "给出总分、分题判断和主要问题。",
            "delivery_label": "A4 评分报告",
            "cents_per_page": 100,
            "enabled": True,
        },
        {
            "id": "annotated_review",
            "label": "逐页精批",
            "description": "在答卷对应位置标注，并给出逐页批改报告。",
            "delivery_label": "逐页批改报告",
            "cents_per_page": 500,
            "enabled": True,
        },
    ]


@pytest.mark.parametrize(
    ("service_tier", "expected_unit_price", "expected_total"),
    [
        ("summary_report", 100, 300),
        ("annotated_review", 500, 1500),
    ],
)
def test_quote_uses_tier_price_and_never_prices_reference_pages(
    authenticated_client: TestClient,
    service_tier: str,
    expected_unit_price: int,
    expected_total: int,
) -> None:
    quote = create_quote(
        authenticated_client,
        pages=3,
        reference_pages=5,
        service_tier=service_tier,
    )

    assert quote["service_tier"] == service_tier
    assert quote["page_count"] == 3
    assert quote["cents_per_page"] == expected_unit_price
    assert quote["amount_cents"] == expected_total


def test_unknown_tier_is_rejected_before_any_quote_is_persisted(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = authenticated_client.post(
        "/api/v1/quotes",
        files={"source_pdf": ("answers.pdf", make_pdf_bytes(), "application/pdf")},
        data={"service_tier": "cheap_but_annotated", "grading_standard": "imo"},
    )

    assert response.status_code == 422
    with session_factory() as session:
        assert session.scalars(select(QuoteSession)).all() == []


def test_summary_tier_can_be_hidden_by_feature_flag(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, summary_report_enabled=False)
    with build_client(settings) as client:
        authenticate(client)
        catalog = client.get("/api/v1/service-tiers").json()["items"]
        response = client.post(
            "/api/v1/quotes",
            files={"source_pdf": ("answers.pdf", make_pdf_bytes(), "application/pdf")},
            data={"service_tier": "summary_report", "grading_standard": "imo"},
        )

    assert catalog[0]["enabled"] is False
    assert response.status_code == 422
    assert response.json()["detail"] == "简明评分尚未开放。"


def test_paid_tier_and_league_scope_are_frozen_into_round_and_lease(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = pay_for_new_order(
        authenticated_client,
        pages=4,
        service_tier="summary_report",
        grading_standard="league_second_round",
    )
    worker_id = register_worker(
        authenticated_client,
        installation_id="service-tier-freeze",
    )["worker_id"]
    lease = authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )

    assert lease.status_code == 200, lease.text
    body = lease.json()
    assert body["order_id"] == order_id
    assert body["service_tier"] == "summary_report"
    assert body["grading_standard"] == "league_second_round"
    assert body["league_scope"] == "auto"
    with session_factory() as session:
        round_one = session.scalar(
            select(GradingRound).where(
                GradingRound.order_id == order_id,
                GradingRound.round_number == 1,
            )
        )
    assert round_one.service_tier == "summary_report"
    assert round_one.league_scope == "auto"


def test_review_inherits_the_original_tier_without_upgrade_or_downgrade(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = pay_for_new_order(
        authenticated_client,
        pages=1,
        service_tier="summary_report",
    )
    worker_id = register_worker(
        authenticated_client,
        installation_id="service-tier-review",
    )["worker_id"]
    deliver_round(
        authenticated_client,
        worker_id,
        expected_order_id=order_id,
    )
    review = authenticated_client.post(
        f"/api/v1/orders/{order_id}/review",
        json={"text": "请重新核验第 1 题的条件。"},
    )

    assert review.status_code == 202, review.text
    with session_factory() as session:
        rounds = session.scalars(
            select(GradingRound)
            .where(GradingRound.order_id == order_id)
            .order_by(GradingRound.round_number)
        ).all()
        order = session.get(Order, order_id)
    assert [record.service_tier for record in rounds] == [
        "summary_report",
        "summary_report",
    ]
    assert order.current_round_number == 2


def test_summary_contract_accepts_a4_and_rejects_cross_tier_delivery(
    tmp_path: Path,
) -> None:
    json_path = tmp_path / "grading.json"
    pdf_path = tmp_path / "report.pdf"
    json_path.write_text(json.dumps(_summary_result(), ensure_ascii=False), encoding="utf-8")
    _write_pdf(pdf_path, pages=1)

    validate_staged_result(
        json_path=json_path,
        pdf_path=pdf_path,
        service_tier="summary_report",
        grading_standard="imo",
        league_scope=None,
        source_page_count=2,
    )

    json_path.write_text(
        json.dumps(_summary_result(tier="annotated_review"), ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(GradingResultInvalid, match="档位"):
        validate_staged_result(
            json_path=json_path,
            pdf_path=pdf_path,
            service_tier="summary_report",
            grading_standard="imo",
            league_scope=None,
            source_page_count=2,
        )


def test_summary_contract_rejects_non_a4_and_illegal_score_band(tmp_path: Path) -> None:
    json_path = tmp_path / "grading.json"
    pdf_path = tmp_path / "report.pdf"
    json_path.write_text(json.dumps(_summary_result(score=5), ensure_ascii=False), encoding="utf-8")
    _write_pdf(pdf_path, pages=1, width=612, height=792)

    with pytest.raises(GradingResultInvalid, match="A4"):
        validate_staged_result(
            json_path=json_path,
            pdf_path=pdf_path,
            service_tier="summary_report",
            grading_standard="imo",
            league_scope=None,
            source_page_count=1,
        )


def test_summary_contract_rejects_issue_deduction_that_does_not_match_score(
    tmp_path: Path,
) -> None:
    json_path = tmp_path / "grading.json"
    pdf_path = tmp_path / "report.pdf"
    payload = _summary_result(score=6)
    payload["problems"][0]["issues"][0]["deduction"] = 0
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _write_pdf(pdf_path, pages=1)

    with pytest.raises(GradingResultInvalid, match="失分一致"):
        validate_staged_result(
            json_path=json_path,
            pdf_path=pdf_path,
            service_tier="summary_report",
            grading_standard="imo",
            league_scope=None,
            source_page_count=1,
        )


def test_summary_contract_rejects_removed_fields_and_full_score_issues(
    tmp_path: Path,
) -> None:
    json_path = tmp_path / "grading.json"
    pdf_path = tmp_path / "report.pdf"
    _write_pdf(pdf_path, pages=1)

    payload = _summary_result(score=7)
    payload["overall_summary"] = "不应再出现。"
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GradingResultInvalid, match="总体判断"):
        validate_staged_result(
            json_path=json_path,
            pdf_path=pdf_path,
            service_tier="summary_report",
            grading_standard="imo",
            league_scope=None,
            source_page_count=1,
        )

    payload = _summary_result(score=7)
    payload["problems"][0]["suggestion"] = "不应再出现。"
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GradingResultInvalid, match="独立建议"):
        validate_staged_result(
            json_path=json_path,
            pdf_path=pdf_path,
            service_tier="summary_report",
            grading_standard="imo",
            league_scope=None,
            source_page_count=1,
        )

    payload = _summary_result(score=7)
    payload["problems"][0]["issues"] = [{
        "title": "不应扣分",
        "reason": "满分题不能列出扣分问题。",
        "deduction": 0,
    }]
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GradingResultInvalid, match="满分题"):
        validate_staged_result(
            json_path=json_path,
            pdf_path=pdf_path,
            service_tier="summary_report",
            grading_standard="imo",
            league_scope=None,
            source_page_count=1,
        )
