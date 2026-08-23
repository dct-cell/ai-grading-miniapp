from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from worker.runtime.contracts import (
    RUNTIME_ERROR_CODES,
    RuntimeResult,
    TaskBundle,
)
from worker.runtime.legacy_codex import (
    CODE_TO_RUNTIME_ERROR,
    LegacyCodexRuntime,
    RuntimeExecutionError,
)
from worker.runtime.legacy.codex_runner import CodexRunError, _load_manifest
from worker.runtime.legacy.settings import Settings
from worker.runtime.testsupport import build_renderable_pdf


def _bundle(**overrides: Any) -> TaskBundle:
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


def _stage_workspace(tmp_path: Path, bundle: TaskBundle) -> Path:
    """Build a workspace that matches what prepare_workspace produces.

    The runtime tests start from a prepared workspace so they exercise the
    adapter, not the workspace builder (covered in test_runtime_workspace).
    Uses build_renderable_pdf so XeLaTeX can include the source PDF as an
    image in the demo report.
    """
    from worker.runtime.workspace import prepare_workspace

    # prepare_workspace expects bundle.source_pdf to point at a staged file.
    staged = tmp_path / "staged"
    staged.mkdir()
    source = staged / "source.pdf"
    source.write_bytes(build_renderable_pdf(page_count=bundle.page_count))
    bundle = bundle.model_copy(update={"source_pdf": str(source)})

    workspace = tmp_path / "ws"
    prepare_workspace(workspace, bundle)
    return workspace


@pytest.fixture
def task_bundle() -> TaskBundle:
    return _bundle()


@pytest.fixture
def imo_workspace(tmp_path: Path, task_bundle: TaskBundle) -> Path:
    return _stage_workspace(tmp_path, task_bundle)


