# Phase 04 Grading Runtime Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace FakeGrader with a stable GradingRuntime adapter that can run the existing Codex/XeLaTeX grader now and a future Harness later without changing server APIs.

**Architecture:** The Worker converts a downloaded TaskBundle into an isolated legacy-compatible workspace, invokes a GradingRuntime protocol, validates JSON/PDF/checksums, and uploads only public result artifacts. Platform adapters own process creation and termination; the grading contract owns neither server nor OS details.

**Tech Stack:** Existing app.codex_runner, olympiad-grader skill, Codex CLI, XeLaTeX, PyMuPDF, asyncio, pytest

---

### Task 1: Freeze the Worker grading contract

**Files:**
- Create: worker/runtime/contracts.py
- Create: worker/runtime/result.schema.json
- Test: tests/worker/test_runtime_contracts.py

- [ ] **Step 1: Write failing contract tests**

    def test_task_bundle_rejects_third_round() -> None:
        with pytest.raises(ValidationError):
            TaskBundle(
                job_id="j", order_id="o", round_number=3,
                grading_standard="imo", source_pdf="input/source.pdf",
                page_count=1, note="", reference_pdf=None,
            )

    def test_runtime_result_requires_existing_pdf_and_json(tmp_path) -> None:
        with pytest.raises(ValueError, match="result PDF is missing"):
            RuntimeResult.from_workspace(tmp_path)

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/worker/test_runtime_contracts.py -q

Expected: contract types are missing.

- [ ] **Step 3: Implement concrete contracts**

    class TaskBundle(BaseModel):
        job_id: str
        order_id: str
        round_number: Literal[1, 2]
        grading_standard: Literal["league_second_round", "cmo", "imo"]
        league_scope: Literal["auto", "full_paper", "problem_set"] | None = None
        source_pdf: str
        reference_pdf: str | None
        page_count: int = Field(ge=1)
        note: str = Field(max_length=4000)

    class RuntimeResult(BaseModel):
        manifest_path: Path
        result_json_path: Path
        result_pdf_path: Path
        result_json_sha256: str
        result_pdf_sha256: str
        output_page_count: int

    class GradingRuntime(Protocol):
        async def run(
            self,
            workspace: Path,
            bundle: TaskBundle,
            progress: Callable[[str], Awaitable[None]],
        ) -> RuntimeResult: ...

- [ ] **Step 4: Define result.schema.json**

Require grading_standard, title, total_score, max_score, overall_summary, problems and pages. Each page finding must include an integer ID, kind, title, reason, deduction and one or more normalized bounding boxes. Keep the schema compatible with app/manifest.schema.json; add a test that every legacy valid fixture validates against both schemas.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/worker/test_runtime_contracts.py -q
    git add worker/runtime/contracts.py worker/runtime/result.schema.json tests/worker/test_runtime_contracts.py
    git commit -m "feat: freeze worker grading contract"

### Task 2: Build a legacy-compatible workspace

**Files:**
- Create: worker/runtime/workspace.py
- Test: tests/worker/test_runtime_workspace.py

- [ ] **Step 1: Write the failing workspace-layout test**

    def test_workspace_matches_existing_runner_layout(tmp_path, downloaded_bundle) -> None:
        workspace = prepare_workspace(tmp_path, downloaded_bundle)
        assert (workspace / "input/submission.pdf").is_file()
        assert (workspace / "input/reference.pdf").is_file()
        assert (workspace / "input/instructions.txt").read_text() == downloaded_bundle.note
        assert json.loads((workspace / "config/grading-profile.json").read_text())[
            "grading_standard"
        ] == downloaded_bundle.grading_standard
        assert (workspace / "config/manifest.schema.json").is_file()
        assert (workspace / ".agents/skills/olympiad-grader/SKILL.md").is_file()

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/worker/test_runtime_workspace.py -q

Expected: prepare_workspace is missing.

- [ ] **Step 3: Implement deterministic layout creation**

Copy the source to input/submission.pdf, optional reference to input/reference.pdf, write UTF-8 instructions.txt, write grading-profile.json, copy app/manifest.schema.json, and copy the olympiad-grader skill while hard-linking bundled fonts when supported. Reject symlinks and paths escaping the workspace.

- [ ] **Step 4: Add cleanup tests**

Verify success cleanup removes QA renders, temporary skill copies and .latex-build-* directories but preserves source, reference, public result JSON/PDF, manifest and sanitized logs until server commit succeeds.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/worker/test_runtime_workspace.py -q
    git add worker/runtime/workspace.py tests/worker/test_runtime_workspace.py
    git commit -m "feat: prepare isolated grading workspaces"

### Task 3: Adapt the existing runner

**Files:**
- Create: worker/runtime/legacy_codex.py
- Modify: worker/runtime/daemon.py
- Test: tests/worker/test_legacy_runtime.py

