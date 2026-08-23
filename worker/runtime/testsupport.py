from __future__ import annotations

"""Shared test helpers for worker runtime tests.

Kept out of the public ``worker.runtime`` package surface so it is not
importable from production code. Tests import via ``worker.runtime.testsupport``.
"""

_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842


def build_minimal_pdf(page_count: int) -> bytes:
    """Emit a real, xref-complete PDF with the requested page count.

    pypdf (and therefore the server's ``inspect_pdf``) rejects a PDF without a
    cross-reference table, so the bytes are assembled properly rather than
    hand-waved.

    Note: this PDF is minimal enough for pypdf to parse but XeLaTeX's image
    inclusion may reject it. Use :func:`build_renderable_pdf` for tests that
    feed the PDF through the XeLaTeX report builder.
    """
    if page_count < 1:
        raise ValueError("page_count must be >= 1")

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


def build_renderable_pdf(page_count: int) -> bytes:
    """Emit a PDF that XeLaTeX can include as an image.

    Uses reportlab to produce a structurally complete PDF that the
    ``build_annotated_pdf.py`` script accepts via ``\\includegraphics``.
    Falls back to :func:`build_minimal_pdf` if reportlab is unavailable
    (e.g. production without dev extras).
    """
    if page_count < 1:
        raise ValueError("page_count must be >= 1")
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:  # pragma: no cover
        return build_minimal_pdf(page_count)

    from io import BytesIO

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for index in range(page_count):
        c.drawString(72, A4[1] - 72, f"Submission page {index + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()
