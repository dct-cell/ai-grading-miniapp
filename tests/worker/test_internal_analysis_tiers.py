from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.runtime.legacy.codex_runner import (
    _build_grading_prompt,
)
from worker.runtime.legacy.internal_analysis import (
    InternalAnalysisValidationError,
    validate_internal_analysis,
)


def _header() -> dict[str, object]:
    return {
        "analysis_version": 1,
        "grading_standard": "imo",
        "resolved_league_scope": None,
    }


def _write_internal_artifacts(
    job_dir: Path,
    *,
    include_locations: bool,
) -> dict[str, object]:
    internal_dir = job_dir / "output" / "internal"
    internal_dir.mkdir(parents=True)

    interpretation: dict[str, object] = {
        "reading": "按上下文理解为变量为正数。",
        "score_relevant": False,
    }
    proof_step: dict[str, object] = {
        "id": "p1-s1",
        "claim": "学生建立关键构造并完成所有评分点所需的推导。",
        "category": "claim",
        "depends_on": [],
    }
    if include_locations:
        interpretation["location"] = "第 1 页第 2 行"
        proof_step["page"] = 1
        proof_step["location"] = "第 1 页第 2–8 行"

    checkpoints = [
        {
            "id": f"p1-u{index}-main",
            "slot_id": f"p1-u{index}",
            "points": 1,
            "description": f"第 {index} 个评分单位",
            "depends_on": [],
            "exclusive_group": None,
        }
        for index in range(1, 8)
    ]
    checkpoint_results = [
        {
            "checkpoint_id": checkpoint["id"],
            "awarded": True,
            "points_awarded": 1,
            "evidence_step_ids": ["p1-s1"],
            "reason": "压缩后的关键证明单元足以支持该评分点。",
        }
        for checkpoint in checkpoints
    ]

    artifacts = {
        "problem-analysis.json": {
            **_header(),
            "problems": [
                {
                    "id": "p1",
                    "label": "第 1 题",
                    "pages": [1],
                    "target": "证明目标结论。",
                    "constraints": ["满足题设条件"],
                    "student_route": "通过关键构造完成证明。",
                    "interpretations": [interpretation],
                    "submission_status": "answered",
                }
            ],
        },
        "marking-scheme.json": {
            **_header(),
            "problems": [
                {
                    "problem_id": "p1",
                    "max_score": 7,
                    "unit": 1,
                    "base_units": 7,
                    "checkpoints": checkpoints,
                    "zero_credit": [],
                    "deductions": [],
                    "caps": [],
                }
            ],
        },
        "proof-map.json": {
            **_header(),
            "problems": [{"problem_id": "p1", "steps": [proof_step]}],
        },
        "verification.json": {
            **_header(),
            "problems": [
                {
                    "problem_id": "p1",
                    "root_errors": [],
                    "steps": [
                        {
                            "step_id": "p1-s1",
                            "verdict": "valid",
                            "reason": "关键证明单元中的条件和推导均成立。",
                            "root_error_id": None,
                            "impact": "支持全部评分单位。",
                            "repair_scope": "none",
                        }
                    ],
                }
            ],
        },
        "score-audit.json": {
            **_header(),
            "total_score": 7,
            "max_score": 7,
            "problems": [
                {
                    "problem_id": "p1",
                    "submission_status": "answered",
                    "unit": 1,
                    "max_score": 7,
                    "checkpoint_results": checkpoint_results,
                    "root_error_impacts": [],
                    "caps_applied": [],
                    "initial_score": 7,
                    "final_score": 7,
                    "review": {
                        "high_score_challenge": "已检查是否遗漏致命缺口。",
                        "low_score_credit_check": "已检查替代方法所得分。",
                        "double_count_check": "已检查重复计分和扣分。",
                        "band_and_total_check": "已检查分档和总分。",
                        "score_changed": False,
                        "change_reason": "",
                    },
                }
            ],
        },
    }
    for filename, payload in artifacts.items():
        (internal_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    return {
        "service_tier": "summary_report",
        "grading_standard": "imo",
        "resolved_league_scope": None,
        "title": "数学竞赛题批改",
        "total_score": 7,
        "max_score": 7,
        "problems": [
            {
                "label": "第 1 题",
                "score": 7,
                "max_score": 7,
                "verdict": "关键构造、推导和结论均成立。",
                "issues": [],
            }
        ],
    }


def _validate(job_dir: Path, *, tier: str, grading: dict[str, object]) -> None:
    validate_internal_analysis(
        job_dir,
        profile={"service_tier": tier, "grading_standard": "imo"},
        grading=grading,
        input_page_count=1,
    )


def test_summary_no_longer_accepts_the_annotated_five_file_contract(
    tmp_path: Path,
) -> None:
    grading = _write_internal_artifacts(tmp_path, include_locations=False)
    with pytest.raises(
        InternalAnalysisValidationError,
        match="summary-analysis.json",
    ):
        _validate(tmp_path, tier="summary_report", grading=grading)


def test_annotated_still_requires_source_locations(tmp_path: Path) -> None:
    grading = _write_internal_artifacts(tmp_path, include_locations=False)
    with pytest.raises(
        InternalAnalysisValidationError,
        match="位置必须是非空文字|页码必须是整数",
    ):
        _validate(tmp_path, tier="annotated_review", grading=grading)


def test_annotated_accepts_positioned_evidence(tmp_path: Path) -> None:
    grading = _write_internal_artifacts(tmp_path, include_locations=True)
    _validate(tmp_path, tier="annotated_review", grading=grading)


def test_summary_prompt_forbids_location_work_but_keeps_mathematical_checks() -> None:
    prompt = _build_grading_prompt(
        profile={"service_tier": "summary_report"},
        has_instructions=False,
        has_reference=False,
    )

    assert "简明报告及内部证据均不做页内定位" in prompt
    assert "summary-analysis.json 和 summary-audit.json" in prompt
    assert "不得生成 annotated 流程的五份内部文件" in prompt
    assert "完整核验决定分数的依据" in prompt
    assert "疑似笔误须按字面写法" in prompt
    assert "发现一个错误后仍须检查" in prompt
    assert "扣分只作用于实际受影响的评分点" in prompt
    assert "冻结 summary-audit" in prompt
    for annotated_artifact in (
        "marking-scheme",
        "proof-map",
        "verification",
        "score-audit",
    ):
        assert annotated_artifact not in prompt


def test_annotated_prompt_retains_selective_location_work() -> None:
    prompt = _build_grading_prompt(
        profile={"service_tier": "annotated_review"},
        has_instructions=False,
        has_reference=False,
    )

    assert "真正需要核对的位置" in prompt
    assert "内部证据均不做页内定位" not in prompt
    assert "完整依赖链" in prompt
    assert "具体评分点 ID 依赖" in prompt


@pytest.mark.parametrize("tier", ["summary_report", "annotated_review"])
def test_prompt_keeps_validation_and_two_corrections_inside_one_codex(
    tier: str,
) -> None:
    prompt = _build_grading_prompt(
        profile={"service_tier": tier},
        has_instructions=False,
        has_reference=False,
    )

    assert "python -I -m worker.runtime.legacy.validate_workspace --job-dir ." in prompt
    assert "失败时利用当前上下文修正并重跑" in prompt
    assert "最多修正两轮" in prompt
    assert "校验通过后不得再修改任何输出" in prompt
