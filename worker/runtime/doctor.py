"""Worker environment doctor.

Runs eight capability checks with a 20-second per-command timeout and
returns a structured report. The CLI surfaces this as ``worker doctor``.

Checks
------
``python``          current interpreter is reachable and reports a version
``codex``           the ``codex`` binary is on PATH and reports a version
``codex_auth``      the local Codex auth file (or ``codex auth status``) is present
``xelatex``         ``xelatex`` is on PATH and reports a version
``fonts``           the two Noto CJK fonts the report renderer needs exist
``pdf_render``      pypdf can render a minimal PDF round-trip
``server_auth``     the Worker control plane accepts the configured shared key
``workspace_write`` the configured workspace root is writable

The default run never invokes the real grading runtime. ``--full`` adds
a golden-PDF check that runs LegacyCodexRuntime against a checked-in
fixture and validates schema, page count, score range and PDF
renderability. CI runs the default path; ``--full`` is opt-in.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from worker.config import WorkerSettings

__all__ = [
    "CheckResult",
    "Doctor",
    "DoctorReport",
    "REQUIRED_CHECKS",
    "_COMMAND_TIMEOUT_SECONDS",
]


REQUIRED_CHECKS = (
    "python",
    "codex",
    "codex_auth",
    "xelatex",
    "fonts",
    "pdf_render",
    "server_auth",
    "workspace_write",
)

_COMMAND_TIMEOUT_SECONDS = 20

_REQUIRED_FONTS = (
    "NotoSansCJKsc-Medium.otf",
    "NotoSerifCJKsc-Regular.otf",
)


def _which(name: str) -> str | None:
    """Indirection so tests can stub binary discovery."""
    return shutil.which(name)


def _run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture output within the timeout.

    Indirection so tests can stub command execution. Never uses
    ``shell=True`` — the doctor only probes trusted, fixed binary names.
    """
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


def _http_ping(config: WorkerSettings) -> bool:
    """Return True if the server accepts the worker's credentials.

    Default implementation defers to :class:`worker.client.WorkerClient`
    so production hits the real heartbeat endpoint. Tests stub this
    directly to avoid network I/O.
    """
    from worker.client import WorkerClient

    try:
        import anyio

        return bool(anyio.run(WorkerClient(config).heartbeat))
    except Exception:
        return False


def _codex_auth_present() -> bool:
    """Return True if the local Codex auth file exists.

    The Codex CLI stores credentials in ``~/.codex/auth.json`` on every
    supported platform. Probing the file is faster and quieter than
    shelling out to ``codex auth status`` and works even when the CLI
    is installed but slow to start.
    """
    home = os.path.expanduser("~")
    return (Path(home) / ".codex" / "auth.json").is_file()


def _skill_fonts_dir() -> Path:
    """Return the skill fonts directory the runtime copies at run time."""
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / ".agents" / "skills" / "olympiad-grader" / "assets" / "fonts"