- [ ] **Step 1: Write the failing demo-runtime test**

    @pytest.mark.asyncio
    async def test_legacy_runtime_returns_valid_public_artifacts(tmp_path, task_bundle) -> None:
        runtime = LegacyCodexRuntime(runner_mode="demo")
        result = await runtime.run(tmp_path, task_bundle, AsyncMock())
        assert result.result_pdf_path.name == "annotated.pdf"
        assert result.result_json_path.name == "grading.json"
        assert result.output_page_count >= task_bundle.page_count

- [ ] **Step 2: Confirm failure**

    AI_GRADER_RUNNER_MODE=demo .venv/bin/python -m pytest tests/worker/test_legacy_runtime.py -q

Expected: LegacyCodexRuntime is missing.

- [ ] **Step 3: Implement the adapter**

Create app.settings.Settings with data_dir equal to the single workspace parent, runner_mode from Worker configuration, max_concurrent_jobs = 1, and the configured timeout. Call app.codex_runner.run_codex_job(workspace, settings, callback). The callback forwards stages through the active lease renewer. Validate output/annotated.pdf with app.pdf_utils.inspect_pdf and load output/grading.json plus manifest.json.

- [ ] **Step 4: Map failures to stable codes**

Map CodexRunError codes to:
- runtime_auth_failed
- runtime_unavailable
- runtime_timeout
- runtime_invalid_json
- runtime_invalid_pdf
- runtime_cancelled

Upload only the stable code and a sanitized message. Keep raw stdout/stderr local and redact tokens, Authorization headers and absolute home paths.

- [ ] **Step 5: Run and commit**

    AI_GRADER_RUNNER_MODE=demo .venv/bin/python -m pytest tests/worker/test_legacy_runtime.py -q
    git add worker/runtime/legacy_codex.py worker/runtime/daemon.py tests/worker/test_legacy_runtime.py
    git commit -m "feat: run verified grader through worker runtime"

### Task 4: Add OS process adapters

**Files:**
- Create: worker/platforms/base.py
- Create: worker/platforms/macos.py
- Create: worker/platforms/linux.py
- Create: worker/platforms/windows.py
- Test: tests/worker/test_platform_processes.py

- [ ] **Step 1: Write adapter-selection tests**

    @pytest.mark.parametrize(
        ("system", "expected"),
        [("Darwin", MacOSPlatform), ("Linux", LinuxPlatform), ("Windows", WindowsPlatform)],
    )
    def test_selects_native_adapter(monkeypatch, system, expected) -> None:
        monkeypatch.setattr(platform, "system", lambda: system)
        assert isinstance(current_platform(), expected)

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/worker/test_platform_processes.py -q

Expected: platform modules are missing.

- [ ] **Step 3: Implement process ownership**

Define start_process, terminate_tree and service_status. macOS/Linux start a new process group and terminate SIGTERM then SIGKILL after ten seconds. Windows creates a new process group and attaches it to a Job Object so cancellation or Worker exit terminates descendants. Never use shell=True.

- [ ] **Step 4: Add real platform smoke tests**

On each platform run a child that spawns a long-lived grandchild, call terminate_tree, and assert both PIDs exit within 15 seconds. Mark each test with the matching platform skip condition.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/worker/test_platform_processes.py -q
    git add worker/platforms tests/worker/test_platform_processes.py
    git commit -m "feat: add native worker process adapters"

### Task 5: Implement doctor and golden-PDF verification

**Files:**
- Create: worker/runtime/doctor.py
- Create: worker/assets/golden-input.pdf
- Create: worker/assets/golden-expected.json
- Modify: worker/cli.py
- Test: tests/worker/test_doctor.py

- [ ] **Step 1: Write the failing doctor test**

    def test_doctor_reports_every_required_capability(fake_commands, worker_config) -> None:
        report = Doctor(worker_config).run()
        assert report.checks.keys() >= {
            "python", "codex", "codex_auth", "xelatex", "fonts",
            "pdf_render", "server_auth", "workspace_write",
        }
        assert report.ok

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/worker/test_doctor.py -q

Expected: Doctor is missing.

- [ ] **Step 3: Implement checks**

Run commands without a shell, apply a 20-second timeout per check, and return JSON plus human-readable output. The golden test uses FakeGrader by default; --full additionally runs the real runtime and checks schema, page count, score range and PDF renderability.

- [ ] **Step 4: Enforce three-session cap as configuration**

Add max_codex_sessions_per_job with allowed range 1..3. Pass it only to a Harness implementation that supports parallel subagents; LegacyCodexRuntime remains one process and does not pretend to parallelize.

- [ ] **Step 5: Run the phase gate**

    .venv/bin/python -m pytest tests/worker -q
    AI_GRADER_RUNNER_MODE=demo .venv/bin/python -m pytest -q

Expected: all tests pass.

- [ ] **Step 6: Commit**

    git add worker/runtime/doctor.py worker/assets worker/cli.py tests/worker/test_doctor.py
    git commit -m "feat: add worker environment doctor"
