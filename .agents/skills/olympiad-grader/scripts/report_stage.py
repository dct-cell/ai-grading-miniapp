#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path


STAGES = (
    "preparing",
    "understanding",
    "rubric",
    "decomposing",
    "verifying",
    "scoring",
    "auditing",
    "reporting",
    "validating",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atomically report the current olympiad grading stage."
    )
    parser.add_argument("stage", choices=STAGES)
    args = parser.parse_args()

    job_dir = Path.cwd().resolve()
    if not (job_dir / "input" / "submission.pdf").is_file() or not (
        job_dir / "config" / "grading-profile.json"
    ).is_file():
        parser.error("run this helper from the isolated grading job directory")

    internal_dir = job_dir / "output" / "internal"
    internal_dir.mkdir(parents=True, exist_ok=True)
    target = internal_dir / "progress.json"
    temporary = internal_dir / "progress.json.tmp"
    payload = {
        "stage": args.stage,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
