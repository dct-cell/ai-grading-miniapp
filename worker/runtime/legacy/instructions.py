from __future__ import annotations

import os
from pathlib import Path


MAX_INSTRUCTIONS_CHARS = 2000


class InstructionsValidationError(ValueError):
    pass


def normalize_instructions(value: str) -> str:
    if not isinstance(value, str):
        raise InstructionsValidationError("补充说明必须是文字。")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > MAX_INSTRUCTIONS_CHARS:
        raise InstructionsValidationError(
            f"补充说明最多 {MAX_INSTRUCTIONS_CHARS} 个字符。"
        )
    return normalized


def read_instructions(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return normalize_instructions(value)


def write_instructions(path: Path, value: str) -> str:
    normalized = normalize_instructions(value)
    if not normalized:
        path.unlink(missing_ok=True)
        return ""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(normalized + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return normalized


def instructions_preview(value: str, *, limit: int = 96) -> str | None:
    compact = " ".join(value.split())
    if not compact:
        return None
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
