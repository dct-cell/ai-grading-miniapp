from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SummaryAnalysisValidationError(ValueError):
    pass


ANALYSIS_VERSION = 1
SUBMISSION_STATUSES = {"answered", "partial", "missing"}
EVIDENCE_VERDICTS = {"valid", "invalid", "unsupported", "ambiguous"}
REPAIR_SCOPES = {"local", "global"}
RUBRIC_SOURCES = {"specific", "profile"}
FORBIDDEN_LOCATION_KEYS = {"page", "location", "source_quote", "bbox", "bboxes"}
REVIEW_KEYS = {
    "typo_checked",
    "independent_credit_checked",
    "double_count_checked",
    "band_and_total_checked",
}


def _fail(message: str) -> None:
    raise SummaryAnalysisValidationError(message)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"缺少简明内部产物：{label}。")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SummaryAnalysisValidationError(
            f"简明内部产物无法解析：{label}。"
        ) from exc
    if not isinstance(payload, dict):
        _fail(f"{label}必须是 JSON 对象。")
    return payload


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label}必须是对象。")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label}必须是数组。")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(f"{label}必须是非空文字。")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label}必须是整数。")
    number = float(value)
    if not number.is_integer():
        _fail(f"{label}必须是整数。")
    return int(number)


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _fail(f"{label}字段不完整或包含多余内容。")


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        _fail(f"{label}存在重复标识。")


def _header(payload: dict[str, Any], label: str, standard: str, scope: str | None) -> None:
    if payload.get("analysis_version") != ANALYSIS_VERSION:
        _fail(f"{label}的版本不受支持。")
    if payload.get("grading_standard") != standard:
        _fail(f"{label}与所选评分标准不一致。")
    if payload.get("resolved_league_scope") != scope:
        _fail(f"{label}与最终联赛范围不一致。")


def _reject_locations(value: Any, label: str = "简明内部产物") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_LOCATION_KEYS:
                _fail(f"{label}不得包含位置字段：{key}。")
            _reject_locations(item, label)
    elif isinstance(value, list):
        for item in value:
            _reject_locations(item, label)


def _expected_maxima(
    profile: dict[str, Any], scope: str | None, problem_count: int
) -> tuple[list[int], int]:
    standard = profile.get("grading_standard")
    if standard == "imo":
        return [7] * problem_count, 1
    if standard == "cmo":
        return [21] * problem_count, 3
    if standard != "league_second_round":
        _fail("简明校验收到未知评分标准。")
    if scope == "full_paper":
        if problem_count != 4 or profile.get("league_problem_number") is not None:
            _fail("联赛整卷必须包含四题且不能指定单题题号。")
        return [40, 40, 50, 50], 10
    if scope != "problem_set":
        _fail("简明联赛评分缺少有效范围。")
    problem_number = profile.get("league_problem_number")
    if problem_number is not None:
        if problem_count != 1 or problem_number not in {1, 2, 3, 4}:
            _fail("指定联赛题号时必须只批改一道有效题目。")
        return [50 if problem_number in {3, 4} else 40], 10
    return [40] * problem_count, 10


def _public_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "title": issue["title"],
            "reason": issue["reason"],
            "deduction": issue["deduction"],
        }
        for issue in issues
    ]


