from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from worker.runtime.legacy import codex_runner
from worker.runtime.legacy import validate_workspace as validation_cli
from worker.runtime.legacy.codex_runner import CodexRunError
from worker.runtime.legacy.settings import Settings


def test_validation_cli_delegates_to_the_single_workspace_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[Path] = []

    def validate(job_dir: Path) -> dict[str, object]:
        seen.append(job_dir)
        return {"score": 7, "max_score": 7}

    monkeypatch.setattr(validation_cli, "validate_workspace", validate)

    assert validation_cli.main(["--job-dir", str(tmp_path)]) == 0
    assert seen == [tmp_path]
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "manifest": {"score": 7, "max_score": 7},
    }


def test_validation_cli_returns_structured_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(job_dir: Path) -> dict[str, object]:
        del job_dir
        raise CodexRunError("总分不一致。", code="bad_analysis")

    monkeypatch.setattr(validation_cli, "validate_workspace", fail)

    assert validation_cli.main(["--job-dir", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "code": "bad_analysis",
        "detail": "总分不一致。",
    }


def test_validate_workspace_wraps_the_existing_full_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = {"service_tier": "summary_report"}
    expected = {"score": 7, "max_score": 7}
    calls: list[tuple[Path, Path, dict[str, object]]] = []

    monkeypatch.setattr(codex_runner, "_load_profile", lambda path: profile)

    def load_manifest(
        path: Path, *, job_dir: Path, profile: dict[str, object]
    ) -> dict[str, object]:
        calls.append((path, job_dir, profile))
        return expected

    monkeypatch.setattr(codex_runner, "_load_manifest", load_manifest)

    assert codex_runner.validate_workspace(tmp_path) == expected
    resolved = tmp_path.resolve()
    assert calls == [(resolved / "manifest.json", resolved, profile)]


class _Stdin:
    def __init__(self) -> None:
        self.body = bytearray()

    def write(self, data: bytes) -> None:
        self.body.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _SuccessfulCodex:
    def __init__(self, manifest_path: Path) -> None:
        self._manifest_path = manifest_path
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = 0

    async def wait(self) -> int:
        self._manifest_path.write_text(
            json.dumps({"summary": "validated in the same Codex"}),
            encoding="utf-8",
        )
        return self.returncode


@pytest.mark.anyio
async def test_worker_does_not_run_full_validation_after_codex_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "config").mkdir()
    (job_dir / "input" / "instructions.txt").write_text("", encoding="utf-8")
    (job_dir / "config" / "grading-profile.json").write_text(
        json.dumps(
            {
                "service_tier": "summary_report",
                "grading_standard": "imo",
                "league_scope": None,
                "league_problem_number": None,
                "report_mode": "summary",
            }
        ),
        encoding="utf-8",
    )
    processes: list[_SuccessfulCodex] = []

    async def spawn(*args, **kwargs) -> _SuccessfulCodex:
        del args, kwargs
        process = _SuccessfulCodex(job_dir / "manifest.json")
        processes.append(process)
        return process

    def unexpected_validation(job_dir: Path) -> dict[str, object]:
        del job_dir
        raise AssertionError("Worker must not run full validation after Codex exit")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codex_runner, "validate_workspace", unexpected_validation)

    result = await codex_runner.run_codex_job(
        job_dir,
        Settings(
            codex_bin="/usr/bin/true",
            runner_mode="real",
            max_codex_attempts=2,
            timeout_seconds=60,
        ),
        lambda **kwargs: asyncio.sleep(0),
    )

    assert result.manifest == {"summary": "validated in the same Codex"}
    assert len(processes) == 1
    prompt = processes[0].stdin.body.decode("utf-8")
    assert "worker.runtime.legacy.validate_workspace" in prompt
    assert "最多修正两轮" in prompt
