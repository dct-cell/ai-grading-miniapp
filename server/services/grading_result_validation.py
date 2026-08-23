"""Deterministic validation for paid grading artefacts.

The Worker is trusted to perform mathematical reasoning, but it is not trusted
to choose the paid service tier, scoring system or output contract.  This
module re-checks those snapshots immediately before delivery so a stale,
misconfigured or compromised Worker cannot exchange one product for another.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from server.domain.service_tiers import ANNOTATED_REVIEW, SUMMARY_REPORT


class GradingResultInvalid(ValueError):
    """The staged result does not match the immutable paid order."""


def _integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GradingResultInvalid(f"{name} 必须是非负整数。")
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GradingResultInvalid(f"{name} 不能为空。")
    return value


def _check_score_band(standard: str, score: int, maximum: int) -> None:
    if score > maximum:
        raise GradingResultInvalid("分题得分不能超过满分。")
    if standard == "imo":
        if maximum != 7:
            raise GradingResultInvalid("IMO 每题必须使用 7 分制。")
    elif standard == "cmo":
        if maximum != 21 or score % 3:
            raise GradingResultInvalid("CMO 每题必须使用 21 分制且得分为 3 的倍数。")
    elif standard == "league_second_round":
        if maximum not in {40, 50} or score % 10:
            raise GradingResultInvalid("联赛二试必须使用 40/50 分满分和 10 分档。")
    else:
        raise GradingResultInvalid("评分标准未知。")


def _validate_problems(payload: dict[str, Any], standard: str) -> tuple[int, int]:
    problems = payload.get("problems")
    if not isinstance(problems, list) or not problems:
        raise GradingResultInvalid("结果必须包含至少一道题。")
    labels: set[str] = set()
    total = maximum_total = 0
    for item in problems:
        if not isinstance(item, dict):
            raise GradingResultInvalid("分题结果格式无效。")
        label = _nonempty(item.get("label"), "题号")
        if label in labels:
            raise GradingResultInvalid("每道题必须且只能出现一次。")
        labels.add(label)
        score = _integer(item.get("score"), f"{label} 得分")
        maximum = _integer(item.get("max_score"), f"{label} 满分")
        _check_score_band(standard, score, maximum)
        total += score
        maximum_total += maximum
    return total, maximum_total


def _validate_summary(payload: dict[str, Any]) -> None:
    if "pages" in payload or "findings" in payload:
        raise GradingResultInvalid("简明评分不得包含公开页内标注。")
    if "overall_summary" in payload:
        raise GradingResultInvalid("简明评分不得包含总体判断。")
    for problem in payload["problems"]:
        if "suggestion" in problem:
            raise GradingResultInvalid("简明评分不得包含独立建议。")
        _nonempty(problem.get("verdict"), "分题判断")
        issues = problem.get("issues")
        if not isinstance(issues, list) or len(issues) > 3:
            raise GradingResultInvalid("每题主要问题必须为 0–3 条。")
        deductions = 0
        for issue in issues:
            if not isinstance(issue, dict):
                raise GradingResultInvalid("主要问题格式无效。")
            _nonempty(issue.get("title"), "问题标题")
            _nonempty(issue.get("reason"), "问题原因")
            deductions += _integer(issue.get("deduction"), "扣分")
        score_gap = problem["max_score"] - problem["score"]
        if score_gap == 0 and issues:
            raise GradingResultInvalid("满分题不得列出扣分问题。")
        if deductions != score_gap:
            raise GradingResultInvalid("主要问题扣分必须与该题失分一致。")


def _validate_annotated(payload: dict[str, Any], source_page_count: int) -> None:
    pages = payload.get("pages")
    if not isinstance(pages, list) or len(pages) != source_page_count:
        raise GradingResultInvalid("逐页精批必须为答卷的每一页生成公开结果。")
    page_numbers = [page.get("page") for page in pages if isinstance(page, dict)]
    if page_numbers != list(range(1, source_page_count + 1)):
        raise GradingResultInvalid("逐页结果页码必须连续且与原答卷一致。")
    for page in pages:
        findings = page.get("findings")
        if not isinstance(findings, list):
            raise GradingResultInvalid("逐页批注必须是列表。")
        for finding in findings:
            if not isinstance(finding, dict):
                raise GradingResultInvalid("批注格式无效。")
            if finding.get("kind") not in {"correct", "informational", "warning", "error"}:
                raise GradingResultInvalid("批注类型无效。")


def validate_staged_result(
    *,
    json_path: Path,
    pdf_path: Path,
    service_tier: str,
    grading_standard: str,
    league_scope: str | None,
    source_page_count: int,
) -> None:
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GradingResultInvalid("评分结果 JSON 无法读取。") from error
    if not isinstance(payload, dict):
        raise GradingResultInvalid("评分结果 JSON 必须是对象。")
    if payload.get("service_tier") != service_tier:
        raise GradingResultInvalid("结果档位与已支付订单不一致。")
    if payload.get("grading_standard") != grading_standard:
        raise GradingResultInvalid("结果评分标准与订单不一致。")
    resolved_scope = payload.get("resolved_league_scope")
    if grading_standard == "league_second_round":
        if league_scope not in {"auto", "full_paper", "problem_set"}:
            raise GradingResultInvalid("联赛范围快照无效。")
        if resolved_scope not in {"full_paper", "problem_set"}:
            raise GradingResultInvalid("联赛任务必须解析为整卷或题组。")
        if league_scope != "auto" and resolved_scope != league_scope:
            raise GradingResultInvalid("联赛范围与订单快照不一致。")
    elif resolved_scope is not None:
        raise GradingResultInvalid("非联赛任务不得包含联赛范围。")

    _nonempty(payload.get("title"), "报告标题")
    total, maximum_total = _validate_problems(payload, grading_standard)
    if _integer(payload.get("total_score"), "总分") != total:
        raise GradingResultInvalid("总分必须等于各题得分之和。")
    if _integer(payload.get("max_score"), "总满分") != maximum_total:
        raise GradingResultInvalid("总满分必须等于各题满分之和。")
    if resolved_scope == "full_paper":
        maxima = [item["max_score"] for item in payload["problems"]]
        if maxima != [40, 40, 50, 50]:
            raise GradingResultInvalid("联赛二试整卷满分必须为 40/40/50/50。")

    try:
        reader = PdfReader(str(pdf_path), strict=False)
        pdf_pages = len(reader.pages)
    except Exception as error:  # pypdf exposes several parser exception types
        raise GradingResultInvalid("交付 PDF 无法读取。") from error

    if service_tier == SUMMARY_REPORT:
        _validate_summary(payload)
        if not 1 <= pdf_pages <= 20:
            raise GradingResultInvalid("简明评分报告页数异常。")
        first = reader.pages[0].mediabox
        width, height = float(first.width), float(first.height)
        if abs(width - 595.28) > 4 or abs(height - 841.89) > 4:
            raise GradingResultInvalid("简明评分必须交付 A4 竖向 PDF。")
    elif service_tier == ANNOTATED_REVIEW:
        _nonempty(payload.get("overall_summary"), "总体结论")
        _validate_annotated(payload, source_page_count)
        if pdf_pages != source_page_count + 1:
            raise GradingResultInvalid("逐页精批报告页数必须为原答卷页数加一。")
    else:
        raise GradingResultInvalid("结果档位未知。")
