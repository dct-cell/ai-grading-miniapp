from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from worker.runtime.legacy import codex_runner
from worker.runtime.legacy.codex_runner import CodexRunError
from worker.runtime.legacy.settings import Settings


class _FakeStdin:
    def __init__(self) -> None:
        self.body = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.body.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _SuccessfulProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode


def _job_dir(tmp_path: Path) -> Path:
    job_dir = tmp_path / "job"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "config").mkdir()
    (job_dir / "output").mkdir()
    (job_dir / "input" / "instructions.txt").write_text("", encoding="utf-8")
    (job_dir / "config" / "grading-profile.json").write_text(
        json.dumps(
            {
                "service_tier": "annotated_review",
                "grading_standard": "imo",
                "league_scope": None,
                "league_problem_number": None,
                "report_mode": "annotated",
            }
        ),
        encoding="utf-8",
    )
    return job_dir


@pytest.mark.anyio
async def test_bad_analysis_gets_one_bounded_repair_without_clearing_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _job_dir(tmp_path)
    processes: list[_SuccessfulProcess] = []

    async def spawn(*args, **kwargs) -> _SuccessfulProcess:
        del args, kwargs
        process = _SuccessfulProcess()
        processes.append(process)
        return process

    load_calls = 0

    def load_manifest(path: Path, *, job_dir: Path, profile: dict) -> dict:
        nonlocal load_calls
        del path, profile
        load_calls += 1
        marker = job_dir / "output" / "internal" / "existing-analysis.json"
        if load_calls == 1:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("{}", encoding="utf-8")
            raise CodexRunError(
                "评分点 p1-u6-main 在依赖未满足时被计分。",
                code="bad_analysis",
            )
        assert marker.is_file(), "repair must retain the existing analysis"
        return {"summary": "repaired"}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codex_runner, "_load_manifest", load_manifest)

    result = await codex_runner.run_codex_job(
        job_dir,
        Settings(
            codex_bin="/usr/bin/true",
            runner_mode="real",
            max_codex_attempts=2,
            timeout_seconds=60,
        ),
        AsyncMock(),
    )

    assert result.manifest == {"summary": "repaired"}
    assert len(processes) == 2
    initial_prompt = processes[0].stdin.body.decode("utf-8")
    repair_prompt = processes[1].stdin.body.decode("utf-8")
    assert "使用 $olympiad-grader" in initial_prompt
    assert "不得从头重新批改" in repair_prompt
    assert "p1-u6-main" in repair_prompt


@pytest.mark.anyio
async def test_a_failed_repair_is_not_retried_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _job_dir(tmp_path)
    processes: list[_SuccessfulProcess] = []

    async def spawn(*args, **kwargs) -> _SuccessfulProcess:
        del args, kwargs
        process = _SuccessfulProcess()
        processes.append(process)
        return process

    def always_invalid(path: Path, *, job_dir: Path, profile: dict) -> dict:
        del path, job_dir, profile
        raise CodexRunError(
            "评分点 p1-u6-main 在依赖未满足时被计分。",
            code="bad_analysis",
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codex_runner, "_load_manifest", always_invalid)

    with pytest.raises(CodexRunError, match="依赖未满足") as caught:
        await codex_runner.run_codex_job(
            job_dir,
            Settings(
                codex_bin="/usr/bin/true",
                runner_mode="real",
                max_codex_attempts=2,
                timeout_seconds=60,
            ),
            AsyncMock(),
        )

    assert caught.value.code == "bad_analysis"
    assert len(processes) == 2