def validate_summary_analysis(
    job_dir: Path,
    *,
    profile: dict[str, Any],
    grading: dict[str, Any],
    input_page_count: int,
) -> None:
    """Validate compact summary evidence without reconstructing a proof graph."""

    internal = job_dir / "output" / "internal"
    analysis = _read_object(internal / "summary-analysis.json", "summary-analysis.json")
    audit = _read_object(internal / "summary-audit.json", "summary-audit.json")
    standard = profile.get("grading_standard")
    scope = grading.get("resolved_league_scope")
    if profile.get("service_tier") != "summary_report":
        _fail("简明校验只能用于 summary_report。")
    if standard not in {"imo", "cmo", "league_second_round"}:
        _fail("简明校验收到未知评分标准。")

    _reject_locations(analysis)
    _reject_locations(audit)
    _exact_keys(
        analysis,
        {
            "analysis_version",
            "grading_standard",
            "resolved_league_scope",
            "reference_use",
            "problems",
        },
        "summary-analysis.json",
    )
    _exact_keys(
        audit,
        {
            "analysis_version",
            "grading_standard",
            "resolved_league_scope",
            "total_score",
            "max_score",
            "problems",
        },
        "summary-audit.json",
    )
    _header(analysis, "summary-analysis.json", standard, scope)
    _header(audit, "summary-audit.json", standard, scope)

    reference_use = _object(analysis.get("reference_use"), "参考材料使用记录")
    _exact_keys(reference_use, {"status", "note"}, "参考材料使用记录")
    reference_status = reference_use.get("status")
    has_reference = (job_dir / "input" / "reference.pdf").is_file()
    if reference_status not in {"absent", "used", "rejected"}:
        _fail("参考材料使用状态无效。")
    if has_reference == (reference_status == "absent"):
        _fail("参考材料使用状态与输入文件不一致。")
    _text(reference_use.get("note"), "参考材料使用说明")

    public_problems = _list(grading.get("problems"), "最终评分题目")
    analysis_problems = _list(analysis.get("problems"), "简明分析题目")
    audit_problems = _list(audit.get("problems"), "简明复核题目")
    if not analysis_problems or len(analysis_problems) != len(public_problems):
        _fail("简明分析数量与最终评分不一致。")
    if len(audit_problems) != len(public_problems):
        _fail("简明复核数量与最终评分不一致。")
    maxima, unit = _expected_maxima(profile, scope, len(public_problems))

    problem_ids: list[str] = []
    covered_pages: set[int] = set()
    analysis_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(analysis_problems, start=1):
        problem = _object(raw, f"简明分析第 {index} 题")
        _exact_keys(
            problem,
            {
                "id",
                "label",
                "pages",
                "submission_status",
                "target",
                "student_route",
                "interpretations",
                "evidence",
                "root_issues",
            },
            f"简明分析第 {index} 题",
        )
        problem_id = _text(problem.get("id"), "简明分析题目标识")
        problem_ids.append(problem_id)
        _text(problem.get("label"), f"简明分析 {problem_id} 标签")
        pages = _list(problem.get("pages"), f"简明分析 {problem_id} 页码")
        if not pages:
            _fail(f"简明分析 {problem_id} 缺少页码。")
        local_pages: set[int] = set()
        for value in pages:
            page = _integer(value, f"简明分析 {problem_id} 页码")
            if page < 1 or page > input_page_count or page in local_pages:
                _fail(f"简明分析 {problem_id} 包含无效页码。")
            local_pages.add(page)
            covered_pages.add(page)
        status = problem.get("submission_status")
        if status not in SUBMISSION_STATUSES:
            _fail(f"简明分析 {problem_id} 作答状态无效。")
        _text(problem.get("target"), f"简明分析 {problem_id} 目标")
        _text(problem.get("student_route"), f"简明分析 {problem_id} 作答路线")

        for raw_interpretation in _list(
            problem.get("interpretations"), f"简明分析 {problem_id} 解释"
        ):
            interpretation = _object(raw_interpretation, "简明解释")
            _exact_keys(interpretation, {"reading", "score_relevant"}, "简明解释")
            _text(interpretation.get("reading"), "简明解释内容")
            if not isinstance(interpretation.get("score_relevant"), bool):
                _fail("简明解释必须说明是否影响分数。")

        evidence_ids: list[str] = []
        evidence_by_id: dict[str, dict[str, Any]] = {}
        for raw_evidence in _list(problem.get("evidence"), f"简明分析 {problem_id} 证据"):
            evidence = _object(raw_evidence, "简明证据")
            _exact_keys(evidence, {"id", "claim", "verdict", "reason"}, "简明证据")
            evidence_id = _text(evidence.get("id"), "简明证据标识")
            evidence_ids.append(evidence_id)
            _text(evidence.get("claim"), f"简明证据 {evidence_id} 内容")
            if evidence.get("verdict") not in EVIDENCE_VERDICTS:
                _fail(f"简明证据 {evidence_id} 结论无效。")
            _text(evidence.get("reason"), f"简明证据 {evidence_id} 理由")
            evidence_by_id[evidence_id] = evidence
        _unique(evidence_ids, f"简明分析 {problem_id} 证据")

        issue_ids: list[str] = []
        root_issues: dict[str, dict[str, Any]] = {}
        covered_nonvalid_evidence: list[str] = []
        for raw_issue in _list(
            problem.get("root_issues"), f"简明分析 {problem_id} 根本问题"
        ):
            issue = _object(raw_issue, "简明根本问题")
            _exact_keys(
                issue,
                {"id", "description", "repair_scope", "evidence_ids"},
                "简明根本问题",
            )
            issue_id = _text(issue.get("id"), "简明根本问题标识")
            issue_ids.append(issue_id)
            _text(issue.get("description"), f"简明根本问题 {issue_id} 说明")
            if issue.get("repair_scope") not in REPAIR_SCOPES:
                _fail(f"简明根本问题 {issue_id} 修补范围无效。")
            affected = [
                _text(value, f"简明根本问题 {issue_id} 证据")
                for value in _list(
                    issue.get("evidence_ids"),
                    f"简明根本问题 {issue_id} 证据",
                )
            ]
            _unique(affected, f"简明根本问题 {issue_id} 证据")
            for evidence_id in affected:
                if evidence_id not in evidence_by_id:
                    _fail(f"简明根本问题 {issue_id} 引用了未知证据。")
                if evidence_by_id[evidence_id]["verdict"] == "valid":
                    _fail(f"简明根本问题 {issue_id} 不应引用有效证据。")
                covered_nonvalid_evidence.append(evidence_id)
            root_issues[issue_id] = issue
        _unique(issue_ids, f"简明分析 {problem_id} 根本问题")
        _unique(covered_nonvalid_evidence, f"简明分析 {problem_id} 问题证据")
        if len(issue_ids) > 3:
            _fail(f"简明分析 {problem_id} 最多保留三个独立根本问题。")
        expected_nonvalid = {
            evidence_id
            for evidence_id, evidence in evidence_by_id.items()
            if evidence["verdict"] != "valid"
        }
        if set(covered_nonvalid_evidence) != expected_nonvalid:
            _fail(f"简明分析 {problem_id} 没有覆盖全部非有效证据。")
        if status == "missing" and evidence_ids:
            _fail(f"未作答题目 {problem_id} 不应包含证据。")
        analysis_by_id[problem_id] = {
            "problem": problem,
            "evidence": evidence_by_id,
            "root_issues": root_issues,
        }

    _unique(problem_ids, "简明分析题目标识")
    if covered_pages != set(range(1, input_page_count + 1)):
        _fail("简明分析没有覆盖全部原稿页。")

    final_scores: list[int] = []
    for index, (raw_audit, raw_public, expected_max) in enumerate(
        zip(audit_problems, public_problems, maxima, strict=True), start=1
    ):
        audit_problem = _object(raw_audit, f"简明复核第 {index} 题")
        public_problem = _object(raw_public, f"最终评分第 {index} 题")
        _exact_keys(
            audit_problem,
            {
                "problem_id",
                "submission_status",
                "score",
                "max_score",
                "rubric_source",
                "rubric_reference",
                "credit_evidence_ids",
                "verdict",
                "issues",
                "review",
            },
            f"简明复核第 {index} 题",
        )
        problem_id = _text(audit_problem.get("problem_id"), "简明复核题目标识")
        if problem_id != problem_ids[index - 1]:
            _fail("简明复核题目顺序与分析不一致。")
        source = analysis_by_id[problem_id]
        analysis_problem = source["problem"]
        if audit_problem.get("submission_status") != analysis_problem["submission_status"]:
            _fail(f"简明复核 {problem_id} 作答状态不一致。")
        if public_problem.get("label") != analysis_problem["label"]:
            _fail(f"简明复核 {problem_id} 标签与最终评分不一致。")
        maximum = _integer(audit_problem.get("max_score"), f"简明复核 {problem_id} 满分")
        score = _integer(audit_problem.get("score"), f"简明复核 {problem_id} 得分")
        if maximum != expected_max or public_problem.get("max_score") != maximum:
            _fail(f"简明复核 {problem_id} 满分不符合所选标准。")
        if score < 0 or score > maximum or score % unit:
            _fail(f"简明复核 {problem_id} 得分不符合所选分档。")
        if public_problem.get("score") != score:
            _fail(f"简明复核 {problem_id} 得分与最终评分不一致。")
        rubric_source = audit_problem.get("rubric_source")
        rubric_reference = audit_problem.get("rubric_reference")
        if rubric_source not in RUBRIC_SOURCES:
            _fail(f"简明复核 {problem_id} 评分来源无效。")
        if rubric_source == "profile":
            if rubric_reference is not None:
                _fail(f"简明复核 {problem_id} 默认评分不应附带专属标准来源。")
        else:
            reference = _object(
                rubric_reference, f"简明复核 {problem_id} 专属评分标准来源"
            )
            _exact_keys(
                reference,
                {"source", "description"},
                f"简明复核 {problem_id} 专属评分标准来源",
            )
            source_kind = reference.get("source")
            if source_kind not in {"reference_pdf", "web"}:
                _fail(f"简明复核 {problem_id} 专属评分标准来源无效。")
            _text(
                reference.get("description"),
                f"简明复核 {problem_id} 专属评分标准说明",
            )
            if source_kind == "reference_pdf" and (
                not has_reference or reference_status != "used"
            ):
                _fail(f"简明复核 {problem_id} 未实际采用参考 PDF 评分标准。")
            if source_kind == "web":
                try:
                    instructions = (job_dir / "input" / "instructions.txt").read_text(
                        encoding="utf-8"
                    )
                except (OSError, UnicodeError):
                    instructions = ""
                if not instructions.strip():
                    _fail(f"简明复核 {problem_id} 未启用网页评分标准核验。")
        if (
            standard == "league_second_round"
            and rubric_source == "profile"
            and any(
                issue["repair_scope"] == "global"
                for issue in source["root_issues"].values()
            )
            and score > 10
        ):
            _fail(f"简明复核 {problem_id} 有不可局部修补缺口时不得超过 10 分。")

        credited = _list(
            audit_problem.get("credit_evidence_ids"),
            f"简明复核 {problem_id} 得分证据",
        )
        credited_ids = [
            _text(value, f"简明复核 {problem_id} 得分证据") for value in credited
        ]
        _unique(credited_ids, f"简明复核 {problem_id} 得分证据")
        evidence_by_id = source["evidence"]
        if any(value not in evidence_by_id for value in credited_ids):
            _fail(f"简明复核 {problem_id} 引用了未知得分证据。")
        if any(evidence_by_id[value]["verdict"] != "valid" for value in credited_ids):
            _fail(f"简明复核 {problem_id} 使用了未核验为有效的证据。")
        if score > 0 and not credited_ids:
            _fail(f"简明复核 {problem_id} 得分缺少有效证据。")
        if analysis_problem["submission_status"] == "missing" and score != 0:
            _fail(f"未作答题目 {problem_id} 必须为 0 分。")

        issues: list[dict[str, Any]] = []
        seen_issue_ids: list[str] = []
        for raw_issue in _list(audit_problem.get("issues"), f"简明复核 {problem_id} 问题"):
            issue = _object(raw_issue, "简明复核问题")
            _exact_keys(
                issue,
                {"issue_id", "title", "reason", "deduction"},
                "简明复核问题",
            )
            issue_id = _text(issue.get("issue_id"), "简明复核问题标识")
            seen_issue_ids.append(issue_id)
            if issue_id not in source["root_issues"]:
                _fail(f"简明复核 {problem_id} 引用了未知根本问题。")
            _text(issue.get("title"), f"简明复核问题 {issue_id} 标题")
            _text(issue.get("reason"), f"简明复核问题 {issue_id} 理由")
            deduction = _integer(issue.get("deduction"), f"简明复核问题 {issue_id} 扣分")
            if deduction <= 0 or deduction % unit:
                _fail(f"简明复核问题 {issue_id} 扣分不符合所选分档。")
            issues.append(issue)
        _unique(seen_issue_ids, f"简明复核 {problem_id} 问题")
        if len(issues) > 3:
            _fail(f"简明复核 {problem_id} 最多保留三个根本问题。")
        if set(seen_issue_ids) != set(source["root_issues"]):
            _fail(f"简明复核 {problem_id} 没有覆盖全部根本问题。")
        if sum(issue["deduction"] for issue in issues) != maximum - score:
            _fail(f"简明复核 {problem_id} 扣分与得分不一致。")
        if score == maximum and issues:
            _fail(f"简明复核 {problem_id} 满分时不应包含问题。")

        verdict = _text(audit_problem.get("verdict"), f"简明复核 {problem_id} 判定")
        if public_problem.get("verdict") != verdict:
            _fail(f"简明复核 {problem_id} 判定与最终评分不一致。")
        if public_problem.get("issues") != _public_issues(issues):
            _fail(f"简明复核 {problem_id} 问题与最终评分不一致。")
        review = _object(audit_problem.get("review"), f"简明复核 {problem_id} 自查")
        _exact_keys(review, REVIEW_KEYS, f"简明复核 {problem_id} 自查")
        if any(review.get(key) is not True for key in REVIEW_KEYS):
            _fail(f"简明复核 {problem_id} 尚未完成全部自查。")
        final_scores.append(score)

    audit_total = _integer(audit.get("total_score"), "简明复核总分")
    audit_max = _integer(audit.get("max_score"), "简明复核总满分")
    public_total = _integer(grading.get("total_score"), "最终总分")
    public_max = _integer(grading.get("max_score"), "最终总满分")
    if (
        audit_total != sum(final_scores)
        or audit_max != sum(maxima)
        or audit_total != public_total
        or audit_max != public_max
    ):
        _fail("简明复核总分与最终评分不一致。")
