from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

import anyio
from pydantic import ValidationError

from worker.runtime.contracts import RuntimeResult, TaskBundle
from worker.runtime.legacy.pdf_utils import PdfValidationError, inspect_pdf
from worker.runtime.legacy_codex import LegacyCodexRuntime, RuntimeExecutionError
from worker.runtime.workspace import (
    WorkspaceError,
    cleanup_transient_artifacts,
    prepare_workspace,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "tmp" / "local-grading"
_RUN_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class LocalGradeUsageError(ValueError):
    """Raised when local-only CLI inputs are inconsistent or unsafe."""


@dataclass(frozen=True, slots=True)
class LocalGradingPlan:
    """A frozen one-shot grading request with no Server or Worker lease."""

    workspace: Path
    bundle: TaskBundle
    runner_mode: str
    codex_bin: str
    timeout_seconds: int
    max_codex_attempts: int
    keep_transient: bool
    json_output: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grader-local",
        description=(
            "Run one olympiad grading job locally with Codex, without starting "
            "the Server, scheduler, or Worker daemon."
        ),
    )
    parser.add_argument("submission", type=Path, help="student submission PDF")
    parser.add_argument(
        "--standard",
        required=True,
        choices=("imo", "cmo", "league_second_round"),
        help="trusted grading standard",
    )
    parser.add_argument(
        "--tier",
        choices=("summary_report", "annotated_review"),
        default="annotated_review",
        help="report tier (default: annotated_review)",
    )
    parser.add_argument("--reference", type=Path, help="optional reference PDF")
    parser.add_argument(
        "--league-scope",
        choices=("auto", "full_paper", "problem_set"),
        help="League-only scope; defaults to auto for league_second_round",
    )
    parser.add_argument(
        "--league-problem-number",
        type=int,
        choices=(1, 2, 3, 4),
        help=(
            "trusted standalone League problem number; problems 3-4 use 50 "
            "points, otherwise the problem-set default is 40"
        ),
    )
    parser.add_argument(
        "--note",
        default="",
        help=(
            "optional untrusted context (non-empty text enables the runner's "
            "narrow public-source web search)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="parent directory for isolated runs (default: repository tmp/local-grading)",
    )
    parser.add_argument(
        "--run-name",
        help="optional safe directory name; an existing run is never overwritten",
    )
    parser.add_argument(
        "--codex-bin", default="codex", help="Codex CLI executable (default: codex)"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_timeout_seconds,
        default=3600,
        help="grading timeout from 60 to 7200 seconds (default: 3600)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        choices=(1, 2),
        default=1,
        help="retry a transient Codex failure at most once (default: 1 attempt)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="generate a fake full-score report without invoking Codex",
    )
    parser.add_argument(
        "--keep-transient",
        action="store_true",
        help="keep copied Skill/fonts and QA renders after a successful run",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="print the final artifact paths as JSON",
    )
    return parser


def _timeout_seconds(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    if not 60 <= value <= 7200:
        raise argparse.ArgumentTypeError("timeout must be between 60 and 7200")
    return value


def _local_path(path: Path, *, description: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    if expanded.is_symlink():
        raise LocalGradeUsageError(f"{description} cannot be a symbolic link")
    return expanded.resolve()


def _run_name(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).strftime("local-%Y%m%d-%H%M%S-%f")
    if not _RUN_NAME_PATTERN.fullmatch(value):
        raise LocalGradeUsageError(
            "run name must be 1-64 ASCII letters, digits, dots, dashes, or underscores"
        )
    return value


def build_plan(args: argparse.Namespace) -> LocalGradingPlan:
    submission = _local_path(args.submission, description="submission PDF")
    submission_info = inspect_pdf(submission)

    reference: Path | None = None
    if args.reference is not None:
        reference = _local_path(args.reference, description="reference PDF")
        inspect_pdf(reference)

    if args.standard == "league_second_round":
        league_scope = args.league_scope or "auto"
        if league_scope == "full_paper" and args.league_problem_number is not None:
            raise LocalGradeUsageError(
                "--league-problem-number cannot be used with --league-scope full_paper"
            )
    else:
        if args.league_scope is not None:
            raise LocalGradeUsageError(
                "--league-scope may only be used with --standard league_second_round"
            )
        if args.league_problem_number is not None:
            raise LocalGradeUsageError(
                "--league-problem-number may only be used with "
                "--standard league_second_round"
            )
        league_scope = None

    run_name = _run_name(args.run_name)
    output_dir = _local_path(args.output_dir, description="output directory")
    workspace = output_dir / run_name
    if workspace.exists():
        raise LocalGradeUsageError(
            f"run directory already exists; choose another --run-name: {workspace}"
        )

    bundle = TaskBundle(
        job_id=run_name,
        order_id="local-simulation",
        round_number=1,
        service_tier=args.tier,
        grading_standard=args.standard,
        league_scope=league_scope,
        league_problem_number=args.league_problem_number,
        source_pdf=str(submission),
        reference_pdf=str(reference) if reference is not None else None,
        page_count=submission_info.page_count,
        note=args.note,
    )
    return LocalGradingPlan(
        workspace=workspace,
        bundle=bundle,
        runner_mode="demo" if args.demo else "real",
        codex_bin=args.codex_bin,
        timeout_seconds=args.timeout_seconds,
        max_codex_attempts=args.max_attempts,
        keep_transient=args.keep_transient,
        json_output=args.json_output,
    )


def _require_runtime_tools(plan: LocalGradingPlan) -> None:
    if shutil.which("xelatex") is None:
        raise LocalGradeUsageError("xelatex is not on PATH")
    if plan.runner_mode == "real" and not (
        shutil.which(plan.codex_bin) or Path(plan.codex_bin).is_file()
    ):
        raise LocalGradeUsageError(f"Codex CLI is not available: {plan.codex_bin}")


async def run_plan(plan: LocalGradingPlan) -> RuntimeResult:
    _require_runtime_tools(plan)
    prepare_workspace(plan.workspace, plan.bundle)

    async def progress(stage: str) -> None:
        if not plan.json_output:
            print(f"[grading] {stage}", file=sys.stderr, flush=True)

    runtime = LegacyCodexRuntime(
        runner_mode=plan.runner_mode,
        codex_bin=plan.codex_bin,
        timeout_seconds=plan.timeout_seconds,
        max_codex_attempts=plan.max_codex_attempts,
    )
    try:
        result = await runtime.run(plan.workspace, plan.bundle, progress)
    except BaseException:
        print(
            f"failed run retained for diagnosis: {plan.workspace}",
            file=sys.stderr,
        )
        raise

    if not plan.keep_transient:
        cleanup_transient_artifacts(plan.workspace)
    return result


def _result_payload(plan: LocalGradingPlan, result: RuntimeResult) -> dict[str, object]:
    return {
        "workspace": str(plan.workspace),
        "report_pdf": str(result.result_pdf_path),
        "grading_json": str(result.result_json_path),
        "manifest_json": str(result.manifest_path),
        "output_page_count": result.output_page_count,
        "service_tier": plan.bundle.service_tier,
        "grading_standard": plan.bundle.grading_standard,
        "league_scope": plan.bundle.league_scope,
        "league_problem_number": plan.bundle.league_problem_number,
        "demo": plan.runner_mode == "demo",
    }


def _print_result(plan: LocalGradingPlan, result: RuntimeResult) -> None:
    payload = _result_payload(plan, result)
    if plan.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if plan.runner_mode == "demo":
        print("warning: demo mode generated a fake full-score report", file=sys.stderr)
    print("grading complete")
    print(f"workspace: {payload['workspace']}")
    print(f"report PDF: {payload['report_pdf']}")
    print(f"grading JSON: {payload['grading_json']}")
    print(f"manifest JSON: {payload['manifest_json']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_plan(args)
        result = anyio.run(run_plan, plan)
    except (
        LocalGradeUsageError,
        PdfValidationError,
        WorkspaceError,
        ValidationError,
    ) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except RuntimeExecutionError as exc:
        detail = f": {exc}" if str(exc) else ""
        print(f"grading failed [{exc.code}]{detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("grading cancelled", file=sys.stderr)
        return 130

    _print_result(plan, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
