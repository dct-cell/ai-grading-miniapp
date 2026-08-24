from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from worker.runtime.contracts import TaskBundle
from worker.runtime.testsupport import build_minimal_pdf
from worker.runtime.workspace import (
    WORKSPACE_LAYOUT,
    WorkspaceError,
    cleanup_transient_artifacts,
    prepare_workspace,
)


HERE = Path(__file__).resolve().parent
RUNTIME_ROOT = HERE.parent.parent / "worker" / "runtime"


def _stage_pdf(parent: Path, name: str = "source.pdf") -> Path:
    """Drop a minimal valid PDF so prepare_workspace can copy it."""
    path = parent / name
    path.write_bytes(build_minimal_pdf(page_count=1))
    return path


class TestWorkspaceLayout:
    def test_layout_constants_match_legacy_runner(self) -> None:
        # The legacy runner reads from these exact relative paths. The
        # workspace layout is a contract, not a convenience.
        assert WORKSPACE_LAYOUT.source_pdf == "input/submission.pdf"
        assert WORKSPACE_LAYOUT.reference_pdf == "input/reference.pdf"
        assert WORKSPACE_LAYOUT.instructions == "input/instructions.txt"
        assert WORKSPACE_LAYOUT.grading_profile == "config/grading-profile.json"
        assert WORKSPACE_LAYOUT.manifest_schema == "config/manifest.schema.json"
        assert WORKSPACE_LAYOUT.skill_dir == ".agents/skills/olympiad-grader"
        assert WORKSPACE_LAYOUT.output_dir == "output"

    def test_prepare_workspace_copies_source_pdf_to_submission(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        submission = workspace / "input/submission.pdf"
        assert submission.is_file()
        source_path = Path(downloaded_bundle.source_pdf)
        assert submission.read_bytes() == source_path.read_bytes()

    def test_prepare_workspace_writes_instructions_txt(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        instructions = workspace / "input/instructions.txt"
        assert instructions.is_file()
        assert instructions.read_text(encoding="utf-8") == downloaded_bundle.note

    def test_prepare_workspace_writes_empty_instructions_when_note_blank(
        self, tmp_path: Path
    ) -> None:
        source = _stage_pdf(tmp_path)
        bundle = downloaded_bundle_with(source_pdf=str(source), note="")
        workspace = prepare_workspace(tmp_path / "ws", bundle)
        assert (workspace / "input/instructions.txt").read_text(encoding="utf-8") == ""

    def test_prepare_workspace_copies_reference_pdf_when_provided(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        reference = workspace / "input/reference.pdf"
        assert reference.is_file()
        reference_path = Path(downloaded_bundle.reference_pdf)  # type: ignore[arg-type]
        assert reference.read_bytes() == reference_path.read_bytes()

    def test_prepare_workspace_omits_reference_when_none(
        self, tmp_path: Path
    ) -> None:
        source = _stage_pdf(tmp_path)
        bundle = downloaded_bundle_with(
            source_pdf=str(source), reference_pdf=None
        )
        workspace = prepare_workspace(tmp_path / "ws", bundle)
        assert not (workspace / "input/reference.pdf").exists()

    def test_prepare_workspace_writes_grading_profile_json(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        profile = json.loads(
            (workspace / "config/grading-profile.json").read_text(encoding="utf-8")
        )
        assert profile["grading_standard"] == "imo"
        assert profile["league_scope"] is None

    def test_prepare_workspace_writes_league_profile_with_scope(
        self, tmp_path: Path
    ) -> None:
        source = _stage_pdf(tmp_path)
        bundle = downloaded_bundle_with(
            source_pdf=str(source),
            grading_standard="league_second_round",
            league_scope="full_paper",
        )
        workspace = prepare_workspace(tmp_path / "ws", bundle)
        profile = json.loads(
            (workspace / "config/grading-profile.json").read_text(encoding="utf-8")
        )
        assert profile["grading_standard"] == "league_second_round"
        assert profile["league_scope"] == "full_paper"

    def test_prepare_workspace_copies_manifest_schema(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        schema_path = workspace / "config/manifest.schema.json"
        assert schema_path.is_file()
        # The copied schema must be byte-identical to the legacy source.
        legacy = RUNTIME_ROOT / "legacy" / "manifest.schema.json"
        assert schema_path.read_bytes() == legacy.read_bytes()

    def test_prepare_workspace_copies_olympiad_grader_skill(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        skill_md = workspace / ".agents/skills/olympiad-grader/SKILL.md"
        assert skill_md.is_file()
        # The renderer script is required for the demo path and the real path.
        builder = (
            workspace
            / ".agents"
            / "skills"
            / "olympiad-grader"
            / "scripts"
            / "build_annotated_pdf.py"
        )
        assert builder.is_file()

    def test_prepare_workspace_creates_empty_output_dir(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        assert (workspace / "output").is_dir()
        assert list((workspace / "output").iterdir()) == []

    def test_prepare_workspace_returns_workspace_path(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace_root = tmp_path / "ws"
        workspace = prepare_workspace(workspace_root, downloaded_bundle)
        assert workspace == workspace_root
        assert workspace.is_dir()

    def test_skill_fonts_are_hardlinked_when_filesystem_allows(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        # Fonts are ≈40MB combined; hard-linking per workspace avoids copying.
        # On filesystems that reject hard links (rare in CI) we fall back to a
        # copy, so assert hard-link equality only when the inode matches.
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        workspace_font = (
            workspace
            / ".agents"
            / "skills"
            / "olympiad-grader"
            / "assets"
            / "fonts"
            / "NotoSansCJKsc-Medium.otf"
        )
        source_font = (
            RUNTIME_ROOT.parent.parent
            / ".agents"
            / "skills"
            / "olympiad-grader"
            / "assets"
            / "fonts"
            / "NotoSansCJKsc-Medium.otf"
        )
        assert workspace_font.is_file()
        # If the inode matches the copy was a hard-link; otherwise it fell
        # back to a copy. Both are acceptable.
        if workspace_font.stat().st_ino == source_font.stat().st_ino:
            assert workspace_font.stat().st_nlink >= 2


class TestWorkspaceSecurity:
    def test_rejects_source_pdf_symlink(self, tmp_path: Path) -> None:
        real = _stage_pdf(tmp_path, "real.pdf")
        link = tmp_path / "link.pdf"
        os.symlink(real, link)
        bundle = downloaded_bundle_with(source_pdf=str(link))
        with pytest.raises(WorkspaceError, match="symlink"):
            prepare_workspace(tmp_path / "ws", bundle)

    def test_rejects_reference_pdf_symlink(self, tmp_path: Path) -> None:
        source = _stage_pdf(tmp_path, "source.pdf")
        real = _stage_pdf(tmp_path, "real-ref.pdf")
        link = tmp_path / "link-ref.pdf"
        os.symlink(real, link)
        bundle = downloaded_bundle_with(
            source_pdf=str(source),
            reference_pdf=str(link),
        )
        with pytest.raises(WorkspaceError, match="symlink"):
            prepare_workspace(tmp_path / "ws", bundle)

    def test_rejects_relative_path_with_traversal(self, tmp_path: Path) -> None:
        # A malicious server could set reference_pdf to a relative path that
        # climbs out of the worker's CWD via '..'. Reject traversal so the
        # bundle cannot read arbitrary files by relative escape.
        source = _stage_pdf(tmp_path)
        bundle = downloaded_bundle_with(
            source_pdf=str(source),
            reference_pdf="../../etc/passwd",
        )
        with pytest.raises(WorkspaceError, match="escape"):
            prepare_workspace(tmp_path / "ws", bundle)

    def test_rejects_missing_source_pdf(self, tmp_path: Path) -> None:
        bundle = downloaded_bundle_with(
            source_pdf=str(tmp_path / "does-not-exist.pdf")
        )
        with pytest.raises(WorkspaceError, match="does not exist"):
            prepare_workspace(tmp_path / "ws", bundle)

    def test_rejects_missing_reference_pdf(self, tmp_path: Path) -> None:
        source = _stage_pdf(tmp_path)
        bundle = downloaded_bundle_with(
            source_pdf=str(source),
            reference_pdf=str(tmp_path / "missing-ref.pdf"),
        )
        with pytest.raises(WorkspaceError, match="does not exist"):
            prepare_workspace(tmp_path / "ws", bundle)


class TestWorkspaceCleanup:
    def test_cleanup_preserves_public_artifacts_until_commit(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)

        # Simulate a successful grading run: the runtime writes the public
        # artifacts and some private QA clutter.
        (workspace / "output").mkdir(parents=True, exist_ok=True)
        (workspace / "output/annotated.pdf").write_bytes(build_minimal_pdf(2))
        (workspace / "output/grading.json").write_text("{}", encoding="utf-8")
        (workspace / "manifest.json").write_text("{}", encoding="utf-8")
        qa_dir = workspace / "qa/final"
        qa_dir.mkdir(parents=True)
        (qa_dir / "render.png").write_bytes(b"png")
        latex_tmp = workspace / ".latex-build-001"
        latex_tmp.mkdir()
        (latex_tmp / "aux.log").write_text("x", encoding="utf-8")
        skill_copy = workspace / ".agents/skills/olympiad-grader/SKILL.md"
        assert skill_copy.is_file()

        cleanup_transient_artifacts(workspace)

        # Public artifacts must survive until the server commits.
        assert (workspace / "output/annotated.pdf").is_file()
        assert (workspace / "output/grading.json").is_file()
        assert (workspace / "manifest.json").is_file()
        # QA renders, latex temp dirs and skill copies are transient.
        assert not qa_dir.exists()
        assert not latex_tmp.exists()
        # The skill copy is removed once grading is done; the source stays in
        # the worker's own .agents directory for the next job.
        assert not skill_copy.exists()
        assert not (workspace / ".agents").exists()

    def test_cleanup_is_idempotent_on_missing_workspace(
        self, tmp_path: Path
    ) -> None:
        # The daemon always removes the workspace after commit; cleanup must
        # not raise if called after the workspace is already gone.
        cleanup_transient_artifacts(tmp_path / "does-not-exist")

    def test_cleanup_leaves_input_files_untouched(
        self, tmp_path: Path, downloaded_bundle: TaskBundle
    ) -> None:
        workspace = prepare_workspace(tmp_path / "ws", downloaded_bundle)
        cleanup_transient_artifacts(workspace)
        # Inputs remain so a retry could re-read them if needed.
        assert (workspace / "input/submission.pdf").is_file()
        assert (workspace / "input/instructions.txt").is_file()


def downloaded_bundle_with(**overrides) -> TaskBundle:
    """Build a bundle with sensible defaults plus overrides.

    Tests that need to mutate one field (e.g. set a symlink source) use this
    helper instead of the fixture so the failure is local to the test. When
    the test overrides source_pdf/reference_pdf it must stage the file
    itself before calling prepare_workspace.
    """
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