class TestLegacyRuntimeDemoMode:
    @pytest.mark.anyio
    async def test_returns_valid_public_artifacts(
        self, imo_workspace: Path, task_bundle: TaskBundle
    ) -> None:
        runtime = LegacyCodexRuntime(runner_mode="demo")
        progress = AsyncMock()
        result = await runtime.run(imo_workspace, task_bundle, progress)

        assert isinstance(result, RuntimeResult)
        assert result.result_pdf_path == imo_workspace / "annotated.pdf"
        assert result.result_json_path == imo_workspace / "grading.json"
        assert result.manifest_path == imo_workspace / "manifest.json"
        assert result.result_pdf_path.is_file()
        assert result.result_json_path.is_file()
        assert result.manifest_path.is_file()
        # The legacy demo always produces input page count + 1 pages.
        assert result.output_page_count == task_bundle.page_count + 1

    @pytest.mark.anyio
    async def test_result_json_has_required_fields(
        self, imo_workspace: Path, task_bundle: TaskBundle
    ) -> None:
        runtime = LegacyCodexRuntime(runner_mode="demo")
        result = await runtime.run(imo_workspace, task_bundle, AsyncMock())
        grading = json.loads(result.result_json_path.read_text(encoding="utf-8"))
        for field in (
            "grading_standard",
            "resolved_league_scope",
            "title",
            "total_score",
            "max_score",
            "overall_summary",
            "problems",
            "pages",
        ):
            assert field in grading, f"grading.json missing {field}"
        assert grading["grading_standard"] == "imo"
        assert grading["resolved_league_scope"] is None

    @pytest.mark.anyio
    async def test_manifest_has_legacy_required_fields(
        self, imo_workspace: Path, task_bundle: TaskBundle
    ) -> None:
        runtime = LegacyCodexRuntime(runner_mode="demo")
        result = await runtime.run(imo_workspace, task_bundle, AsyncMock())
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        for field in (
            "output_pdf",
            "page_count",
            "summary",
            "score",
            "max_score",
            "grading_standard",
            "resolved_league_scope",
        ):
            assert field in manifest, f"manifest missing {field}"
        assert manifest["output_pdf"] == "output/annotated.pdf"
        assert manifest["page_count"] == task_bundle.page_count + 1

    @pytest.mark.anyio
    async def test_progress_callback_receives_stages(
        self, imo_workspace: Path, task_bundle: TaskBundle
    ) -> None:
        runtime = LegacyCodexRuntime(runner_mode="demo")
        progress = AsyncMock()
        await runtime.run(imo_workspace, task_bundle, progress)
        # The demo path emits at least preparing + reporting + validating.
        assert progress.await_count >= 3
        # Each call is a stage string, never raw dict.
        for call in progress.await_args_list:
            assert len(call.args) == 1
            assert isinstance(call.args[0], str)

    @pytest.mark.anyio
    async def test_checksums_match_file_bytes(
        self, imo_workspace: Path, task_bundle: TaskBundle
    ) -> None:
        import hashlib

        runtime = LegacyCodexRuntime(runner_mode="demo")
        result = await runtime.run(imo_workspace, task_bundle, AsyncMock())
        assert result.result_json_sha256 == hashlib.sha256(
            result.result_json_path.read_bytes()
        ).hexdigest()
        assert result.result_pdf_sha256 == hashlib.sha256(
            result.result_pdf_path.read_bytes()
        ).hexdigest()

    @pytest.mark.anyio
    async def test_league_full_paper_demo(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle(
            grading_standard="league_second_round",
            league_scope="full_paper",
            page_count=4,
        )
        workspace = _stage_workspace(tmp_path, bundle)
        runtime = LegacyCodexRuntime(runner_mode="demo")
        result = await runtime.run(workspace, bundle, AsyncMock())
        grading = json.loads(result.result_json_path.read_text(encoding="utf-8"))
        assert grading["grading_standard"] == "league_second_round"
        assert grading["resolved_league_scope"] == "full_paper"
        assert len(grading["problems"]) == 4
        assert result.output_page_count == 5

    @pytest.mark.anyio
    async def test_summary_demo_builds_a4_tex_report(self, tmp_path: Path) -> None:
        from pypdf import PdfReader

        bundle = _bundle(service_tier="summary_report")
        workspace = _stage_workspace(tmp_path, bundle)
        runtime = LegacyCodexRuntime(runner_mode="demo")

        result = await runtime.run(workspace, bundle, AsyncMock())

        assert result.result_pdf_path == workspace / "report.pdf"
        assert result.output_page_count >= 1
        grading = json.loads(result.result_json_path.read_text(encoding="utf-8"))
        assert grading["service_tier"] == "summary_report"
        assert "pages" not in grading
        assert "overall_summary" not in grading
        assert all("suggestion" not in problem for problem in grading["problems"])
        page = PdfReader(str(result.result_pdf_path)).pages[0]
        assert float(page.mediabox.width) == pytest.approx(595.28, abs=4)
        assert float(page.mediabox.height) == pytest.approx(841.89, abs=4)

        import fitz

        with fitz.open(result.result_pdf_path) as document:
            report_text = "\n".join(item.get_text() for item in document)
            title_spans = [
                span
                for block in document[0].get_text("dict")["blocks"]
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span["text"] == "评分报告"
            ]
            assert len(title_spans) == 1
            title = title_spans[0]
            x0, _, x1, _ = title["bbox"]
            assert (x0 + x1) / 2 == pytest.approx(
                document[0].rect.width / 2,
                abs=0.5,
            )
            assert title["size"] == pytest.approx(24, abs=0.25)
        assert "总分" in report_text
        assert "主要问题" in report_text
        assert "总体判断" not in report_text
        assert "分题概览" not in report_text
        assert "总体建议" not in report_text


class TestTrustedServiceTierRuntimeProfiles:
    def test_summary_uses_luna_max_and_report_contract(self, tmp_path: Path) -> None:
        from worker.runtime.legacy.codex_runner import _build_codex_command

        profile = {
            "service_tier": "summary_report",
            "grading_standard": "imo",
            "league_scope": None,
            "report_mode": "summary",
        }
        command = _build_codex_command(
            codex_bin="codex",
            job_dir=tmp_path,
            schema_path=tmp_path / "manifest.schema.json",
            manifest_path=tmp_path / "manifest.json",
            has_instructions=False,
            profile=profile,
        )

        assert command[command.index("--model") + 1] == "gpt-5.6-luna"
        assert 'model_reasoning_effort="max"' in command
        assert 'web_search="disabled"' in command

    def test_annotated_uses_sol_high(self, tmp_path: Path) -> None:
        from worker.runtime.legacy.codex_runner import _build_codex_command

        profile = {
            "service_tier": "annotated_review",
            "grading_standard": "imo",
            "league_scope": None,
            "report_mode": "annotated",
        }
        command = _build_codex_command(
            codex_bin="codex",
            job_dir=tmp_path,
            schema_path=tmp_path / "manifest.schema.json",
            manifest_path=tmp_path / "manifest.json",
            has_instructions=True,
            profile=profile,
        )

        assert command[command.index("--model") + 1] == "gpt-5.6-sol"
        assert 'model_reasoning_effort="high"' in command
        assert "--search" in command


class TestManifestRuntimeBinding:
    @pytest.mark.parametrize(
        ("service_tier", "wrong_output", "report_mode"),
        [
            ("summary_report", "output/annotated.pdf", "summary"),
            ("annotated_review", "output/report.pdf", "annotated"),
        ],
    )
    def test_runtime_rejects_tier_output_path_mismatch(
        self,
        tmp_path: Path,
        service_tier: str,
        wrong_output: str,
        report_mode: str,
    ) -> None:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "service_tier": service_tier,
                    "grading_standard": "imo",
                    "resolved_league_scope": None,
                    "output_pdf": wrong_output,
                    "page_count": 1,
                    "summary": "完成批改",
                    "score": 7,
                    "max_score": 7,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with pytest.raises(CodexRunError, match="未授权的输出路径"):
            _load_manifest(
                manifest_path,
                job_dir=tmp_path,
                profile={
                    "service_tier": service_tier,
                    "grading_standard": "imo",
                    "league_scope": None,
                    "report_mode": report_mode,
                },
            )


class TestLegacyRuntimeErrorMapping:
    @pytest.mark.parametrize(
        ("legacy_code", "expected"),
        [
            ("codex_timeout", "runtime_timeout"),
            ("demo_timeout", "runtime_timeout"),
            ("codex_not_found", "runtime_unavailable"),
            ("codex_start_failed", "runtime_unavailable"),
            ("codex_network_error", "runtime_unavailable"),
            ("demo_failed", "runtime_unavailable"),
            ("codex_failed", "runtime_unavailable"),
            ("bad_manifest", "runtime_invalid_json"),
            ("bad_analysis", "runtime_invalid_json"),
            ("configuration_error", "runtime_misconfigured"),
        ],
    )
    def test_legacy_code_maps_to_stable_code(
        self, legacy_code: str, expected: str
    ) -> None:
        assert CODE_TO_RUNTIME_ERROR[legacy_code] == expected

    def test_every_mapped_target_is_a_stable_code(self) -> None:
        for target in CODE_TO_RUNTIME_ERROR.values():
            assert target in RUNTIME_ERROR_CODES

    def test_unknown_legacy_code_falls_back_to_unavailable(self) -> None:
        # Codes the adapter does not recognise are operational failures, not
        # auth or json problems; default to unavailable so the server retries.
        assert CODE_TO_RUNTIME_ERROR["totally_new_code"] == "runtime_unavailable"

    def test_codex_failed_with_auth_markers_in_logs_maps_to_auth_failed(
        self, tmp_path: Path
    ) -> None:
        from worker.runtime.legacy_codex import classify_codex_failed

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "codex-attempt-1.stderr.log").write_text(
            "Error: 401 Unauthorized. Invalid API key.\n", encoding="utf-8"
        )
        code = classify_codex_failed(logs_dir)
        assert code == "runtime_auth_failed"

    def test_codex_failed_without_auth_markers_maps_to_unavailable(
        self, tmp_path: Path
    ) -> None:
        from worker.runtime.legacy_codex import classify_codex_failed

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "codex-attempt-1.stderr.log").write_text(
            "connection reset by peer\n", encoding="utf-8"
        )
        code = classify_codex_failed(logs_dir)
        assert code == "runtime_unavailable"

    def test_codex_failed_with_missing_logs_maps_to_unavailable(
        self, tmp_path: Path
    ) -> None:
        from worker.runtime.legacy_codex import classify_codex_failed

        # If logs are missing (e.g. crash before writing), default to
        # unavailable so the server retries rather than mis-flagging auth.
        code = classify_codex_failed(tmp_path / "no-logs-here")
        assert code == "runtime_unavailable"

    @pytest.mark.anyio
    async def test_configuration_error_raises_runtime_execution_error(
        self, imo_workspace: Path, task_bundle: TaskBundle
    ) -> None:
        # An unknown runner_mode triggers the legacy configuration_error path.
        runtime = LegacyCodexRuntime(runner_mode="totally-unknown")
        with pytest.raises(RuntimeExecutionError) as exc_info:
            await runtime.run(imo_workspace, task_bundle, AsyncMock())
        assert exc_info.value.code == "runtime_misconfigured"
        # The sanitized message must not leak absolute paths or tokens.
        message = str(exc_info.value)
        assert str(imo_workspace) not in message
        assert "Authorization" not in message
        assert "Bearer" not in message

    @pytest.mark.anyio
    async def test_runtime_execution_error_carries_stable_code_only(
        self, imo_workspace: Path, task_bundle: TaskBundle
    ) -> None:
        runtime = LegacyCodexRuntime(runner_mode="totally-unknown")
        with pytest.raises(RuntimeExecutionError) as exc_info:
            await runtime.run(imo_workspace, task_bundle, AsyncMock())
        # The public surface is the stable code; the legacy code stays in the
        # local repr for operator debugging only.
        assert exc_info.value.code in RUNTIME_ERROR_CODES
        assert exc_info.value.legacy_code == "configuration_error"

    @pytest.mark.anyio
    async def test_asyncio_cancelled_maps_to_runtime_cancelled(
        self, imo_workspace: Path, task_bundle: TaskBundle, monkeypatch
    ) -> None:
        import asyncio

        from worker.runtime.legacy import codex_runner as legacy_runner

        async def _raise_cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(legacy_runner, "run_codex_job", _raise_cancelled)
        runtime = LegacyCodexRuntime(runner_mode="demo")
        with pytest.raises(RuntimeExecutionError) as exc_info:
            await runtime.run(imo_workspace, task_bundle, AsyncMock())
        assert exc_info.value.code == "runtime_cancelled"


class TestLegacyRuntimeSettingsConstruction:
    def test_settings_override_runner_mode(self) -> None:
        runtime = LegacyCodexRuntime(runner_mode="demo")
        settings = runtime._build_settings(Path("/tmp/ws"))
        assert settings.runner_mode == "demo"

    def test_settings_max_concurrent_jobs_is_one(self) -> None:
        # The worker holds one job at a time; the legacy runner must not
        # pretend it can parallelize inside a single workspace.
        runtime = LegacyCodexRuntime(runner_mode="demo")
        settings = runtime._build_settings(Path("/tmp/ws"))
        assert settings.max_concurrent_jobs == 1

    def test_settings_codex_bin_configurable(self) -> None:
        runtime = LegacyCodexRuntime(runner_mode="real", codex_bin="/usr/local/bin/codex")
        settings = runtime._build_settings(Path("/tmp/ws"))
        assert settings.codex_bin == "/usr/local/bin/codex"

    def test_settings_timeout_configurable(self) -> None:
        runtime = LegacyCodexRuntime(runner_mode="demo", timeout_seconds=120)
        settings = runtime._build_settings(Path("/tmp/ws"))
        assert settings.timeout_seconds == 120
