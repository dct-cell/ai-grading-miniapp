from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from worker.runtime.contracts import TaskBundle

__all__ = [
    "WORKSPACE_LAYOUT",
    "WorkspaceError",
    "WorkspaceLayout",
    "cleanup_transient_artifacts",
    "prepare_workspace",
]


# The repository ships its own copy of the olympiad-grader skill at
# .agents/skills/olympiad-grader/. Phase 04 packs that copy into every
# workspace so the grading runtime sees the same SKILL.md, references,
# scripts and fonts the legacy runner expected.
# worker/runtime/workspace.py -> parents[0]=runtime, parents[1]=worker,
# parents[2]=repo root.
_SKILL_SOURCE = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "olympiad-grader"
)

@dataclass(frozen=True)
class WorkspaceLayout:
    """Relative paths the legacy runner reads from.

    These are a contract: the codex_runner, the manifest schema and the
    PDF builder all assume these exact locations. Renaming a field here
    without updating the legacy code will break grading.
    """

    source_pdf: str = "input/submission.pdf"
    reference_pdf: str = "input/reference.pdf"
    instructions: str = "input/instructions.txt"
    grading_profile: str = "config/grading-profile.json"
    manifest_schema: str = "config/manifest.schema.json"
    summary_grading_schema: str = "config/summary-grading.schema.json"
    annotated_grading_schema: str = "config/annotated-grading.schema.json"
    skill_dir: str = ".agents/skills/olympiad-grader"
    output_dir: str = "output"


WORKSPACE_LAYOUT = WorkspaceLayout()


# Files inside the skill directory that must always be present for the
# grader. Missing files mean the skill copy is corrupt and grading would
# fail opaquely later.
_REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "scripts/build_annotated_pdf.py",
    "scripts/build_summary_pdf.py",
    "scripts/render_pdf.py",
    "scripts/report_stage.py",
)


class WorkspaceError(RuntimeError):
    """Raised when a bundle path is unsafe or inputs are missing."""


def prepare_workspace(workspace_root: Path, bundle: TaskBundle) -> Path:
    """Materialise an isolated, legacy-compatible workspace.

    Returns the workspace path. The caller owns the workspace lifetime; use
    :func:`cleanup_transient_artifacts` to remove QA clutter after a
    successful run while keeping public artifacts for the server commit.
    """
    workspace = Path(workspace_root)
    workspace.mkdir(parents=True, exist_ok=True)

    _copy_pdf(
        bundle.source_pdf,
        workspace / WORKSPACE_LAYOUT.source_pdf,
        description="source PDF",
    )
    if bundle.reference_pdf is not None:
        _copy_pdf(
            bundle.reference_pdf,
            workspace / WORKSPACE_LAYOUT.reference_pdf,
            description="reference PDF",
        )
    _write_instructions(workspace, bundle.note)
    _write_grading_profile(workspace, bundle)
    _copy_manifest_schema(workspace)
    _copy_grading_schemas(workspace)
    _copy_skill(workspace)
    (workspace / WORKSPACE_LAYOUT.output_dir).mkdir(parents=True, exist_ok=True)
    return workspace


def cleanup_transient_artifacts(workspace: Path) -> None:
    """Remove QA renders, latex temp dirs and the per-job skill copy.

    Public artifacts (annotated.pdf, grading.json, manifest.json) stay until
    the server commits so a retry can re-read them. The skill copy is
    transient because it is rebuilt for every job from the worker's own
    .agents directory.
    """
    workspace = Path(workspace)
    if not workspace.is_dir():
        return

    qa = workspace / "qa"
    if qa.is_dir():
        shutil.rmtree(qa, ignore_errors=True)

    for entry in workspace.iterdir():
        if entry.name.startswith(".latex-build"):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    skill_copy = workspace / WORKSPACE_LAYOUT.skill_dir
    if skill_copy.is_dir():
        shutil.rmtree(skill_copy, ignore_errors=True)
    # Local one-shot runs retain the workspace after success. Remove only the
    # now-empty Skill parents so those runs do not leave a misleading
    # ``.agents/skills`` shell; rmdir is intentionally a no-op if another
    # workspace Skill is present.
    for parent in (skill_copy.parent, skill_copy.parent.parent):
        try:
            parent.rmdir()
        except OSError:
            pass


