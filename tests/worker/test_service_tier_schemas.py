from __future__ import annotations

import json
from pathlib import Path

import pytest


jsonschema = pytest.importorskip("jsonschema")
RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "worker" / "runtime"


def _validator(name: str):
    payload = json.loads((RUNTIME_ROOT / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(payload)


def summary_grading() -> dict:
    return {
        "service_tier": "summary_report",
        "grading_standard": "imo",
        "resolved_league_scope": None,
        "title": "数学竞赛题批改",
        "total_score": 6,
        "max_score": 7,
        "problems": [{
            "label": "第 1 题",
            "score": 6,
            "max_score": 7,
            "verdict": "一个条件说明不足。",
            "issues": [{
                "title": "条件说明不足",
                "reason": "使用结论前没有核验必要条件。",
                "deduction": 1,
            }],
        }],
    }


def annotated_grading() -> dict:
    return {
        "service_tier": "annotated_review",
        "grading_standard": "imo",
        "resolved_league_scope": None,
        "title": "数学竞赛题批改",
        "total_score": 6,
        "max_score": 7,
        "overall_summary": "主体方法正确。",
        "problems": [{
            "label": "第 1 题",
            "score": 6,
            "max_score": 7,
            "summary": "主体成立。",
        }],
        "pages": [{
            "page": 1,
            "problem": "第 1 题",
            "score": 6,
            "max_score": 7,
            "page_summary": "本页主体推导正确。",
            "findings": [],
        }],
    }


def test_tier_specific_grading_schemas_are_mutually_exclusive() -> None:
    summary = _validator("summary-grading.schema.json")
    annotated = _validator("annotated-grading.schema.json")
    summary.validate(summary_grading())
    annotated.validate(annotated_grading())
    with pytest.raises(jsonschema.ValidationError):
        summary.validate(annotated_grading())
    with pytest.raises(jsonschema.ValidationError):
        annotated.validate(summary_grading())


@pytest.mark.parametrize("removed_field", ["overall_summary", "suggestion"])
def test_summary_schema_rejects_removed_public_fields(removed_field: str) -> None:
    summary = _validator("summary-grading.schema.json")
    payload = summary_grading()
    if removed_field == "suggestion":
        payload["problems"][0][removed_field] = "不再公开单独建议。"
    else:
        payload[removed_field] = "不再公开总体判断。"
    with pytest.raises(jsonschema.ValidationError):
        summary.validate(payload)


@pytest.mark.parametrize(
    ("tier", "path"),
    [
        ("summary_report", "output/report.pdf"),
        ("annotated_review", "output/annotated.pdf"),
    ],
)
def test_manifest_accepts_supported_tier_and_output_values(tier: str, path: str) -> None:
    validator = _validator("legacy/manifest.schema.json")
    manifest = {
        "output_pdf": path,
        "page_count": 1,
        "summary": "完成",
        "service_tier": tier,
        "score": 6,
        "max_score": 7,
        "grading_standard": "imo",
        "resolved_league_scope": None,
    }
    validator.validate(manifest)


def test_manifest_schema_avoids_unsupported_response_composition() -> None:
    schema = json.loads(
        (RUNTIME_ROOT / "legacy/manifest.schema.json").read_text(encoding="utf-8")
    )

    def assert_supported_shape(value: object) -> None:
        if isinstance(value, dict):
            assert not {"oneOf", "anyOf", "allOf"}.intersection(value)
            for child in value.values():
                assert_supported_shape(child)
        elif isinstance(value, list):
            for child in value:
                assert_supported_shape(child)

    assert_supported_shape(schema)


def test_summary_schema_forbids_annotated_pages() -> None:
    grading = summary_grading()
    grading["pages"] = []
    with pytest.raises(jsonschema.ValidationError):
        _validator("summary-grading.schema.json").validate(grading)


def test_annotated_schema_accepts_public_math_fields_and_informational_kind() -> None:
    grading = annotated_grading()
    grading["pages"][0]["findings"] = [{
        "id": 1,
        "kind": "informational",
        "title": "关键得分点",
        "reason": "结论成立。",
        "deduction": 0,
        "source_quote": "因此命题成立",
        "formula": r"a^2+b^2=c^2",
    }]
    _validator("annotated-grading.schema.json").validate(grading)