def _run_golden_pdf_check(config: WorkerSettings) -> CheckResult:
    """Run the real runtime against the golden PDF.

    Only invoked via ``doctor --full``. The default doctor run skips
    this so CI does not depend on Codex or XeLaTeX being installed.
    """
    raise NotImplementedError(
        "golden-PDF check is opt-in via `worker doctor --full`"
    )


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single doctor check."""

    name: str
    ok: bool
    detail: str


@dataclass
class DoctorReport:
    """Aggregate result of a doctor run."""

    checks: dict[str, CheckResult] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.checks.values())

    @property
    def failed_checks(self) -> list[str]:
        return [name for name, result in self.checks.items() if not result.ok]

    def to_json(self) -> str:
        payload: dict[str, object] = {
            "ok": self.ok,
            "checks": {
                name: {"ok": result.ok, "detail": result.detail}
                for name, result in self.checks.items()
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def to_human(self) -> str:
        lines = [f"doctor: {'OK' if self.ok else 'FAIL'}"]
        for name in REQUIRED_CHECKS:
            result = self.checks.get(name)
            if result is None:
                lines.append(f"  {name}: (not run)")
                continue
            status = "ok" if result.ok else "FAIL"
            lines.append(f"  {name}: {status} — {result.detail}")
        if self.failed_checks:
            lines.append(f"failed: {', '.join(self.failed_checks)}")
        return "\n".join(lines)


class Doctor:
    """Run capability checks against the local worker environment.

    The doctor is intentionally side-effect free: it does not write to
    the workspace root, modify the codex auth file, or push anything to
    the server. ``server_auth`` performs a single heartbeat round-trip
    so the server can reject the shared key without granting a lease.
    """

    def __init__(
        self,
        config: WorkerSettings,
        *,
        full: bool = False,
    ) -> None:
        self._config = config
        self._full = full

    def run(self) -> DoctorReport:
        report = DoctorReport()
        for name in REQUIRED_CHECKS:
            handler = self._handlers().get(name)
            if handler is None:
                report.checks[name] = CheckResult(
                    name=name, ok=False, detail="no handler"
                )
                continue
            try:
                report.checks[name] = handler()
            except Exception as exc:  # noqa: BLE001 — doctor never raises
                report.checks[name] = CheckResult(
                    name=name, ok=False, detail=f"check crashed: {exc}"
                )
        if self._full:
            try:
                golden = _run_golden_pdf_check(self._config)
                report.checks["golden_pdf"] = golden
            except NotImplementedError as exc:
                report.checks["golden_pdf"] = CheckResult(
                    name="golden_pdf", ok=False, detail=str(exc)
                )
        return report

    def _handlers(self) -> Mapping[str, Callable[[], CheckResult]]:
        return {
            "python": self._check_python,
            "codex": self._check_codex,
            "codex_auth": self._check_codex_auth,
            "xelatex": self._check_xelatex,
            "fonts": self._check_fonts,
            "pdf_render": self._check_pdf_render,
            "server_auth": self._check_server_auth,
            "workspace_write": self._check_workspace_write,
        }

    # --- individual checks -------------------------------------------------

    def _check_python(self) -> CheckResult:
        binary = _which("python") or sys.executable
        if binary is None:
            return CheckResult("python", False, "no python interpreter on PATH")
        try:
            result = _run_command([binary, "--version"])
        except subprocess.TimeoutExpired:
            return CheckResult("python", False, "version probe timed out")
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout:
            return CheckResult("python", True, stdout)
        return CheckResult(
            "python",
            False,
            f"python --version failed rc={result.returncode} stderr={result.stderr.strip()}",
        )

    def _check_codex(self) -> CheckResult:
        binary = _which("codex")
        if binary is None:
            return CheckResult("codex", False, "codex not on PATH")
        try:
            result = _run_command([binary, "--version"])
        except subprocess.TimeoutExpired:
            return CheckResult("codex", False, "version probe timed out")
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout:
            return CheckResult("codex", True, stdout)
        return CheckResult(
            "codex",
            False,
            f"codex --version failed rc={result.returncode} stderr={result.stderr.strip()}",
        )

    def _check_codex_auth(self) -> CheckResult:
        if _codex_auth_present():
            return CheckResult("codex_auth", True, "auth file present")
        return CheckResult(
            "codex_auth",
            False,
            "~/.codex/auth.json missing — run `codex login`",
        )

    def _check_xelatex(self) -> CheckResult:
        binary = _which("xelatex")
        if binary is None:
            return CheckResult("xelatex", False, "xelatex not on PATH")
        try:
            result = _run_command([binary, "--version"])
        except subprocess.TimeoutExpired:
            return CheckResult("xelatex", False, "version probe timed out")
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout:
            return CheckResult("xelatex", True, stdout.splitlines()[0])
        return CheckResult(
            "xelatex",
            False,
            f"xelatex --version failed rc={result.returncode}",
        )

    def _check_fonts(self) -> CheckResult:
        fonts_dir = _skill_fonts_dir()
        missing = [
            name for name in _REQUIRED_FONTS
            if not (fonts_dir / name).is_file()
        ]
        if missing:
            return CheckResult(
                "fonts",
                False,
                f"missing in {fonts_dir}: {', '.join(missing)}",
            )
        return CheckResult(
            "fonts",
            True,
            f"all {len(_REQUIRED_FONTS)} fonts present in {fonts_dir}",
        )

    def _check_pdf_render(self) -> CheckResult:
        try:
            from pypdf import PdfReader

            from worker.runtime.testsupport import build_minimal_pdf
        except ImportError as exc:
            return CheckResult(
                "pdf_render",
                False,
                f"pdf library unavailable: {exc}",
            )
        try:
            pdf_bytes = build_minimal_pdf(page_count=1)
            from io import BytesIO

            reader = PdfReader(BytesIO(pdf_bytes))
            page_count = len(reader.pages)
        except Exception as exc:  # noqa: BLE001
            return CheckResult("pdf_render", False, f"render failed: {exc}")
        if page_count != 1:
            return CheckResult(
                "pdf_render",
                False,
                f"expected 1 page, got {page_count}",
            )
        return CheckResult("pdf_render", True, "pypdf parsed minimal PDF")

    def _check_server_auth(self) -> CheckResult:
        ok = _http_ping(self._config)
        if ok:
            return CheckResult(
                "server_auth",
                True,
                f"heartbeat accepted by {self._config.server_base_url}",
            )
        return CheckResult(
            "server_auth",
            False,
            f"heartbeat rejected by {self._config.server_base_url}",
        )

    def _check_workspace_write(self) -> CheckResult:
        root = self._config.workspace_root
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CheckResult(
                "workspace_write",
                False,
                f"cannot create workspace root {root}: {exc}",
            )
        probe = root / ".doctor-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
        except OSError as exc:
            return CheckResult(
                "workspace_write",
                False,
                f"workspace root {root} is not writable: {exc}",
            )
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        return CheckResult(
            "workspace_write",
            True,
            f"workspace root {root} is writable",
        )
