from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import fitz
import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "olympiad-grader"
    / "scripts"
    / "build_annotated_pdf.py"
)


@pytest.fixture(scope="module")
def builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_annotated_pdf", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finding(kind: str = "correct") -> dict[str, object]:
    return {
        "id": 99,
        "kind": kind,
        "title": "检查点",
        "reason": "用于验证编号。",
        "deduction": 0,
        "bbox": [0.1, 0.1, 0.3, 0.2],
    }


def page(
    page_number: int,
    problem: str,
    kinds: tuple[str, ...],
) -> dict[str, object]:
    return {
        "page": page_number,
        "problem": problem,
        "score": 7,
        "max_score": 7,
        "page_summary": "已检查。",
        "findings": [finding(kind) for kind in kinds],
    }


def grading(pages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "title": "编号测试",
        "overall_summary": "已完成检查。",
        "problems": [
            {"label": "第 1 题", "summary": "第一题。"},
            {"label": "第 2 题", "summary": "第二题。"},
        ],
        "pages": pages,
    }


def source_document(page_count: int) -> fitz.Document:
    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    return document


def ids_by_page(resolved: dict[int, dict[str, object]]) -> dict[int, list[int]]:
    return {
        page_number: [item["id"] for item in page_data["_findings"]]
        for page_number, page_data in resolved.items()
    }


def test_numbering_is_per_problem_across_sorted_pages_and_all_kinds(
    builder: ModuleType,
) -> None:
    pages = [
        page(5, "第 １ 题", ("correct",)),
        page(2, "第2题", ("informational",)),
        page(4, "第1题", ("warning", "error")),
        page(1, "第 1 题", ("correct",)),
        page(3, "第 1 题", ("informational",)),
    ]
    with source_document(5) as source:
        resolved = builder.validate_and_resolve_grading(grading(pages), source)

    assert ids_by_page(resolved) == {
        1: [1],
        2: [1],
        3: [2],
        4: [3, 4],
        5: [5],
    }


def test_unknown_page_problem_is_rejected(builder: ModuleType) -> None:
    with source_document(1) as source, pytest.raises(
        ValueError, match=r"pages\[1\]\.problem.*does not match"
    ):
        builder.validate_and_resolve_grading(
            grading([page(1, "第 3 题", ("correct",))]), source
        )


def test_ambiguous_normalized_problem_labels_are_rejected(
    builder: ModuleType,
) -> None:
    payload = grading([page(1, "第 1 题", ("correct",))])
    payload["problems"] = [
        {"label": "第 1 题", "summary": "第一题。"},
        {"label": "第１题", "summary": "重复标签。"},
    ]
    with source_document(1) as source, pytest.raises(ValueError, match="ambiguous"):
        builder.validate_and_resolve_grading(payload, source)


@pytest.mark.parametrize("kind", ("correct", "informational"))
def test_positive_marker_has_one_number_and_no_frames(
    builder: ModuleType, kind: str
) -> None:
    rendered = builder.marker_tex(
        (10.0, 20.0, 100.0, 200.0),
        {
            "id": 4,
            "kind": kind,
            "_bboxes": [(0.1, 0.1, 0.3, 0.2), (0.4, 0.3, 0.6, 0.4)],
        },
    )

    assert rendered.count(r"\node[circle") == 1
    assert r"\draw[" not in rendered


@pytest.mark.parametrize("kind", ("warning", "error"))
def test_problem_marker_has_one_number_and_all_valid_frames(
    builder: ModuleType, kind: str
) -> None:
    rendered = builder.marker_tex(
        (10.0, 20.0, 100.0, 200.0),
        {
            "id": 4,
            "kind": kind,
            "_bboxes": [(0.1, 0.1, 0.3, 0.2), (0.4, 0.3, 0.6, 0.4)],
        },
    )

    assert rendered.count(r"\node[circle") == 1
    assert rendered.count(r"\draw[") == 2
