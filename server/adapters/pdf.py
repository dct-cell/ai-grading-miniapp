from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class PdfValidationError(ValueError):
    """An uploaded PDF cannot be accepted."""


@dataclass(frozen=True, slots=True)
class PdfInfo:
    page_count: int
    size_bytes: int


def inspect_pdf(path: Path, *, max_pages: int | None = None) -> PdfInfo:
    """Validate an untrusted PDF and report its real page count and size.

    The rules mirror the verified legacy grader so that quoting and grading
    agree on what counts as an acceptable submission.
    """
    if not path.is_file():
        raise PdfValidationError("没有找到 PDF 文件。")
    size = path.stat().st_size
    if size < 5:
        raise PdfValidationError("PDF 文件为空或不完整。")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PdfValidationError("文件内容不是有效的 PDF。")
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise PdfValidationError("暂不支持加密或带密码的 PDF。")
        page_count = len(reader.pages)
    except PdfValidationError:
        raise
    except (PdfReadError, OSError, ValueError) as exc:
        raise PdfValidationError("PDF 已损坏或无法读取。") from exc

    if page_count < 1:
        raise PdfValidationError("PDF 中没有可批改的页面。")
    if max_pages is not None and page_count > max_pages:
        raise PdfValidationError(f"PDF 最多支持 {max_pages} 页。")
    return PdfInfo(page_count=page_count, size_bytes=size)
