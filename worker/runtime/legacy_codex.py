from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from worker.runtime.contracts import GradingRuntime, RuntimeResult, TaskBundle
from worker.runtime.legacy import codex_runner as _legacy_runner
from worker.runtime.legacy.codex_runner import CodexRunError
from worker.runtime.legacy.settings import Settings

__all__ = [
    "CODE_TO_RUNTIME_ERROR",
    "RuntimeExecutionError",
    "LegacyCodexRuntime",
    "classify_codex_failed",
]


# Map each legacy CodexRunError.code to a stable Worker error code. The
# adapter never exposes the legacy names through the control plane. Unknown
# codes default to runtime_unavailable so the server retries rather than
# mis-classifying an unfamiliar failure as auth or json.
CODE_TO_RUNTIME_ERROR: dict[str, str] = defaultdict(
    lambda: "runtime_unavailable",
    {
        "codex_timeout": "runtime_timeout",
        "demo_timeout": "runtime_timeout",
        "codex_not_found": "runtime_unavailable",
        "codex_start_failed": "runtime_unavailable",
        "codex_network_error": "runtime_unavailable",
        "demo_failed": "runtime_unavailable",
        "codex_failed": "runtime_unavailable",
        "bad_manifest": "runtime_invalid_json",
        "bad_analysis": "runtime_invalid_json",
        "configuration_error": "runtime_misconfigured",
    },
)


# Auth markers scanned from codex stderr logs to distinguish auth failures
# from generic operational failures. The legacy runner folds both into
# ``codex_failed``; the adapter re-classifies by reading the logs it wrote.
_AUTH_MARKERS = (
    "401 unauthorized",
    "403 forbidden",
    "invalid api key",
    "invalid_api_key",
    "unauthorised",
    "unauthorized",
    "authentication required",
    "not authenticated",
    "missing api key",
)

# Patterns scrubbed from sanitised messages before they leave the worker.
# Absolute paths leak the host filesystem layout; Authorization/Bearer
# strings leak credentials. The patterns cover both literal and lowercased
# forms.
_SENSITIVE_PATTERNS = (
    re.compile(r"/Users/[^/\s]+/[\S]+"),
    re.compile(r"/home/[^/\s]+/[\S]+"),
    re.compile(r"Authorization:\s*\S+", re.IGNORECASE),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
)


