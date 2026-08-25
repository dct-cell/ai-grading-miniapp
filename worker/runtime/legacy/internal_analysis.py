from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class InternalAnalysisValidationError(ValueError):
    pass


ANALYSIS_VERSION = 1
REQUIRED_ARTIFACTS = {
    "problem-analysis": "problem-analysis.json",
    "marking-scheme": "marking-scheme.json",
    "proof-map": "proof-map.json",
    "verification": "verification.json",
    "score-audit": "score-audit.json",
}
SUBMISSION_STATUSES = {"answered", "partial", "missing"}
STEP_VERDICTS = {"valid", "invalid", "unsupported", "ambiguous"}
REPAIR_SCOPES = {"none", "local", "global"}


def _fail(message: str) -> None:
    raise InternalAnalysisValidationError(message)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"缺少内部产物：{label}。")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InternalAnalysisValidationError(
            f"内部产物无法解析：{label}。"
        ) from exc
    if not isinstance(payload, dict):
        _fail(f"内部产物必须是 JSON 对象：{label}。")
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


def _string_list(value: Any, label: str) -> list[str]:
    values = _list(value, label)
    result: list[str] = []
    for index, item in enumerate(values, start=1):
        result.append(_text(item, f"{label}第 {index} 项"))
    return result


def _ensure_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        _fail(f"{label}存在重复标识。")


def _ensure_exact_order(
    items: list[Any], expected_ids: list[str], label: str
) -> list[dict[str, Any]]:
    objects = [_object(item, f"{label}第 {index} 项") for index, item in enumerate(items, 1)]
    actual = [_text(item.get("problem_id"), f"{label}题目标识") for item in objects]
    if actual != expected_ids:
        _fail(f"{label}题目顺序与题目分析不一致。")
    return objects


def _validate_dependencies(
    dependencies: dict[str, list[str]], known_ids: set[str], label: str
) -> None:
    for item_id, refs in dependencies.items():
        for ref in refs:
            if ref not in known_ids or ref == item_id:
                _fail(f"{label}包含无效依赖：{item_id} -> {ref}。")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            _fail(f"{label}包含循环依赖。")
        if item_id in visited:
            return
        visiting.add(item_id)
        for ref in dependencies.get(item_id, []):
            visit(ref)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in known_ids:
        visit(item_id)


def _validate_scheme_dependencies(
    dependencies: dict[str, list[str]],
    checkpoint_slots: dict[str, str],
    label: str,
) -> None:
    """Validate checkpoint dependencies expressed by checkpoint or slot ID."""

    known_checkpoints = set(checkpoint_slots)
    known_slots = set(checkpoint_slots.values())
    slot_dependencies: dict[str, list[str]] = {slot: [] for slot in known_slots}
    for checkpoint_id, refs in dependencies.items():
        own_slot = checkpoint_slots[checkpoint_id]
        for ref in refs:
            if ref in known_checkpoints:
                target_slot = checkpoint_slots[ref]
            elif ref in known_slots:
                target_slot = ref
            else:
                _fail(f"{label}包含无效依赖：{checkpoint_id} -> {ref}。")
            if target_slot == own_slot:
                _fail(f"{label}包含自我依赖：{checkpoint_id} -> {ref}。")
            if target_slot not in slot_dependencies[own_slot]:
                slot_dependencies[own_slot].append(target_slot)
    _validate_dependencies(slot_dependencies, known_slots, label)


def _validate_header(
    payload: dict[str, Any],
    label: str,
    standard: str,
    resolved_scope: str | None,
) -> None:
    if payload.get("analysis_version") != ANALYSIS_VERSION:
        _fail(f"{label}的版本不受支持。")
    if payload.get("grading_standard") != standard:
        _fail(f"{label}与所选评分标准不一致。")
    if payload.get("resolved_league_scope") != resolved_scope:
        _fail(f"{label}与最终联赛范围不一致。")


def _expected_unit(standard: str) -> int:
    return {"imo": 1, "cmo": 3, "league_second_round": 10}[standard]


