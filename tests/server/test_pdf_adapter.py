from __future__ import annotations

import ast
from pathlib import Path

import pytest

from server.adapters.pdf import PdfValidationError, inspect_pdf
from tests.server.conftest import make_encrypted_pdf_bytes, make_pdf_bytes


SERVER_ROOT = Path("server")


def _imported_top_level_modules(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_server_never_imports_the_legacy_app_package() -> None:
    """server/ must stand alone so app/ can move out of the repository."""
    offenders = {
        str(path): sorted(_imported_top_level_modules(path) & {"app"})
        for path in SERVER_ROOT.rglob("*.py")
        if _imported_top_level_modules(path) & {"app"}
    }

    assert offenders == {}


def test_inspect_pdf_accepts_a_valid_document(tmp_path: Path) -> None:
    path = tmp_path / "valid.pdf"
    path.write_bytes(make_pdf_bytes(2))

    info = inspect_pdf(path, max_pages=3)

    assert info.page_count == 2
    assert info.size_bytes == path.stat().st_size


def test_inspect_pdf_rejects_an_encrypted_document(tmp_path: Path) -> None:
    path = tmp_path / "encrypted.pdf"
    path.write_bytes(make_encrypted_pdf_bytes())

    with pytest.raises(PdfValidationError, match="加密"):
        inspect_pdf(path)


def test_inspect_pdf_enforces_the_page_limit(tmp_path: Path) -> None:
    path = tmp_path / "long.pdf"
    path.write_bytes(make_pdf_bytes(3))

    with pytest.raises(PdfValidationError, match="最多支持 2 页"):
        inspect_pdf(path, max_pages=2)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "为空"),
        (b"not a pdf at all", "不是有效的 PDF"),
        (b"%PDF-1.7truncated", "损坏"),
    ],
)
def test_inspect_pdf_rejects_unusable_payloads(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(payload)

    with pytest.raises(PdfValidationError, match=message):
        inspect_pdf(path)


def test_inspect_pdf_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PdfValidationError, match="没有找到"):
        inspect_pdf(tmp_path / "absent.pdf")
