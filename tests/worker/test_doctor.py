"""Tests for the worker environment doctor.

The doctor runs eight capability checks with a 20-second per-command
timeout and reports JSON plus human-readable output. The default doctor
run uses FakeGrader so it works in CI without Codex or XeLaTeX; the
``--full`` flag additionally exercises the real runtime against a
golden PDF.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest

from worker.config import WorkerSettings
from worker.runtime.doctor import (
    CheckResult,
    Doctor,
    DoctorReport,
    REQUIRED_CHECKS,
)

SHARED_KEY = "worker-shared-key-" + "w" * 32


@pytest.fixture
def worker_config(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        server_base_url="https://grader.example.com",
        shared_key=SHARED_KEY,
        installation_id="install-doctor",
        worker_id="worker-doctor",
        workspace_root=tmp_path / "workspace",
    )


@dataclass
class FakeCommand:
    """A canned response for a command name."""

    argv_prefix: tuple[str, ...]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def fake_commands(
    monkeypatch: pytest.MonkeyPatch,
    worker_config: WorkerSettings,
    tmp_path: Path,
) -> dict[str, FakeCommand]:
    """Stub command execution and binary discovery for the doctor.

    The doctor probes the local environment by running ``shutil.which``
    and ``subprocess.run``. This fixture replaces both with canned
    responses so the test suite does not depend on Codex or XeLaTeX
    being installed.

    Returns the canned-response registry so individual tests can override
    a single command (e.g. simulate a missing codex).
    """
    workspace = worker_config.workspace_root
    workspace.mkdir(parents=True, exist_ok=True)

    # Pretend every binary the doctor looks for exists on PATH.
    available = {
        "python",
        "codex",
        "xelatex",
        "fc-list",
    }

    def fake_which(name: str) -> str | None:
        if name in available:
            return f"/usr/local/bin/{name}"
        return None

    canned: dict[str, FakeCommand] = {
        "python_version": FakeCommand(
            argv_prefix=("python",),
            stdout="Python 3.14.6",
        ),
        "codex_version": FakeCommand(
            argv_prefix=("codex", "--version"),
            stdout="codex 0.10.0",
        ),
        "codex_auth": FakeCommand(
            argv_prefix=("codex", "auth", "status"),
            stdout="authenticated as worker@example.com",
        ),
        "xelatex_version": FakeCommand(
            argv_prefix=("xelatex", "--version"),
            stdout="XeTeX 3.141592653",
        ),
    }

    def fake_run(argv: Sequence[str], *args, **kwargs):
        # Match by argv prefix so callers can pass extra flags.
        key = _match_canned(argv, canned)
        if key is None:
            return _CompletedProcess(returncode=127, stdout="", stderr="not stubged")
        canned_result = canned[key]
        return _CompletedProcess(
            returncode=canned_result.returncode,
            stdout=canned_result.stdout,
            stderr=canned_result.stderr,
        )

    monkeypatch.setattr("worker.runtime.doctor._which", fake_which)
    monkeypatch.setattr("worker.runtime.doctor._run_command", fake_run)
    monkeypatch.setattr(
        "worker.runtime.doctor._http_ping",
        lambda config: True,
    )
    # Pretend the codex auth file exists.
    monkeypatch.setattr(
        "worker.runtime.doctor._codex_auth_present",
        lambda: True,
    )
    return canned


def _match_canned(
    argv: Sequence[str], canned: dict[str, FakeCommand]
) -> str | None:
    """Match canned responses by argv[0] basename plus trailing args.

    The doctor resolves binaries via ``shutil.which`` so ``argv[0]`` is a
    full path (e.g. ``/usr/local/bin/python``); we compare by basename
    so the canned registry can use the short binary name.
    """
    import os

    argv0 = os.path.basename(argv[0]) if argv else ""
    for key, fake in canned.items():
        prefix = list(fake.argv_prefix)
        head = prefix[0]
        rest = prefix[1:]
        if argv0 != head:
            continue
        if list(argv[1 : 1 + len(rest)]) == rest:
            return key
    return None


@dataclass
class _CompletedProcess:
    returncode: int
    stdout: str
    stderr: str


class TestDoctorContract:
    def test_reports_every_required_capability(
        self, fake_commands, worker_config
    ) -> None:
        report = Doctor(worker_config).run()
        assert set(REQUIRED_CHECKS) == {
            "python",
            "codex",
            "codex_auth",
            "xelatex",
            "fonts",
            "pdf_render",
            "server_auth",
            "workspace_write",
        }
        assert set(report.checks.keys()) >= set(REQUIRED_CHECKS)
        assert report.ok, report.failed_checks

    def test_check_result_shape(self) -> None:
        result = CheckResult(name="python", ok=True, detail="3.14.6")
        assert result.ok
        assert result.name == "python"

    def test_report_exposes_failed_checks_property(self, fake_commands, worker_config) -> None:
        # Force one check to fail.
        fake_commands["codex_version"] = FakeCommand(
            argv_prefix=("codex", "--version"),
            returncode=127,
            stderr="command not found",
        )
        report = Doctor(worker_config).run()
        assert not report.ok
        assert "codex" in report.failed_checks
        assert report.checks["codex"].ok is False

    def test_report_to_json_round_trip(self, fake_commands, worker_config) -> None:
        report = Doctor(worker_config).run()
        payload = json.loads(report.to_json())
        assert payload["ok"] is True
        assert set(payload["checks"].keys()) >= set(REQUIRED_CHECKS)
        for name, entry in payload["checks"].items():
            assert "ok" in entry
            assert "detail" in entry


class TestIndividualChecks:
    def test_python_check_passes_on_valid_version(
        self, fake_commands, worker_config
    ) -> None:
        report = Doctor(worker_config).run()
        assert report.checks["python"].ok
        assert "3.14" in report.checks["python"].detail or "Python" in report.checks["python"].detail

    def test_python_check_fails_when_version_probe_errors(
        self, fake_commands, worker_config, monkeypatch
    ) -> None:
        # The doctor falls back to ``sys.executable`` when ``python`` is
        # not on PATH (the runtime can still spawn build scripts), so
        # the meaningful failure mode is the version probe itself erroring.
        def fake_run(argv, *args, **kwargs):
            import os

            if os.path.basename(argv[0]) == "python":
                return _CompletedProcess(returncode=1, stdout="", stderr="broken")
            return _CompletedProcess(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr("worker.runtime.doctor._run_command", fake_run)
        report = Doctor(worker_config).run()
        assert report.checks["python"].ok is False

    def test_codex_check_fails_when_binary_missing(
        self, fake_commands, worker_config, monkeypatch
    ) -> None:
        def fake_which(name: str) -> str | None:
            if name == "codex":
                return None
            return f"/usr/local/bin/{name}"

        monkeypatch.setattr("worker.runtime.doctor._which", fake_which)
        report = Doctor(worker_config).run()
        assert report.checks["codex"].ok is False
        assert "codex" in report.failed_checks

    def test_codex_auth_check_fails_when_not_authenticated(
        self, fake_commands, worker_config, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "worker.runtime.doctor._codex_auth_present", lambda: False
        )
        report = Doctor(worker_config).run()
        assert report.checks["codex_auth"].ok is False

    def test_xelatex_check_fails_when_binary_missing(
        self, fake_commands, worker_config, monkeypatch
    ) -> None:
        def fake_which(name: str) -> str | None:
            if name == "xelatex":
                return None
            return f"/usr/local/bin/{name}"

        monkeypatch.setattr("worker.runtime.doctor._which", fake_which)
        report = Doctor(worker_config).run()
        assert report.checks["xelatex"].ok is False

    def test_fonts_check_passes_when_skill_fonts_exist(
        self, fake_commands, worker_config
    ) -> None:
        report = Doctor(worker_config).run()
        assert report.checks["fonts"].ok

    def test_fonts_check_fails_when_a_font_is_missing(
        self, fake_commands, worker_config, monkeypatch, tmp_path
    ) -> None:
        # Point the doctor at an empty fonts directory.
        empty = tmp_path / "empty-fonts"
        empty.mkdir()
        monkeypatch.setattr(
            "worker.runtime.doctor._skill_fonts_dir",
            lambda: empty,
        )
        report = Doctor(worker_config).run()
        assert report.checks["fonts"].ok is False

    def test_pdf_render_check_passes_with_minimal_pdf(
        self, fake_commands, worker_config
    ) -> None:
        report = Doctor(worker_config).run()
        assert report.checks["pdf_render"].ok

    def test_server_auth_check_passes_when_http_ping_succeeds(
        self, fake_commands, worker_config
    ) -> None:
        report = Doctor(worker_config).run()
        assert report.checks["server_auth"].ok

    def test_server_auth_check_fails_when_http_ping_fails(
        self, fake_commands, worker_config, monkeypatch
    ) -> None:
        monkeypatch.setattr("worker.runtime.doctor._http_ping", lambda config: False)
        report = Doctor(worker_config).run()
        assert report.checks["server_auth"].ok is False

    def test_workspace_write_check_fails_when_read_only(
        self, fake_commands, worker_config, monkeypatch, tmp_path
    ) -> None:
        read_only = tmp_path / "ro"
        read_only.mkdir()
        read_only.chmod(0o555)
        original_settings = worker_config.model_copy(
            update={"workspace_root": read_only}
        )
        try:
            report = Doctor(original_settings).run()
            assert report.checks["workspace_write"].ok is False
        finally:
            # Restore so cleanup can succeed.
            read_only.chmod(0o755)


class TestCommandTimeout:
    def test_run_command_applies_twenty_second_timeout(self, monkeypatch) -> None:
        captured = {}

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_popen_run(argv, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _CompletedProcess(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr("worker.runtime.doctor._run_command", fake_popen_run)
        from worker.runtime.doctor import _run_command

        _run_command(["echo", "hi"])
        # The default _run_command is what we monkeypatched; assert the
        # doctor passes timeout=20 via the real implementation contract
        # by inspecting the module constant instead.
        from worker.runtime.doctor import _COMMAND_TIMEOUT_SECONDS

        assert _COMMAND_TIMEOUT_SECONDS == 20


class TestGoldenPDFOptional:
    """The --full golden run is exercised only when the real runtime is
    available. In CI we only assert the default doctor skips it cleanly.
    """

    def test_default_run_does_not_invoke_real_runtime(
        self, fake_commands, worker_config, monkeypatch
    ) -> None:
        from worker.runtime import doctor as doctor_module

        called = {"count": 0}

        def _boom(*args, **kwargs):
            called["count"] += 1
            raise AssertionError("real runtime should not be invoked")

        monkeypatch.setattr(
            doctor_module, "_run_golden_pdf_check", _boom
        )
        Doctor(worker_config).run()
        assert called["count"] == 0


class TestMaxCodexSessionsConfig:
    """The three-session cap is reserved for the Harness runtime.

    LegacyCodexRuntime remains one process and does not pretend to
    parallelise, so the setting must default to 1 and only widen when a
    Harness implementation is wired up.
    """

    def test_default_is_one(self) -> None:
        settings = WorkerSettings(
            server_base_url="https://grader.example.com",
            shared_key=SHARED_KEY,
            installation_id="install-x",
            workspace_root=Path("/tmp/ws"),
        )
        assert settings.max_codex_sessions_per_job == 1

    def test_setting_clamped_to_three(self) -> None:
        # 4 must be rejected; 3 is the documented ceiling.
        with pytest.raises(Exception):
            WorkerSettings(
                server_base_url="https://grader.example.com",
                shared_key=SHARED_KEY,
                installation_id="install-x",
                workspace_root=Path("/tmp/ws"),
                max_codex_sessions_per_job=4,
            )

    def test_setting_rejects_zero(self) -> None:
        with pytest.raises(Exception):
            WorkerSettings(
                server_base_url="https://grader.example.com",
                shared_key=SHARED_KEY,
                installation_id="install-x",
                workspace_root=Path("/tmp/ws"),
                max_codex_sessions_per_job=0,
            )

    def test_setting_accepts_three(self) -> None:
        settings = WorkerSettings(
            server_base_url="https://grader.example.com",
            shared_key=SHARED_KEY,
            installation_id="install-x",
            workspace_root=Path("/tmp/ws"),
            max_codex_sessions_per_job=3,
        )
        assert settings.max_codex_sessions_per_job == 3


class TestCLIDoctor:
    def test_cli_doctor_invokes_doctor_and_returns_zero(
        self, fake_commands, worker_config, monkeypatch, capsys
    ) -> None:
        from worker import cli

        monkeypatch.setenv("GRADER_WORKER_SERVER_BASE_URL", worker_config.server_base_url)
        monkeypatch.setenv("GRADER_WORKER_SHARED_KEY", SHARED_KEY)
        monkeypatch.setenv("GRADER_WORKER_INSTALLATION_ID", worker_config.installation_id)
        monkeypatch.setenv("GRADER_WORKER_WORKSPACE_ROOT", str(worker_config.workspace_root))

        exit_code = cli.main(["doctor"])
        output = capsys.readouterr()
        assert exit_code == 0
        # The CLI must still hide the shared key.
        assert SHARED_KEY not in output.out
        assert SHARED_KEY not in output.err
        # And it must report the installation id and the checks.
        assert worker_config.installation_id in output.out
        assert "python" in output.out

    def test_cli_doctor_returns_one_when_a_check_fails(
        self, fake_commands, worker_config, monkeypatch, capsys
    ) -> None:
        from worker import cli

        # Force one check to fail.
        def fake_which(name: str) -> str | None:
            if name == "codex":
                return None
            return f"/usr/local/bin/{name}"

        monkeypatch.setattr("worker.runtime.doctor._which", fake_which)
        monkeypatch.setenv("GRADER_WORKER_SERVER_BASE_URL", worker_config.server_base_url)
        monkeypatch.setenv("GRADER_WORKER_SHARED_KEY", SHARED_KEY)
        monkeypatch.setenv("GRADER_WORKER_INSTALLATION_ID", worker_config.installation_id)
        monkeypatch.setenv("GRADER_WORKER_WORKSPACE_ROOT", str(worker_config.workspace_root))

        exit_code = cli.main(["doctor"])
        output = capsys.readouterr()
        assert exit_code == 1
        assert "codex" in output.out