def validate_internal_analysis(
    job_dir: Path,
    *,
    profile: dict[str, Any],
    grading: dict[str, Any],
    input_page_count: int,
) -> None:
    """Validate the staged reasoning artifacts without judging mathematics.

    This checks provenance, references, allowed scoring bands and arithmetic. It
    intentionally does not require or attempt to validate a full reference proof.
    """

    service_tier = profile.get("service_tier")
    standard = profile.get("grading_standard")
    resolved_scope = grading.get("resolved_league_scope")
    if service_tier not in {"summary_report", "annotated_review"}:
        _fail("内部校验收到未知服务档位。")
    if standard not in {"imo", "cmo", "league_second_round"}:
        _fail("内部校验收到未知评分标准。")
    if service_tier == "summary_report":
        from .summary_analysis import (
            SummaryAnalysisValidationError,
            validate_summary_analysis,
        )

        try:
            validate_summary_analysis(
                job_dir,
                profile=profile,
                grading=grading,
                input_page_count=input_page_count,
            )
        except SummaryAnalysisValidationError as exc:
            raise InternalAnalysisValidationError(str(exc)) from exc
        return

    internal_dir = job_dir / "output" / "internal"
    artifacts = {
        key: _read_object(internal_dir / filename, filename)
        for key, filename in REQUIRED_ARTIFACTS.items()
    }
    for key, payload in artifacts.items():
        _validate_header(payload, REQUIRED_ARTIFACTS[key], standard, resolved_scope)

    grading_problems = _list(grading.get("problems"), "最终评分题目")
    analysis_problems = _list(
        artifacts["problem-analysis"].get("problems"), "题目分析"
    )
    if not analysis_problems or len(analysis_problems) != len(grading_problems):
        _fail("题目分析数量与最终评分不一致。")

    problem_ids: list[str] = []
    submission_statuses: dict[str, str] = {}
    covered_analysis_pages: set[int] = set()
    for index, raw_problem in enumerate(analysis_problems, start=1):
        problem = _object(raw_problem, f"题目分析第 {index} 题")
        problem_id = _text(problem.get("id"), f"题目分析第 {index} 题标识")
        problem_ids.append(problem_id)
        _text(problem.get("label"), f"题目分析 {problem_id} 标签")
        pages = _list(problem.get("pages"), f"题目分析 {problem_id} 页码")
        if not pages:
            _fail(f"题目分析 {problem_id} 缺少页码。")
        seen_pages: set[int] = set()
        for page_value in pages:
            page = _integer(page_value, f"题目分析 {problem_id} 页码")
            if page < 1 or page > input_page_count or page in seen_pages:
                _fail(f"题目分析 {problem_id} 包含无效页码。")
            seen_pages.add(page)
            covered_analysis_pages.add(page)
        _text(problem.get("target"), f"题目分析 {problem_id} 目标")
        _string_list(problem.get("constraints"), f"题目分析 {problem_id} 条件")
        _text(problem.get("student_route"), f"题目分析 {problem_id} 作答路线")
        interpretations = _list(
            problem.get("interpretations"), f"题目分析 {problem_id} 模糊内容解释"
        )
        for note_index, raw_note in enumerate(interpretations, start=1):
            note = _object(raw_note, f"题目分析 {problem_id} 解释 {note_index}")
            if service_tier == "annotated_review":
                _text(note.get("location"), "模糊内容位置")
            elif note.get("location") is not None:
                # Older summary artifacts may contain a coarse location. Accept
                # it during rollout without requiring new jobs to produce one.
                _text(note.get("location"), "模糊内容位置")
            _text(note.get("reading"), "采用的合理解释")
            if not isinstance(note.get("score_relevant"), bool):
                _fail("模糊内容解释必须说明是否影响分数。")
        submission_status = problem.get("submission_status")
        if submission_status not in SUBMISSION_STATUSES:
            _fail(f"题目分析 {problem_id} 的作答状态无效。")
        submission_statuses[problem_id] = submission_status
    _ensure_unique(problem_ids, "题目标识")
    if covered_analysis_pages != set(range(1, input_page_count + 1)):
        _fail("题目分析没有覆盖全部原稿页。")

    scheme_problems = _ensure_exact_order(
        _list(artifacts["marking-scheme"].get("problems"), "评分表题目"),
        problem_ids,
        "评分表",
    )
    unit = _expected_unit(standard)
    schemes: dict[str, dict[str, Any]] = {}
    checkpoints_by_problem: dict[str, dict[str, dict[str, Any]]] = {}
    slots_by_problem: dict[str, set[str]] = {}
    caps_by_problem: dict[str, dict[str, int]] = {}
    deduction_slots_by_problem: dict[str, set[frozenset[str]]] = {}
    for index, (scheme, raw_grading_problem) in enumerate(
        zip(scheme_problems, grading_problems, strict=True), start=1
    ):
        problem_id = problem_ids[index - 1]
        grading_problem = _object(raw_grading_problem, f"最终评分第 {index} 题")
        expected_max = _integer(grading_problem.get("max_score"), "最终题目满分")
        if _integer(scheme.get("max_score"), f"评分表 {problem_id} 满分") != expected_max:
            _fail(f"评分表 {problem_id} 满分与最终评分不一致。")
        if _integer(scheme.get("unit"), f"评分表 {problem_id} 单位") != unit:
            _fail(f"评分表 {problem_id} 使用了错误分档。")
        expected_units = expected_max // unit
        if _integer(scheme.get("base_units"), f"评分表 {problem_id} 基础单位") != expected_units:
            _fail(f"评分表 {problem_id} 基础评分单位数量不正确。")

        checkpoints = [
            _object(item, f"评分表 {problem_id} 评分点")
            for item in _list(scheme.get("checkpoints"), f"评分表 {problem_id} 评分点")
        ]
        if not checkpoints:
            _fail(f"评分表 {problem_id} 没有评分点。")
        checkpoint_map: dict[str, dict[str, Any]] = {}
        checkpoint_slots: dict[str, str] = {}
        dependencies: dict[str, list[str]] = {}
        slots: set[str] = set()
        for checkpoint in checkpoints:
            checkpoint_id = _text(checkpoint.get("id"), "评分点标识")
            if checkpoint_id in checkpoint_map:
                _fail(f"评分表 {problem_id} 存在重复评分点。")
            slot_id = _text(checkpoint.get("slot_id"), f"评分点 {checkpoint_id} 单位标识")
            slots.add(slot_id)
            if _integer(checkpoint.get("points"), f"评分点 {checkpoint_id} 分值") != unit:
                _fail(f"评分点 {checkpoint_id} 不符合所选评分档位。")
            _text(checkpoint.get("description"), f"评分点 {checkpoint_id} 内容")
            refs = _string_list(
                checkpoint.get("depends_on"), f"评分点 {checkpoint_id} 依赖"
            )
            exclusive_group = checkpoint.get("exclusive_group")
            if exclusive_group is not None:
                _text(exclusive_group, f"评分点 {checkpoint_id} 互斥组")
            checkpoint_map[checkpoint_id] = checkpoint
            checkpoint_slots[checkpoint_id] = slot_id
            dependencies[checkpoint_id] = refs
        if len(slots) != expected_units:
            _fail(f"评分表 {problem_id} 没有覆盖全部基础评分单位。")
        _validate_scheme_dependencies(
            dependencies, checkpoint_slots, f"评分表 {problem_id}"
        )

        _list(scheme.get("zero_credit"), f"评分表 {problem_id} 零分条件")
        deductions = _list(scheme.get("deductions"), f"评分表 {problem_id} 扣分条件")
        deduction_slot_sets: set[frozenset[str]] = set()
        for deduction_index, raw_deduction in enumerate(deductions, start=1):
            deduction = _object(raw_deduction, "扣分条件")
            _text(deduction.get("id"), f"扣分条件 {deduction_index} 标识")
            withheld = _string_list(deduction.get("withheld_slots"), "扣分条件影响单位")
            if not set(withheld).issubset(slots):
                _fail(f"评分表 {problem_id} 的扣分条件引用未知单位。")
            if deduction.get("root_error_once") is not True:
                _fail(f"评分表 {problem_id} 的扣分条件必须避免重复扣分。")
            deduction_slot_sets.add(frozenset(withheld))

        cap_map: dict[str, int] = {}
        for raw_cap in _list(scheme.get("caps"), f"评分表 {problem_id} 封顶条件"):
            cap = _object(raw_cap, "封顶条件")
            cap_id = _text(cap.get("id"), "封顶条件标识")
            if cap_id in cap_map:
                _fail(f"评分表 {problem_id} 存在重复封顶条件。")
            cap_score = _integer(cap.get("max_score"), f"封顶条件 {cap_id} 分数")
            if cap_score < 0 or cap_score > expected_max or cap_score % unit:
                _fail(f"封顶条件 {cap_id} 不符合所选评分档位。")
            cap_map[cap_id] = cap_score
        schemes[problem_id] = scheme
        checkpoints_by_problem[problem_id] = checkpoint_map
        slots_by_problem[problem_id] = slots
        caps_by_problem[problem_id] = cap_map
        deduction_slots_by_problem[problem_id] = deduction_slot_sets

    proof_problems = _ensure_exact_order(
        _list(artifacts["proof-map"].get("problems"), "证明图题目"),
        problem_ids,
        "证明图",
    )
    proof_steps: dict[str, dict[str, dict[str, Any]]] = {}
    for proof_problem in proof_problems:
        problem_id = proof_problem["problem_id"]
        steps = [
            _object(item, f"证明图 {problem_id} 步骤")
            for item in _list(proof_problem.get("steps"), f"证明图 {problem_id} 步骤")
        ]
        if submission_statuses[problem_id] == "missing" and steps:
            _fail(f"未作答题目 {problem_id} 不应包含学生证明步骤。")
        if submission_statuses[problem_id] != "missing" and not steps:
            _fail(f"已作答题目 {problem_id} 缺少证明步骤。")
        step_map: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, list[str]] = {}
        for step in steps:
            step_id = _text(step.get("id"), "证明步骤标识")
            if step_id in step_map:
                _fail(f"证明图 {problem_id} 存在重复步骤。")
            if service_tier == "annotated_review":
                page = _integer(step.get("page"), f"证明步骤 {step_id} 页码")
                if page < 1 or page > input_page_count:
                    _fail(f"证明步骤 {step_id} 页码无效。")
                _text(step.get("location"), f"证明步骤 {step_id} 原稿位置")
            else:
                # Preserve compatibility with already-running summary jobs while
                # allowing the new location-free evidence contract.
                if step.get("page") is not None:
                    page = _integer(step.get("page"), f"证明步骤 {step_id} 页码")
                    if page < 1 or page > input_page_count:
                        _fail(f"证明步骤 {step_id} 页码无效。")
                if step.get("location") is not None:
                    _text(step.get("location"), f"证明步骤 {step_id} 原稿位置")
            _text(step.get("claim"), f"证明步骤 {step_id} 内容")
            _text(step.get("category"), f"证明步骤 {step_id} 类型")
            dependencies[step_id] = _string_list(
                step.get("depends_on"), f"证明步骤 {step_id} 依赖"
            )
            step_map[step_id] = step
        _validate_dependencies(dependencies, set(step_map), f"证明图 {problem_id}")
        proof_steps[problem_id] = step_map

    verification_problems = _ensure_exact_order(
        _list(artifacts["verification"].get("problems"), "核验结果题目"),
        problem_ids,
        "核验结果",
    )
    root_errors_by_problem: dict[str, dict[str, dict[str, Any]]] = {}
    verdicts_by_problem: dict[str, dict[str, str]] = {}
    for verification_problem in verification_problems:
        problem_id = verification_problem["problem_id"]
        known_steps = proof_steps[problem_id]
        root_errors: dict[str, dict[str, Any]] = {}
        for raw_error in _list(
            verification_problem.get("root_errors"), f"核验结果 {problem_id} 根本错误"
        ):
            error = _object(raw_error, "根本错误")
            error_id = _text(error.get("id"), "根本错误标识")
            if error_id in root_errors:
                _fail(f"核验结果 {problem_id} 存在重复根本错误。")
            source_step = _text(error.get("step_id"), f"根本错误 {error_id} 来源步骤")
            affected = _string_list(
                error.get("affected_step_ids"), f"根本错误 {error_id} 影响步骤"
            )
            if source_step not in known_steps or not set(affected).issubset(known_steps):
                _fail(f"根本错误 {error_id} 引用了未知证明步骤。")
            _text(error.get("description"), f"根本错误 {error_id} 说明")
            root_errors[error_id] = error

        checks = [
            _object(item, f"核验结果 {problem_id} 步骤")
            for item in _list(
                verification_problem.get("steps"), f"核验结果 {problem_id} 步骤"
            )
        ]
        checked_ids: list[str] = []
        verdicts: dict[str, str] = {}
        for check in checks:
            step_id = _text(check.get("step_id"), "核验步骤标识")
            checked_ids.append(step_id)
            if check.get("verdict") not in STEP_VERDICTS:
                _fail(f"证明步骤 {step_id} 的核验结论无效。")
            verdicts[step_id] = check["verdict"]
            _text(check.get("reason"), f"证明步骤 {step_id} 核验理由")
            _text(check.get("impact"), f"证明步骤 {step_id} 影响")
            if check.get("repair_scope") not in REPAIR_SCOPES:
                _fail(f"证明步骤 {step_id} 的修复范围无效。")
            root_error_id = check.get("root_error_id")
            if root_error_id is not None and root_error_id not in root_errors:
                _fail(f"证明步骤 {step_id} 引用了未知根本错误。")
        _ensure_unique(checked_ids, f"核验结果 {problem_id} 步骤")
        if set(checked_ids) != set(known_steps):
            _fail(f"核验结果 {problem_id} 没有逐步覆盖学生证明。")
        root_errors_by_problem[problem_id] = root_errors
        verdicts_by_problem[problem_id] = verdicts

    audit_problems = _ensure_exact_order(
        _list(artifacts["score-audit"].get("problems"), "评分复核题目"),
        problem_ids,
        "评分复核",
    )
    final_scores: list[int] = []
    final_maxima: list[int] = []
    for index, (audit_problem, raw_grading_problem) in enumerate(
        zip(audit_problems, grading_problems, strict=True), start=1
    ):
        problem_id = problem_ids[index - 1]
        grading_problem = _object(raw_grading_problem, f"最终评分第 {index} 题")
        expected_score = _integer(grading_problem.get("score"), "最终题目得分")
        expected_max = _integer(grading_problem.get("max_score"), "最终题目满分")
        if _integer(audit_problem.get("unit"), f"评分复核 {problem_id} 单位") != unit:
            _fail(f"评分复核 {problem_id} 使用了错误分档。")
        if _integer(audit_problem.get("max_score"), f"评分复核 {problem_id} 满分") != expected_max:
            _fail(f"评分复核 {problem_id} 满分不一致。")
        if audit_problem.get("submission_status") != submission_statuses[problem_id]:
            _fail(f"评分复核 {problem_id} 作答状态不一致。")

        checkpoint_map = checkpoints_by_problem[problem_id]
        known_steps = proof_steps[problem_id]
        results = [
            _object(item, f"评分复核 {problem_id} 评分点结果")
            for item in _list(
                audit_problem.get("checkpoint_results"),
                f"评分复核 {problem_id} 评分点结果",
            )
        ]
        result_ids: list[str] = []
        awarded_ids: set[str] = set()
        awarded_slots: set[str] = set()
        awarded_exclusive_groups: set[str] = set()
        calculated_score = 0
        for result in results:
            checkpoint_id = _text(result.get("checkpoint_id"), "评分点结果标识")
            result_ids.append(checkpoint_id)
            checkpoint = checkpoint_map.get(checkpoint_id)
            if checkpoint is None:
                _fail(f"评分复核 {problem_id} 引用了未知评分点。")
            awarded = result.get("awarded")
            if not isinstance(awarded, bool):
                _fail(f"评分点 {checkpoint_id} 缺少是否得分结论。")
            points = _integer(result.get("points_awarded"), f"评分点 {checkpoint_id} 得分")
            if points != (unit if awarded else 0):
                _fail(f"评分点 {checkpoint_id} 的得分与结论不一致。")
            evidence = _string_list(
                result.get("evidence_step_ids"), f"评分点 {checkpoint_id} 证据步骤"
            )
            if not set(evidence).issubset(known_steps):
                _fail(f"评分点 {checkpoint_id} 引用了未知证明步骤。")
            _text(result.get("reason"), f"评分点 {checkpoint_id} 判定理由")
            if awarded:
                if not evidence:
                    _fail(f"已授予的评分点 {checkpoint_id} 缺少学生原稿证据。")
                if any(verdicts_by_problem[problem_id][step_id] != "valid" for step_id in evidence):
                    _fail(f"评分点 {checkpoint_id} 使用了未核验为有效的证据。")
                slot_id = checkpoint["slot_id"]
                if slot_id in awarded_slots:
                    _fail(f"评分复核 {problem_id} 对同一基础单位重复计分。")
                awarded_slots.add(slot_id)
                awarded_ids.add(checkpoint_id)
                exclusive_group = checkpoint.get("exclusive_group")
                if exclusive_group is not None:
                    if exclusive_group in awarded_exclusive_groups:
                        _fail(f"评分复核 {problem_id} 同时授予了互斥评分点。")
                    awarded_exclusive_groups.add(exclusive_group)
                calculated_score += points
        _ensure_unique(result_ids, f"评分复核 {problem_id} 评分点结果")
        if set(result_ids) != set(checkpoint_map):
            _fail(f"评分复核 {problem_id} 没有覆盖全部评分点。")
        for checkpoint_id in awarded_ids:
            dependencies = checkpoint_map[checkpoint_id].get("depends_on", [])
            for dependency in dependencies:
                satisfied = (
                    dependency in awarded_ids
                    if dependency in checkpoint_map
                    else dependency in awarded_slots
                )
                if not satisfied:
                    _fail(f"评分点 {checkpoint_id} 在依赖未满足时被计分。")

        impacted_errors: set[str] = set()
        for raw_impact in _list(
            audit_problem.get("root_error_impacts"),
            f"评分复核 {problem_id} 根本错误影响",
        ):
            impact = _object(raw_impact, "根本错误影响")
            root_error_id = _text(impact.get("root_error_id"), "根本错误影响标识")
            if root_error_id in impacted_errors:
                _fail(f"评分复核 {problem_id} 对同一根本错误重复扣分。")
            if root_error_id not in root_errors_by_problem[problem_id]:
                _fail(f"评分复核 {problem_id} 引用了未知根本错误。")
            impacted_errors.add(root_error_id)
            withheld = _string_list(
                impact.get("withheld_slot_ids"), "根本错误未授予单位"
            )
            if not set(withheld).issubset(slots_by_problem[problem_id]):
                _fail(f"评分复核 {problem_id} 的根本错误引用未知单位。")
            if set(withheld) & awarded_slots:
                _fail(f"评分复核 {problem_id} 同时授予并扣除了同一单位。")
            if frozenset(withheld) not in deduction_slots_by_problem[problem_id]:
                _fail(f"评分复核 {problem_id} 的根本错误没有对应评分表扣分条件。")
            _text(impact.get("reason"), "根本错误影响说明")

        applied_caps: list[int] = []
        seen_caps: set[str] = set()
        for raw_applied in _list(
            audit_problem.get("caps_applied"), f"评分复核 {problem_id} 封顶"
        ):
            applied = _object(raw_applied, "已应用封顶")
            cap_id = _text(applied.get("cap_id"), "已应用封顶标识")
            if cap_id in seen_caps or cap_id not in caps_by_problem[problem_id]:
                _fail(f"评分复核 {problem_id} 引用了重复或未知封顶。")
            seen_caps.add(cap_id)
            cap_score = _integer(applied.get("max_score"), f"封顶 {cap_id} 分数")
            if cap_score != caps_by_problem[problem_id][cap_id]:
                _fail(f"评分复核 {problem_id} 的封顶分数不一致。")
            _text(applied.get("reason"), f"封顶 {cap_id} 理由")
            applied_caps.append(cap_score)
        if applied_caps:
            calculated_score = min(calculated_score, min(applied_caps))

        initial_score = _integer(
            audit_problem.get("initial_score"), f"评分复核 {problem_id} 初判"
        )
        final_score = _integer(
            audit_problem.get("final_score"), f"评分复核 {problem_id} 终判"
        )
        if (
            initial_score < 0
            or initial_score > expected_max
            or initial_score % unit
            or final_score != calculated_score
            or final_score != expected_score
        ):
            _fail(f"评分复核 {problem_id} 的分档或算术不一致。")
        if submission_statuses[problem_id] == "missing" and final_score != 0:
            _fail(f"未作答题目 {problem_id} 必须为 0 分。")

        review = _object(audit_problem.get("review"), f"评分复核 {problem_id} 自查")
        for key, label in (
            ("high_score_challenge", "高分质疑"),
            ("low_score_credit_check", "低分补偿检查"),
            ("double_count_check", "重复计扣检查"),
            ("band_and_total_check", "档位与总分检查"),
        ):
            _text(review.get(key), f"评分复核 {problem_id} {label}")
        if review.get("score_changed") != (initial_score != final_score):
            _fail(f"评分复核 {problem_id} 的改分标记不一致。")
        _text(
            review.get("change_reason"),
            f"评分复核 {problem_id} 改分说明",
            allow_empty=True,
        )
        final_scores.append(final_score)
        final_maxima.append(expected_max)

    audit_total = _integer(artifacts["score-audit"].get("total_score"), "复核总分")
    audit_max = _integer(artifacts["score-audit"].get("max_score"), "复核总满分")
    grading_total = _integer(grading.get("total_score"), "最终总分")
    grading_max = _integer(grading.get("max_score"), "最终总满分")
    if (
        audit_total != sum(final_scores)
        or audit_max != sum(final_maxima)
        or audit_total != grading_total
        or audit_max != grading_max
    ):
        _fail("评分复核总分与最终评分不一致。")