class RuntimeExecutionError(RuntimeError):
    """Raised when the legacy runtime fails.

    ``code`` is the stable Worker error code the daemon uploads to the server.
    ``legacy_code`` keeps the original CodexRunError code for local operator
    debugging only and never crosses the control plane.
    """

    def __init__(
        self,
        code: str,
        *,
        legacy_code: str | None = None,
        message: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.legacy_code = legacy_code
        self.message = message

    def __str__(self) -> str:
        return self.message


def classify_codex_failed(logs_dir: Path) -> str:
    """Re-classify ``codex_failed`` by scanning the codex stderr logs.

    Returns ``runtime_auth_failed`` when an auth marker appears in any
    ``codex-attempt-*.stderr.log`` under ``logs_dir``; otherwise returns
    ``runtime_unavailable``. Missing logs default to unavailable so the
    server retries rather than mis-flagging auth.
    """
    if not logs_dir.is_dir():
        return "runtime_unavailable"
    for entry in logs_dir.glob("codex-attempt-*.stderr.log"):
        try:
            text = entry.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(marker in text for marker in _AUTH_MARKERS):
            return "runtime_auth_failed"
    return "runtime_unavailable"


def _sanitise_message(raw: str) -> str:
    """Strip absolute paths and credential strings from a legacy message."""
    sanitised = raw
    for pattern in _SENSITIVE_PATTERNS:
        sanitised = pattern.sub("[redacted]", sanitised)
    return sanitised


class LegacyCodexRuntime:
    """Adapter that runs the verified legacy Codex/XeLaTeX grader.

    The adapter owns:
    - converting a TaskBundle into the legacy ``Settings`` shape,
    - forwarding legacy stage callbacks to the daemon's progress callable,
    - validating output PDF/JSON and building a RuntimeResult,
    - mapping CodexRunError codes to the seven stable Worker error codes.

    It does not own process spawning (the legacy runner does, via asyncio)
    or server interaction (the daemon does).
    """

    def __init__(
        self,
        *,
        runner_mode: str = "demo",
        codex_bin: str = "codex",
        timeout_seconds: int = 60 * 60,
        max_codex_attempts: int = 2,
        retry_delay_seconds: float = 3.0,
    ) -> None:
        self._runner_mode = runner_mode
        self._codex_bin = codex_bin
        self._timeout_seconds = timeout_seconds
        self._max_codex_attempts = max_codex_attempts
        self._retry_delay_seconds = retry_delay_seconds

    def _build_settings(self, workspace: Path) -> Settings:
        """Build a Settings that points the legacy runner at one workspace.

        The legacy runner reads only runner_mode, codex_bin, max_codex_attempts,
        timeout_seconds and retry_delay_seconds; the path fields are unused
        because the workspace is already prepared by the daemon.
        max_concurrent_jobs is forced to 1: the worker holds one job at a
        time and the legacy runner must not pretend it can parallelise
        inside a single workspace.
        """
        return Settings(
            project_root=workspace,
            data_dir=workspace,
            skill_source_dir=workspace / ".agents" / "skills" / "olympiad-grader",
            manifest_schema_path=workspace / "config" / "manifest.schema.json",
            codex_bin=self._codex_bin,
            runner_mode=self._runner_mode,
            timeout_seconds=self._timeout_seconds,
            max_codex_attempts=self._max_codex_attempts,
            retry_delay_seconds=self._retry_delay_seconds,
            max_concurrent_jobs=1,
        )

    async def run(
        self,
        workspace: Path,
        bundle: TaskBundle,
        progress: Callable[[str], Awaitable[None]],
    ) -> RuntimeResult:
        settings = self._build_settings(workspace)

        async def legacy_callback(*, stage: str, message: str | None = None, **_: Any) -> None:
            # The legacy runner calls status_callback with keyword args
            # (attempts, stage, message). The daemon only cares about the
            # stage label, which is the trusted, server-defined string.
            if stage:
                await progress(stage)

        try:
            await _legacy_runner.run_codex_job(workspace, settings, legacy_callback)
        except CodexRunError as exc:
            raise self._map_error(exc, workspace) from exc
        except asyncio.CancelledError:
            # Cancellation means drain, refund, or a lost lease.  Preserve
            # asyncio's control-flow signal so the daemon never reports it as a
            # second runtime failure.
            raise

        # The runner writes a tier-specific PDF plus output/grading.json and
        # manifest.json. Promote the manifest-authorized PDF to the workspace
        # root without assuming every service produces annotated.pdf.
        self._promote_outputs(workspace)
        try:
            return RuntimeResult.from_workspace(workspace)
        except ValueError as exc:
            # Output validation failed even though the runner returned. This
            # is a runtime bug, not a server-side misconfiguration.
            raise RuntimeExecutionError(
                "runtime_invalid_pdf",
                legacy_code="output_validation",
                message=_sanitise_message(str(exc)),
            ) from exc

    def _map_error(self, exc: CodexRunError, workspace: Path) -> RuntimeExecutionError:
        legacy_code = exc.code
        if legacy_code == "codex_failed":
            # codex_failed folds auth and operational failures together;
            # re-classify by scanning the stderr logs the runner wrote.
            stable_code = classify_codex_failed(workspace / "logs")
        else:
            stable_code = CODE_TO_RUNTIME_ERROR.get(legacy_code, "runtime_unavailable")
        return RuntimeExecutionError(
            stable_code,
            legacy_code=legacy_code,
            message=_sanitise_message(str(exc)),
        )

    def _promote_outputs(self, workspace: Path) -> None:
        """Move legacy output paths to the RuntimeResult canonical layout.

        Promoting with os.replace is atomic and idempotent.
        """
        output_dir = workspace / "output"
        manifest = workspace / "manifest.json"
        output_name = "annotated.pdf"
        if manifest.is_file():
            try:
                import json

                output_pdf = json.loads(manifest.read_text(encoding="utf-8")).get(
                    "output_pdf"
                )
                if output_pdf in {"output/report.pdf", "output/annotated.pdf"}:
                    output_name = Path(output_pdf).name
            except (OSError, ValueError, AttributeError):
                pass
        for name in (output_name, "grading.json"):
            legacy_path = output_dir / name
            target = workspace / name
            if legacy_path.is_file() and not target.exists():
                os.replace(legacy_path, target)
        # manifest.json is already at the workspace root in the legacy layout.
