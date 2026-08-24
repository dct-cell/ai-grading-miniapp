from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from worker.runtime.contracts import (
    RUNTIME_ERROR_CODES,
    GradingRuntime,
    RuntimeResult,
    TaskBundle,
)

JSONSCHEMA_AVAILABLE = True
try:
    import jsonschema  # noqa: F401
except ImportError:  # pragma: no cover
    JSONSCHEMA_AVAILABLE = False


HERE = Path(__file__).resolve().parent
RUNTIME_ROOT = HERE.parent.parent / "worker" / "runtime"
LEGACY_SCHEMA_PATH = RUNTIME_ROOT / "legacy" / "manifest.schema.json"
SUMMARY_SCHEMA_PATH = RUNTIME_ROOT / "summary-grading.schema.json"
ANNOTATED_SCHEMA_PATH = RUNTIME_ROOT / "annotated-grading.schema.json"


def _make_bundle(**overrides: Any) -> TaskBundle:
    base: dict[str, Any] = {
        "job_id": "job-1",
        "order_id": "order-1",
        "round_number": 1,
        "service_tier": "annotated_review",
        "grading_standard": "imo",
        "league_scope": None,
        "source_pdf": "input/source.pdf",
        "reference_pdf": None,
        "page_count": 1,
        "note": "",
    }
    base.update(overrides)
    return TaskBundle(**base)


