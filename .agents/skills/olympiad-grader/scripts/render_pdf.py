#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import fitz


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render every PDF page to PNG")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--dpi", type=int, default=120)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(args.pdf)
    if document.needs_pass:
        raise SystemExit("Encrypted PDFs are not supported")
    matrix = fitz.Matrix(args.dpi / 72, args.dpi / 72)
    for index, page in enumerate(document):
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        pixmap.save(args.output_dir / f"page-{index + 1:03d}.png")
    print(f"rendered_pages={document.page_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