def _copy_pdf(bundle_path: str, dest: Path, *, description: str) -> None:
    """Copy a PDF referenced by the bundle into the workspace.

    Bundle paths are server-controlled identifiers that the Worker client
    stages locally before ``prepare_workspace`` runs. The path must not be a
    symlink or contain '..' so a malicious server cannot redirect the copy at
    an arbitrary file outside the staging area.
    """
    candidate = Path(bundle_path)
    if candidate.is_symlink():
        raise WorkspaceError(f"{description} is a symlink; refusing to follow")
    source = _resolve_bundle_path(bundle_path, description=description)
    if not source.is_file():
        raise WorkspaceError(f"{description} does not exist at {source}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)


def _resolve_bundle_path(bundle_path: str, *, description: str) -> Path:
    candidate = Path(bundle_path)
    # A malicious server could ship a relative path that climbs out of the
    # worker's CWD via '..'. Reject traversal regardless of absolute/relative
    # form so the bundle cannot escape its intended staging area.
    if ".." in candidate.parts:
        raise WorkspaceError(
            f"{description} path escapes its directory via '..'"
        )
    return candidate.resolve()


def _write_instructions(workspace: Path, note: str) -> None:
    instructions = workspace / WORKSPACE_LAYOUT.instructions
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text(note, encoding="utf-8")


def _write_grading_profile(workspace: Path, bundle: TaskBundle) -> None:
    profile = {
        "service_tier": bundle.service_tier,
        "service_tier_label": _SERVICE_TIER_LABELS[bundle.service_tier],
        "report_mode": _REPORT_MODES[bundle.service_tier],
        "grading_standard": bundle.grading_standard,
        "grading_standard_label": _STANDARD_LABELS[bundle.grading_standard],
        "league_scope": bundle.league_scope,
    }
    profile_path = workspace / WORKSPACE_LAYOUT.grading_profile
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_STANDARD_LABELS = {
    "league_second_round": "联赛二试",
    "cmo": "CMO",
    "imo": "IMO",
}

_SERVICE_TIER_LABELS = {
    "summary_report": "简明评分",
    "annotated_review": "逐页精批",
}

_REPORT_MODES = {
    "summary_report": "summary",
    "annotated_review": "annotated",
}


def _copy_manifest_schema(workspace: Path) -> None:
    legacy = (
        Path(__file__).resolve().parent / "legacy" / "manifest.schema.json"
    )
    if not legacy.is_file():
        raise WorkspaceError(
            "legacy manifest.schema.json missing from worker/runtime/legacy/"
        )
    dest = workspace / WORKSPACE_LAYOUT.manifest_schema
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(legacy, dest)


def _copy_grading_schemas(workspace: Path) -> None:
    runtime_dir = Path(__file__).resolve().parent
    for source_name, target_name in (
        ("summary-grading.schema.json", WORKSPACE_LAYOUT.summary_grading_schema),
        ("annotated-grading.schema.json", WORKSPACE_LAYOUT.annotated_grading_schema),
    ):
        source = runtime_dir / source_name
        if not source.is_file():
            raise WorkspaceError(f"runtime schema is missing: {source_name}")
        target = workspace / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _copy_skill(workspace: Path) -> None:
    if not _SKILL_SOURCE.is_dir():
        raise WorkspaceError(
            f"olympiad-grader skill missing from {_SKILL_SOURCE}"
        )
    dest = workspace / WORKSPACE_LAYOUT.skill_dir
    dest.parent.mkdir(parents=True, exist_ok=True)
    # The job sandbox is writable. Hard-linking trusted SKILL.md, rubrics or
    # builder scripts into it would let one compromised job mutate the inode
    # used by every later job. A private copy is intentionally required here.
    shutil.copytree(_SKILL_SOURCE, dest, copy_function=shutil.copy2)
    for required in _REQUIRED_SKILL_FILES:
        if not (dest / required).is_file():
            raise WorkspaceError(
                f"olympiad-grader skill is missing {required}"
            )
