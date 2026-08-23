from __future__ import annotations

from typing import Any


GRADING_STANDARD_LABELS = {
    "league_second_round": "联赛二试",
    "cmo": "CMO",
    "imo": "IMO",
}
LEAGUE_SCOPES = {"auto", "full_paper", "problem_set"}
PROFILE_VERSION = 1


class GradingProfileValidationError(ValueError):
    pass


def normalize_grading_profile(
    grading_standard: Any,
    league_scope: Any = None,
) -> dict[str, str | None]:
    standard = grading_standard.strip().lower() if isinstance(grading_standard, str) else ""
    if standard not in GRADING_STANDARD_LABELS:
        raise GradingProfileValidationError(
            "请选择联赛二试、CMO 或 IMO 评分标准。"
        )

    scope = league_scope.strip().lower() if isinstance(league_scope, str) else None
    scope = scope or None
    if standard == "league_second_round":
        scope = scope or "auto"
        if scope not in LEAGUE_SCOPES:
            raise GradingProfileValidationError(
                "联赛二试范围必须为自动识别、完整卷或单题/题组。"
            )
    elif scope is not None:
        raise GradingProfileValidationError(
            "只有联赛二试任务可以设置答卷范围。"
        )

    return {
        "grading_standard": standard,
        "grading_standard_label": GRADING_STANDARD_LABELS[standard],
        "league_scope": scope,
    }


def profile_from_status(status: dict[str, Any]) -> dict[str, str | None]:
    return normalize_grading_profile(
        status.get("grading_standard"), status.get("league_scope")
    )
