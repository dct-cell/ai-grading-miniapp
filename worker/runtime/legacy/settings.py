from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data" / "jobs"
    skill_source_dir: Path = PROJECT_ROOT / ".agents" / "skills" / "olympiad-grader"
    manifest_schema_path: Path = PROJECT_ROOT / "app" / "manifest.schema.json"
    codex_bin: str = "codex"
    runner_mode: str = "real"
    max_upload_bytes: int = 25 * 1024 * 1024
    max_input_pages: int = 30
    timeout_seconds: int = 60 * 60
    max_codex_attempts: int = 2
    retry_delay_seconds: float = 3.0
    max_concurrent_jobs: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = PROJECT_ROOT
        data_dir = Path(
            os.environ.get("AI_GRADER_DATA_DIR", str(project_root / "data" / "jobs"))
        ).expanduser()
        codex_bin = os.environ.get("AI_GRADER_CODEX_BIN") or shutil.which("codex") or "codex"
        max_concurrent_jobs = max(
            1, int(os.environ.get("AI_GRADER_MAX_CONCURRENT_JOBS", "2"))
        )
        return cls(
            project_root=project_root,
            data_dir=data_dir,
            skill_source_dir=project_root / ".agents" / "skills" / "olympiad-grader",
            manifest_schema_path=project_root / "app" / "manifest.schema.json",
            codex_bin=codex_bin,
            runner_mode=os.environ.get("AI_GRADER_RUNNER_MODE", "real").strip().lower(),
            timeout_seconds=int(os.environ.get("AI_GRADER_TIMEOUT_SECONDS", 60 * 60)),
            max_concurrent_jobs=max_concurrent_jobs,
        )