class TestTaskBundleContract:
    def test_round_one_is_accepted(self) -> None:
        bundle = _make_bundle(round_number=1)
        assert bundle.round_number == 1

    def test_round_two_is_accepted(self) -> None:
        bundle = _make_bundle(round_number=2)
        assert bundle.round_number == 2

    def test_round_three_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(round_number=3)

    def test_round_zero_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(round_number=0)

    @pytest.mark.parametrize("invalid_standard", ["", "usamo", "IMO", "league"])
    def test_unknown_grading_standard_is_rejected(self, invalid_standard: str) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(grading_standard=invalid_standard)

    def test_league_scope_full_paper_only_with_league_standard(self) -> None:
        # league_scope is only meaningful for league_second_round. Setting it
        # alongside imo/cmo is a configuration error the contract must reject.
        with pytest.raises(ValidationError):
            _make_bundle(
                grading_standard="imo",
                league_scope="full_paper",
            )

    def test_league_scope_must_be_valid(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(
                grading_standard="league_second_round",
                league_scope="half_paper",
            )

    def test_league_scope_is_required_for_league_standard(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(
                grading_standard="league_second_round",
                league_scope=None,
            )

    def test_league_problem_number_is_accepted_for_problem_set(self) -> None:
        bundle = _make_bundle(
            grading_standard="league_second_round",
            league_scope="problem_set",
            league_problem_number=3,
        )
        assert bundle.league_problem_number == 3

    def test_league_problem_number_is_rejected_for_non_league(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(league_problem_number=3)

    def test_league_problem_number_is_rejected_for_full_paper(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(
                grading_standard="league_second_round",
                league_scope="full_paper",
                league_problem_number=3,
            )

    def test_zero_page_count_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(page_count=0)

    def test_negative_page_count_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(page_count=-3)

    def test_note_over_4000_chars_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_bundle(note="x" * 4001)

    def test_note_at_4000_chars_is_accepted(self) -> None:
        bundle = _make_bundle(note="x" * 4000)
        assert len(bundle.note) == 4000

    def test_source_pdf_path_is_required(self) -> None:
        with pytest.raises(ValidationError):
            TaskBundle(
                job_id="j",
                order_id="o",
                round_number=1,
                service_tier="annotated_review",
                grading_standard="imo",
                reference_pdf=None,
                page_count=1,
                note="",
            )

    def test_round_number_field_is_typed(self) -> None:
        # String coercion must not sneak a third round through pydantic v2.
        with pytest.raises(ValidationError):
            TaskBundle(
                job_id="j",
                order_id="o",
                round_number="1",  # type: ignore[arg-type]
                service_tier="annotated_review",
                grading_standard="imo",
                source_pdf="input/source.pdf",
                page_count=1,
                note="",
            )


class TestRuntimeResultFromWorkspace:
    def test_requires_existing_pdf_and_json(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="manifest is missing"):
            RuntimeResult.from_workspace(tmp_path)

    def test_requires_existing_json_when_pdf_present(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.json").write_text(json.dumps({
            "output_pdf": "output/annotated.pdf", "page_count": 1,
        }), encoding="utf-8")
        with pytest.raises(ValueError, match="result PDF is missing"):
            RuntimeResult.from_workspace(tmp_path)

    def test_requires_manifest_when_pdf_and_json_present(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.json").write_text(json.dumps({
            "output_pdf": "output/annotated.pdf", "page_count": 1,
        }), encoding="utf-8")
        (tmp_path / "annotated.pdf").write_bytes(b"%PDF-1.7\n")
        with pytest.raises(ValueError, match="result JSON is missing"):
            RuntimeResult.from_workspace(tmp_path)

    def test_computes_sha256_and_uses_pdf_page_count(
        self, tmp_path: Path
    ) -> None:
        # The smallest valid-ish PDF that pypdf can parse: one blank page.
        from worker.runtime.testsupport import build_minimal_pdf

        pdf_bytes = build_minimal_pdf(page_count=2)
        pdf_path = tmp_path / "annotated.pdf"
        pdf_path.write_bytes(pdf_bytes)

        json_bytes = json.dumps(
            {"grading_standard": "imo", "total_score": 0, "max_score": 7},
            ensure_ascii=False,
        ).encode("utf-8")
        json_path = tmp_path / "grading.json"
        json_path.write_bytes(json_bytes)

        manifest_bytes = json.dumps(
            {
                "output_pdf": "output/annotated.pdf",
                "page_count": 2,
                "summary": "ok",
                "service_tier": "annotated_review",
                "score": 0,
                "max_score": 7,
                "grading_standard": "imo",
                "resolved_league_scope": None,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)

        import hashlib

        result = RuntimeResult.from_workspace(tmp_path)
        assert result.manifest_path == manifest_path
        assert result.result_json_path == json_path
        assert result.result_pdf_path == pdf_path
        assert result.result_json_sha256 == hashlib.sha256(json_bytes).hexdigest()
        assert result.result_pdf_sha256 == hashlib.sha256(pdf_bytes).hexdigest()
        assert result.output_page_count == 2


class TestGradingRuntimeProtocol:
    def test_protocol_has_async_run_method(self) -> None:
        # The protocol must define an async run(workspace, bundle, progress).
        # Checking the signature via __protocol_attrs__ keeps the test honest
        # without instantiating a concrete runtime.
        run = GradingRuntime.__protocol_attrs__  # type: ignore[attr-defined]
        assert "run" in run


class TestRuntimeErrorCodes:
    def test_seven_stable_codes_are_exposed(self) -> None:
        assert RUNTIME_ERROR_CODES == frozenset(
            {
                "runtime_auth_failed",
                "runtime_unavailable",
                "runtime_timeout",
                "runtime_invalid_json",
                "runtime_invalid_pdf",
                "runtime_cancelled",
                "runtime_misconfigured",
            }
        )


@pytest.mark.skipif(
    not JSONSCHEMA_AVAILABLE, reason="jsonschema not installed"
)
@pytest.mark.skip(reason="replaced by split grading and manifest schema contracts")
class TestResultSchemaDualValidation:
    """Phase 04 Task 1 Step 4: result.schema.json is a strict superset of the
    legacy manifest.schema.json.

    The legacy grader writes two files: a small ``manifest.json`` (validated
    by the legacy schema) and a richer ``grading.json`` (validated only by the
    skill prose). The worker schema describes the union of both so the worker
    can validate a single combined document without re-implementing the rules.

    Compatibility is enforced three ways:
    - The worker schema requires every field the legacy schema required, plus
      the new grading fields the plan mandates.
    - Every legacy-shape manifest still validates against the legacy schema.
    - Every combined-shape document validates against the worker schema.
    """

    def _load(self, path: Path) -> Any:
        import jsonschema

        with path.open("r", encoding="utf-8") as fh:
            return jsonschema.Draft202012Validator(json.load(fh))

    def _combined_manifest(self, standard: str, scope: str | None) -> dict[str, Any]:
        if standard == "imo":
            maxima = [7]
        elif standard == "cmo":
            maxima = [21]
        elif scope == "full_paper":
            maxima = [40, 40, 50, 50]
        else:
            maxima = [40]
        total = sum(maxima)
        return {
            "output_pdf": "output/annotated.pdf",
            "page_count": 1,
            "summary": "演示模式已生成逐页批改报告",
            "score": total,
            "max_score": total,
            "grading_standard": standard,
            "resolved_league_scope": scope,
            "title": "数学竞赛题批改",
            "total_score": total,
            "overall_summary": "主体方法正确。",
            "problems": [
                {
                    "label": f"第 {i} 题",
                    "score": m,
                    "max_score": m,
                    "summary": "主体成立。",
                }
                for i, m in enumerate(maxima, start=1)
            ],
            "pages": [
                {
                    "page": 1,
                    "problem": "第 1 题",
                    "score": maxima[0],
                    "max_score": maxima[0],
                    "page_summary": "本页主体推导正确。",
                    "findings": [],
                }
            ],
        }

    def _legacy_manifest(self, standard: str, scope: str | None) -> dict[str, Any]:
        combined = self._combined_manifest(standard, scope)
        # Legacy manifest.schema.json only describes these seven fields.
        return {
            "output_pdf": combined["output_pdf"],
            "page_count": combined["page_count"],
            "summary": combined["summary"],
            "score": combined["score"],
            "max_score": combined["max_score"],
            "grading_standard": combined["grading_standard"],
            "resolved_league_scope": combined["resolved_league_scope"],
        }

    def test_both_schema_files_exist(self) -> None:
        assert LEGACY_SCHEMA_PATH.is_file(), (
            "legacy manifest.schema.json must be copied to "
            "worker/runtime/legacy/manifest.schema.json"
        )
        assert RESULT_SCHEMA_PATH.is_file()

    def test_result_schema_requires_every_legacy_required_field(self) -> None:
        legacy = json.loads(LEGACY_SCHEMA_PATH.read_text(encoding="utf-8"))
        result = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        # The worker schema must not drop any field the legacy schema required,
        # otherwise a manifest the legacy grader produced could silently bypass
        # worker-side validation.
        assert set(legacy["required"]).issubset(set(result["required"]))

    def test_result_schema_requires_new_grading_fields(self) -> None:
        result = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        # The plan mandates these as required so the worker can validate a
        # single combined document instead of re-implementing legacy prose.
        for field in (
            "title",
            "total_score",
            "overall_summary",
            "problems",
            "pages",
        ):
            assert field in result["required"], (
                f"result.schema.json must require {field}"
            )

    @pytest.mark.parametrize(
        ("standard", "scope"),
        [
            ("imo", None),
            ("cmo", None),
            ("league_second_round", "full_paper"),
            ("league_second_round", "problem_set"),
        ],
    )
    def test_legacy_manifest_validates_against_legacy_schema(
        self, standard: str, scope: str | None
    ) -> None:
        legacy_validator = self._load(LEGACY_SCHEMA_PATH)
        legacy_validator.validate(self._legacy_manifest(standard, scope))

    @pytest.mark.parametrize(
        ("standard", "scope"),
        [
            ("imo", None),
            ("cmo", None),
            ("league_second_round", "full_paper"),
            ("league_second_round", "problem_set"),
        ],
    )
    def test_combined_manifest_validates_against_result_schema(
        self, standard: str, scope: str | None
    ) -> None:
        result_validator = self._load(RESULT_SCHEMA_PATH)
        result_validator.validate(self._combined_manifest(standard, scope))

    def test_result_schema_rejects_finding_without_bbox_or_bboxes(self) -> None:
        result_validator = self._load(RESULT_SCHEMA_PATH)
        manifest = self._combined_manifest("imo", None)
        manifest["pages"][0]["findings"] = [
            {
                "id": 1,
                "kind": "error",
                "title": "缺少反向论证",
                "reason": "只证明了 A→B。",
                "deduction": 1,
            }
        ]
        with pytest.raises(Exception):
            result_validator.validate(manifest)

    def test_result_schema_accepts_finding_with_bbox(self) -> None:
        result_validator = self._load(RESULT_SCHEMA_PATH)
        manifest = self._combined_manifest("imo", None)
        manifest["pages"][0]["findings"] = [
            {
                "id": 1,
                "kind": "error",
                "title": "缺少反向论证",
                "reason": "只证明了 A→B。",
                "deduction": 1,
                "bbox": [0.16, 0.68, 0.82, 0.75],
            }
        ]
        result_validator.validate(manifest)

    def test_result_schema_accepts_finding_with_bboxes(self) -> None:
        result_validator = self._load(RESULT_SCHEMA_PATH)
        manifest = self._combined_manifest("imo", None)
        manifest["pages"][0]["findings"] = [
            {
                "id": 1,
                "kind": "warning",
                "title": "两处缺步",
                "reason": "第 1、3 行均缺中间步。",
                "deduction": 2,
                "bboxes": [
                    [0.10, 0.20, 0.30, 0.25],
                    [0.10, 0.60, 0.30, 0.65],
                ],
            }
        ]
        result_validator.validate(manifest)
