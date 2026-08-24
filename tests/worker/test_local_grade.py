from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker import local_grade
from worker.runtime.testsupport import build_minimal_pdf


def _pdf(path: Path) -> Path:
    path.write_bytes(build_minimal_pdf(page_count=1))
    return path


def _args(tmp_path: Path, *extra: str):
    submission = _pdf(tmp_path / "submission.pdf")
    return local_grade.build_parser().parse_args(
        [
            str(submission),
            "--standard",
            "imo",
            "--output-dir",
            str(tmp_path / "runs"),
            "--run-name",
            "case-1",
            *extra,
        ]
    )


def test_build_plan_defaults_to_annotated_imo(tmp_path: Path) -> None:
    plan = local_grade.build_plan(_args(tmp_path))

    assert plan.bundle.service_tier == "annotated_review"
    assert plan.bundle.grading_standard == "imo"
    assert plan.bundle.league_scope is None
    assert plan.bundle.league_problem_number is None
    assert plan.bundle.page_count == 1
    assert plan.workspace == (tmp_path / "runs" / "case-1").resolve()
    assert plan.runner_mode == "real"


def test_default_output_directory_is_inside_repository(tmp_path: Path) -> None:
    submission = _pdf(tmp_path / "submission.pdf")
    args = local_grade.build_parser().parse_args(
        [str(submission), "--standard", "imo", "--run-name", "default-output"]
    )

    plan = local_grade.build_plan(args)

    assert plan.workspace == local_grade.DEFAULT_OUTPUT_DIR / "default-output"


def test_league_defaults_to_auto_scope(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.standard = "league_second_round"

    plan = local_grade.build_plan(args)

    assert plan.bundle.league_scope == "auto"
    assert plan.bundle.league_problem_number is None


def test_league_problem_three_selects_trusted_50_point_profile(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "--league-problem-number", "3")
    args.standard = "league_second_round"

    plan = local_grade.build_plan(args)

    assert plan.bundle.league_scope == "auto"
    assert plan.bundle.league_problem_number == 3


def test_non_league_rejects_league_scope(tmp_path: Path) -> None:
    with pytest.raises(local_grade.LocalGradeUsageError, match="league-scope"):
        local_grade.build_plan(_args(tmp_path, "--league-scope", "problem_set"))


def test_non_league_rejects_league_problem_number(tmp_path: Path) -> None:
    with pytest.raises(local_grade.LocalGradeUsageError, match="league-problem-number"):
        local_grade.build_plan(_args(tmp_path, "--league-problem-number", "3"))


def test_full_paper_rejects_league_problem_number(tmp_path: Path) -> None:
    args = _args(
        tmp_path,
        "--league-scope",
        "full_paper",
        "--league-problem-number",
        "3",
    )
    args.standard = "league_second_round"

    with pytest.raises(local_grade.LocalGradeUsageError, match="full_paper"):
        local_grade.build_plan(args)


def test_rejects_existing_run_directory(tmp_path: Path) -> None:
    args = _args(tmp_path)
    (tmp_path / "runs" / "case-1").mkdir(parents=True)

    with pytest.raises(local_grade.LocalGradeUsageError, match="already exists"):
        local_grade.build_plan(args)


def test_rejects_submission_symlink(tmp_path: Path) -> None:
    target = _pdf(tmp_path / "real.pdf")
    link = tmp_path / "linked.pdf"
    link.symlink_to(target)
    args = local_grade.build_parser().parse_args(
        [str(link), "--standard", "imo"]
    )

    with pytest.raises(local_grade.LocalGradeUsageError, match="symbolic link"):
        local_grade.build_plan(args)


def test_run_plan_uses_runtime_and_cleans_successful_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = local_grade.build_plan(_args(tmp_path, "--demo"))
    calls: dict[str, object] = {}

    def fake_tools(current_plan: local_grade.LocalGradingPlan) -> None:
        calls["tools"] = current_plan.runner_mode

    def fake_prepare(workspace: Path, bundle) -> Path:
        workspace.mkdir(parents=True)
        calls["bundle"] = bundle
        return workspace

    def fake_cleanup(workspace: Path) -> None:
        calls["cleanup"] = workspace

    class FakeRuntime:
        def __init__(self, **kwargs) -> None:
            calls["runtime_kwargs"] = kwargs

        async def run(self, workspace, bundle, progress):
            await progress("analysis")
            return SimpleNamespace(
                result_pdf_path=workspace / "annotated.pdf",
                result_json_path=workspace / "grading.json",
                manifest_path=workspace / "manifest.json",
                output_page_count=2,
            )

    monkeypatch.setattr(local_grade, "_require_runtime_tools", fake_tools)
    monkeypatch.setattr(local_grade, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(local_grade, "cleanup_transient_artifacts", fake_cleanup)
    monkeypatch.setattr(local_grade, "LegacyCodexRuntime", FakeRuntime)

    result = __import__("anyio").run(local_grade.run_plan, plan)

    assert result.output_page_count == 2
    assert calls["tools"] == "demo"
    assert calls["cleanup"] == plan.workspace
    assert calls["runtime_kwargs"] == {
        "runner_mode": "demo",
        "codex_bin": "codex",
        "timeout_seconds": 3600,
        "max_codex_attempts": 1,
    }


def test_main_json_output_is_machine_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = local_grade.build_plan(_args(tmp_path, "--json"))
    result = SimpleNamespace(
        result_pdf_path=plan.workspace / "annotated.pdf",
        result_json_path=plan.workspace / "grading.json",
        manifest_path=plan.workspace / "manifest.json",
        output_page_count=2,
    )
    monkeypatch.setattr(local_grade, "build_plan", lambda args: plan)

    async def fake_run_plan(current_plan):
        assert current_plan is plan
        return result

    monkeypatch.setattr(local_grade, "run_plan", fake_run_plan)

    code = local_grade.main(
        [str(tmp_path / "ignored.pdf"), "--standard", "imo"]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report_pdf"].endswith("annotated.pdf")
    assert payload["service_tier"] == "annotated_review"
    assert payload["demo"] is False
