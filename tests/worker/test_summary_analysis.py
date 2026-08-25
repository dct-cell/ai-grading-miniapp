from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from worker.runtime.legacy.internal_analysis import (
    InternalAnalysisValidationError,
    validate_internal_analysis,
)


def _case(
    *,
    standard: str = "imo",
    maximum: int = 7,
    score: int = 7,
    problem_number: int | None = None,
    rubric_source: str = "profile",
    submission_status: str = "answered",
    repair_scope: str = "local",
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    scope = "problem_set" if standard == "league_second_round" else None
    profile = {
        "service_tier": "summary_report",
        "grading_standard": standard,
        "league_scope": scope,
        "league_problem_number": problem_number,
    }
    header = {
        "analysis_version": 1,
        "grading_standard": standard,
        "resolved_league_scope": scope,
    }
    evidence = [] if submission_status == "missing" else [
        {
            "id": "p1-a1",
            "claim": "学生完成了决定得分的关键论证。",
            "verdict": "valid",
            "reason": "条件、推导和结论均已核验。",
        }
    ]
    lost = maximum - score
    root_issues = [] if lost == 0 else [
        {
            "id": "p1-e1",
            "description": "存在一个影响得分的独立缺口。",
            "repair_scope": repair_scope,
            "evidence_ids": [],
        }
    ]
    public_issues = [] if lost == 0 else [
        {
            "title": "关键缺口",
            "reason": "该缺口使相应分值不能获得。",
            "deduction": lost,
        }
    ]
    analysis = {
        **header,
        "reference_use": (
            {
                "status": "used",
                "note": "已核验并采用参考 PDF 中的题目专属评分标准。",
            }
            if rubric_source == "specific"
            else {
                "status": "absent",
                "note": "未提供参考 PDF。",
            }
        ),
        "problems": [
            {
                "id": "p1",
                "label": "第 1 题",
                "pages": [1],
                "submission_status": submission_status,
                "target": "证明题目要求的结论。",
                "student_route": (
                    "未作答" if submission_status == "missing" else "学生采用一条相关证明路线。"
                ),
                "interpretations": [],
                "evidence": evidence,
                "root_issues": root_issues,
            }
        ],
    }
    audit_issues = [
        {"issue_id": "p1-e1", **issue} for issue in public_issues
    ]
    audit = {
        **header,
        "total_score": score,
        "max_score": maximum,
        "problems": [
            {
                "problem_id": "p1",
                "submission_status": submission_status,
                "score": score,
                "max_score": maximum,
                "rubric_source": rubric_source,
                "rubric_reference": (
                    {
                        "source": "reference_pdf",
                        "description": "参考 PDF 中与本题精确对应的评分标准。",
                    }
                    if rubric_source == "specific"
                    else None
                ),
                "credit_evidence_ids": ["p1-a1"] if score > 0 else [],
                "verdict": "关键成果与失分原因已经核验。",
                "issues": audit_issues,
                "review": {
                    "typo_checked": True,
                    "independent_credit_checked": True,
                    "double_count_checked": True,
                    "band_and_total_checked": True,
                },
            }
        ],
    }
    grading = {
        "service_tier": "summary_report",
        "grading_standard": standard,
        "resolved_league_scope": scope,
        "title": "数学竞赛题批改",
        "total_score": score,
        "max_score": maximum,
        "problems": [
            {
                "label": "第 1 题",
                "score": score,
                "max_score": maximum,
                "verdict": "关键成果与失分原因已经核验。",
                "issues": public_issues,
            }
        ],
    }
    return profile, analysis, audit, grading


def _write(job_dir: Path, analysis: dict[str, Any], audit: dict[str, Any]) -> None:
    internal = job_dir / "output" / "internal"
    internal.mkdir(parents=True)
    for name, payload in (
        ("summary-analysis.json", analysis),
        ("summary-audit.json", audit),
    ):
        (internal / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def _validate_case(
    tmp_path: Path,
    profile: dict[str, Any],
    analysis: dict[str, Any],
    audit: dict[str, Any],
    grading: dict[str, Any],
) -> None:
    if analysis["reference_use"]["status"] in {"used", "rejected"}:
        reference = tmp_path / "input" / "reference.pdf"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"%PDF-1.4\n% test reference\n")
    _write(tmp_path, analysis, audit)
    validate_internal_analysis(
        tmp_path,
        profile=profile,
        grading=grading,
        input_page_count=1,
    )


@pytest.mark.parametrize(
    ("standard", "maximum", "score"),
    [
        ("imo", 7, 7),
        ("imo", 7, 6),
        ("imo", 7, 0),
        ("cmo", 21, 18),
    ],
)
def test_compact_summary_accepts_full_partial_and_zero_scores(
    tmp_path: Path, standard: str, maximum: int, score: int
) -> None:
    _validate_case(tmp_path, *_case(standard=standard, maximum=maximum, score=score))


def test_full_score_can_record_a_uniquely_recoverable_typo(tmp_path: Path) -> None:
    profile, analysis, audit, grading = _case()
    analysis["problems"][0]["interpretations"] = [
        {"reading": "按上下文唯一恢复为 x > 0。", "score_relevant": False}
    ]

    _validate_case(tmp_path, profile, analysis, audit, grading)


def test_missing_solution_is_a_compact_zero_score(tmp_path: Path) -> None:
    _validate_case(
        tmp_path,
        *_case(score=0, submission_status="missing", repair_scope="global"),
    )


def test_independent_valid_credit_survives_an_unrelated_invalid_claim(
    tmp_path: Path,
) -> None:
    profile, analysis, audit, grading = _case(score=1, repair_scope="global")
    analysis["problems"][0]["evidence"].append(
        {
            "id": "p1-a2",
            "claim": "另一条分支含有错误。",
            "verdict": "invalid",
            "reason": "该分支使用了不成立的断言。",
        }
    )
    analysis["problems"][0]["root_issues"][0]["evidence_ids"] = ["p1-a2"]

    _validate_case(tmp_path, profile, analysis, audit, grading)


@pytest.mark.parametrize(
    ("problem_number", "maximum", "allowed"),
    [
        (1, 40, [0, 10, 30, 40]),
        (3, 50, [0, 10, 40, 50]),
    ],
)
def test_league_profile_accepts_the_common_four_bands(
    tmp_path: Path, problem_number: int, maximum: int, allowed: list[int]
) -> None:
    for index, score in enumerate(allowed):
        case_dir = tmp_path / str(index)
        _validate_case(
            case_dir,
            *_case(
                standard="league_second_round",
                maximum=maximum,
                score=score,
                problem_number=problem_number,
            ),
        )


def test_verified_specific_league_rubric_may_use_an_intermediate_band(
    tmp_path: Path,
) -> None:
    _validate_case(
        tmp_path,
        *_case(
            standard="league_second_round",
            maximum=40,
            score=20,
            problem_number=1,
            rubric_source="specific",
        ),
    )


def test_specific_league_rubric_requires_a_real_verified_source(
    tmp_path: Path,
) -> None:
    profile, analysis, audit, grading = _case(
        standard="league_second_round",
        maximum=40,
        score=20,
        problem_number=1,
        rubric_source="specific",
    )
    analysis["reference_use"] = {
        "status": "absent",
        "note": "未提供参考 PDF。",
    }

    with pytest.raises(InternalAnalysisValidationError, match="未实际采用参考 PDF"):
        _validate_case(tmp_path, profile, analysis, audit, grading)


def test_specific_web_rubric_requires_and_accepts_enabled_verification(
    tmp_path: Path,
) -> None:
    profile, analysis, audit, grading = _case(
        standard="league_second_round",
        maximum=40,
        score=20,
        problem_number=1,
        rubric_source="specific",
    )
    analysis["reference_use"] = {
        "status": "absent",
        "note": "未提供参考 PDF；评分标准来自已核验网页。",
    }
    audit["problems"][0]["rubric_reference"] = {
        "source": "web",
        "description": "已核验的本题官方评分标准。",
    }

    with pytest.raises(InternalAnalysisValidationError, match="未启用网页评分标准核验"):
        _validate_case(tmp_path / "disabled", profile, analysis, audit, grading)

    enabled = tmp_path / "enabled"
    instructions = enabled / "input" / "instructions.txt"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("请核验本题官方评分标准。", encoding="utf-8")
    _validate_case(enabled, profile, analysis, audit, grading)


def test_full_score_rejects_unaccounted_invalid_decisive_evidence(
    tmp_path: Path,
) -> None:
    profile, analysis, audit, grading = _case()
    analysis["problems"][0]["evidence"].append(
        {
            "id": "p1-a2",
            "claim": "一个决定性但实际不成立的关键步骤。",
            "verdict": "invalid",
            "reason": "关键推导不成立。",
        }
    )

    with pytest.raises(InternalAnalysisValidationError, match="没有覆盖全部非有效证据"):
        _validate_case(tmp_path, profile, analysis, audit, grading)


def test_unrepairable_league_gap_is_capped_at_ten_by_the_default_rubric(
    tmp_path: Path,
) -> None:
    _validate_case(
        tmp_path,
        *_case(
            standard="league_second_round",
            maximum=40,
            score=10,
            problem_number=1,
            repair_scope="global",
        ),
    )


def test_unrepairable_league_gap_cannot_receive_near_full_default_credit(
    tmp_path: Path,
) -> None:
    case = _case(
        standard="league_second_round",
        maximum=40,
        score=30,
        problem_number=1,
        repair_scope="global",
    )
    with pytest.raises(InternalAnalysisValidationError, match="不得超过 10 分"):
        _validate_case(tmp_path, *case)


@pytest.mark.parametrize(
    ("problem_number", "maximum", "score"),
    [(1, 40, 20), (3, 50, 20), (3, 50, 30)],
)
def test_profile_league_rubric_allows_justified_intermediate_bands(
    tmp_path: Path, problem_number: int, maximum: int, score: int
) -> None:
    _validate_case(
        tmp_path,
        *_case(
            standard="league_second_round",
            maximum=maximum,
            score=score,
            problem_number=problem_number,
        ),
    )


Mutation = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], None]


