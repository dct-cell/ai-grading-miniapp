from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .codex_runner import CodexRunError, validate_workspace


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate one completed olympiad grading workspace."
    )
    parser.add_argument("--job-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        manifest = validate_workspace(args.job_dir)
    except CodexRunError as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": error.code,
                    "detail": str(error)[:1000],
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(json.dumps({"ok": True, "manifest": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
