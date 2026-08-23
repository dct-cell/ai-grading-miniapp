from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from worker.runtime.contracts import RuntimeResult, TaskBundle
from worker.runtime.testsupport import build_minimal_pdf

__all__ = ["FakeGrader"]


_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842


def _build_pdf(page_count: int) -> bytes:
    """Emit a real, xref-complete PDF.

    pypdf (and therefore the server's inspect_pdf) rejects a PDF without a
    cross-reference table, so the bytes are assembled properly rather than
    hand-waved. The page count mirrors the grading contract: one annotated page
    per input page plus a summary page.
    """
    objects: list[bytes] = []
    page_object_numbers = [3 + index for index in range(page_count)]
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)

    objects.append(b"<</Type/Catalog/Pages 2 0 R>>")
    objects.append(
        f"<</Type/Pages/Kids[{kids}]/Count {page_count}>>".encode("ascii")
    )
    for _ in page_object_numbers:
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}]>>".encode(
                "ascii"
            )
        )

    body = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for index, payload in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n".encode("ascii") + payload + b"\nendobj\n"

    xref_offset = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    body += b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode("ascii")
    body += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n"
        f"{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(body)


class FakeGrader:
    """Deterministic stand-in runtime used until Phase 04 lands.

    It produces the two artefacts the protocol requires without invoking Codex
    or XeLaTeX, so the control plane can be exercised end to end for free. The
    student note is copied nowhere near the score: it is untrusted input and
    must not influence grading.
    """

    async def run(
        self,
        workspace: Path,
        bundle: TaskBundle,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> RuntimeResult:
        if progress is not None:
            await progress("preparing")
            await progress("reporting")
            await progress("validating")

        if bundle.grading_standard == "imo":
            maximum = 7
        elif bundle.grading_standard == "cmo":
            maximum = 21
        else:
            maximum = 40
        resolved_scope = (
            "problem_set"
            if bundle.grading_standard == "league_second_round"
            else None
        )
        output_page_count = (
            1 if bundle.service_tier == "summary_report" else bundle.page_count + 1
        )
        common = {
            "service_tier": bundle.service_tier,
            "grading_standard": bundle.grading_standard,
            "resolved_league_scope": resolved_scope,
            "title": "数学竞赛题批改（演示）",
            "total_score": 0,
            "max_score": maximum,
        }
        if bundle.service_tier == "summary_report":
            grading = {
                **common,
                "problems": [
                    {
                        "label": "演示题 1",
                        "score": 0,
                        "max_score": maximum,
                        "verdict": "演示模式仅确认简明评分报告生成链路可用，不作真实数学判断，因此未授予评分点。",
                        "issues": [
                            {
                                "title": "演示模式",
                                "reason": "测试结果不代表真实数学评分。",
                                "deduction": maximum,
                            }
                        ],
                    }
                ],
            }
        else:
            grading = {
                **common,
                "overall_summary": "演示模式只验证本地上传、排版与下载流程，不作真实数学判断。",
                "problems": [
                {
                    "label": "演示题 1",
                    "score": 0,
                    "max_score": maximum,
                    "summary": "PDF 生成链路运行正常。",
                }
                ],
                "pages": [
                {
                    "page": page_number,
                    "problem": "演示题 · 原稿第 %d 页" % page_number,
                    "score": 0,
                    "max_score": maximum,
                    "page_summary": "本页用于验证原稿清晰度、中文字体与报告排版。",
                    "findings": [],
                }
                for page_number in range(1, bundle.page_count + 1)
                ],
            }
        json_bytes = json.dumps(grading, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        json_path = workspace / "grading.json"
        json_path.write_bytes(json_bytes)

        pdf_bytes = _build_pdf(output_page_count)
        output_name = (
            "report.pdf" if bundle.service_tier == "summary_report" else "annotated.pdf"
        )
        pdf_path = workspace / output_name
        pdf_path.write_bytes(pdf_bytes)

        manifest = {
            "output_pdf": f"output/{output_name}",
            "page_count": output_page_count,
            "summary": "演示模式已生成逐页批改报告",
            "score": 0,
            "max_score": maximum,
            "service_tier": bundle.service_tier,
            "grading_standard": bundle.grading_standard,
            "resolved_league_scope": resolved_scope,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        manifest_path = workspace / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)

        return RuntimeResult(
            manifest_path=manifest_path,
            result_json_path=json_path,
            result_pdf_path=pdf_path,
            result_json_sha256=hashlib.sha256(json_bytes).hexdigest(),
            result_pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            output_page_count=output_page_count,
        )