def _missing_page(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del audit, grading
    analysis["problems"][0]["pages"] = []


def _unknown_credit(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del analysis, grading
    audit["problems"][0]["credit_evidence_ids"] = ["p1-unknown"]


def _invalid_credit(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del audit, grading
    analysis["problems"][0]["evidence"][0]["verdict"] = "invalid"
    analysis["problems"][0]["root_issues"][0]["evidence_ids"] = ["p1-a1"]


def _duplicate_issue(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del analysis, grading
    audit["problems"][0]["issues"].append(
        copy.deepcopy(audit["problems"][0]["issues"][0])
    )


def _wrong_deduction(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del analysis, grading
    audit["problems"][0]["issues"][0]["deduction"] = 2


def _wrong_total(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del analysis, grading
    audit["total_score"] = 5


def _location_field(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del audit, grading
    analysis["problems"][0]["evidence"][0]["location"] = "第 1 行"


def _unfinished_review(analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]) -> None:
    del analysis, grading
    audit["problems"][0]["review"]["typo_checked"] = False


def _unreported_root_issue(
    analysis: dict[str, Any], audit: dict[str, Any], grading: dict[str, Any]
) -> None:
    del audit, grading
    analysis["problems"][0]["root_issues"].append(
        {
            "id": "p1-e2",
            "description": "另一个未进入评分理由的根本问题。",
            "repair_scope": "global",
            "evidence_ids": [],
        }
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (_missing_page, "缺少页码"),
        (_unknown_credit, "未知得分证据"),
        (_invalid_credit, "未核验为有效"),
        (_duplicate_issue, "重复标识"),
        (_wrong_deduction, "扣分与得分不一致"),
        (_wrong_total, "总分与最终评分不一致"),
        (_location_field, "不得包含位置字段"),
        (_unfinished_review, "尚未完成全部自查"),
        (_unreported_root_issue, "没有覆盖全部根本问题"),
    ],
)
def test_compact_summary_rejects_inconsistent_or_location_heavy_evidence(
    tmp_path: Path, mutation: Mutation, message: str
) -> None:
    profile, analysis, audit, grading = _case(score=6)
    mutation(analysis, audit, grading)

    with pytest.raises(InternalAnalysisValidationError, match=message):
        _validate_case(tmp_path, profile, analysis, audit, grading)
