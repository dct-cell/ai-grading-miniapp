#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import fitz
from jsonschema import Draft202012Validator

from build_annotated_pdf import (
    FONT_FILES,
    find_executable,
    load_profile,
    mixed_tex,
    text,
    validate_grading_profile,
)


A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an A4 XeLaTeX score report")
    parser.add_argument("--grading", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def load_json(path: Path, description: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must be a JSON object")
    return payload


def standard_label(profile: dict[str, Any], grading: dict[str, Any]) -> str:
    standard = profile["grading_standard"]
    if standard == "imo":
        return "IMO 7 分制"
    if standard == "cmo":
        return "CMO 21 分制"
    if grading["resolved_league_scope"] == "full_paper":
        return "联赛二试 · 整卷 180 分"
    return "联赛二试 · 题组每题 40 分"


def tex(value: Any, field_name: str, fallback: str = "") -> str:
    return mixed_tex(value, fallback, field_name=field_name)


def problem_tex(problem: dict[str, Any], index: int) -> str:
    issues = problem["issues"]
    issue_block = ""
    if issues:
        rows = []
        for issue_index, issue in enumerate(issues, start=1):
            deduction = issue["deduction"]
            deduction_text = f"（扣 {deduction} 分）" if deduction else ""
            rows.append(
                r"\item \textbf{" + tex(issue["title"], f"problems[{index}].issues[{issue_index}].title") + "}"
                + tex(deduction_text, f"problems[{index}].issues[{issue_index}].deduction")
                + r"\\[-1mm]"
                + tex(issue["reason"], f"problems[{index}].issues[{issue_index}].reason")
            )
        issue_block = (
            r"\vspace{2mm}{\sffamily\bfseries\color{rust} 主要问题}\par"
            r"\begin{enumerate}[leftmargin=6mm,itemsep=1.5mm,topsep=1mm]"
            + "".join(rows)
            + r"\end{enumerate}"
        )
    else:
        issue_block = (
            r"\vspace{2mm}{\sffamily\bfseries\color{green} 主要问题}\par"
            r"{\sffamily\color{muted} 无}\par"
        )
    return rf"""
\Needspace{{10\baselineskip}}
\begin{{tcolorbox}}[problem]
  {{\sffamily\bfseries\fontsize{{15}}{{19}}\selectfont
    \strut {tex(problem['label'], f'problems[{index}].label')}
    \hfill
    \textcolor{{green}}{{\strut {problem['score']} / {problem['max_score']}}}
  }}\par
  \vspace{{2mm}}\par
  {{\sffamily\bfseries 判断：}}{tex(problem['verdict'], f'problems[{index}].verdict')}\par
  {issue_block}
\end{{tcolorbox}}
"""


def build_tex(grading: dict[str, Any], profile: dict[str, Any]) -> str:
    label = standard_label(profile, grading)
    problem_blocks = "".join(
        problem_tex(problem, index)
        for index, problem in enumerate(grading["problems"], start=1)
    )
    return rf"""\documentclass[10.5pt]{{article}}
\usepackage[a4paper,top=18mm,bottom=17mm,left=18mm,right=18mm,headheight=12pt]{{geometry}}
\usepackage{{fontspec,xeCJK,amsmath,amssymb,mathtools,xcolor}}
\usepackage[most]{{tcolorbox}}
\usepackage{{enumitem,needspace,fancyhdr}}
\defaultfontfeatures{{Ligatures=TeX}}
\setmainfont[Path={{fonts/}},BoldFont={{NotoSansCJKsc-Medium.otf}}]{{NotoSerifCJKsc-Regular.otf}}
\setCJKmainfont[Path={{fonts/}},BoldFont={{NotoSansCJKsc-Medium.otf}}]{{NotoSerifCJKsc-Regular.otf}}
\setsansfont[Path={{fonts/}},BoldFont={{NotoSansCJKsc-Medium.otf}}]{{NotoSansCJKsc-Medium.otf}}
\setCJKsansfont[Path={{fonts/}},BoldFont={{NotoSansCJKsc-Medium.otf}}]{{NotoSansCJKsc-Medium.otf}}
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip=0pt plus 1pt
\definecolor{{paper}}{{HTML}}{{FEFDF9}}
\definecolor{{ink}}{{HTML}}{{202320}}
\definecolor{{muted}}{{HTML}}{{737773}}
\definecolor{{green}}{{HTML}}{{285A4A}}
\definecolor{{pale}}{{HTML}}{{E8F0EB}}
\definecolor{{divider}}{{HTML}}{{D8D3CA}}
\definecolor{{rust}}{{HTML}}{{A64F3F}}
\pagecolor{{paper}}\color{{ink}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{1.2mm}}
\linespread{{1.17}}
\sloppy
\pagestyle{{fancy}}\fancyhf{{}}
\renewcommand{{\headrulewidth}}{{0pt}}
\fancyfoot[L]{{\sffamily\fontsize{{8}}{{10}}\selectfont\color{{muted}} 数学竞赛题批改 · {tex(label, 'standard_label')}}}
\fancyfoot[R]{{\sffamily\fontsize{{8}}{{10}}\selectfont\color{{muted}} 第 \thepage\ 页}}
\tcbset{{
  summary/.style={{colback=pale,colframe=pale,boxrule=0pt,arc=3mm,left=5mm,right=5mm,top=4mm,bottom=4mm}},
  problem/.style={{colback=paper,colframe=divider,boxrule=.35pt,arc=2.5mm,left=5mm,right=5mm,top=4mm,bottom=3.5mm,before skip=4mm,after skip=0mm,breakable}}
}}
\begin{{document}}
{{\sffamily\fontsize{{10}}{{12}}\selectfont\color{{green}} 数学竞赛题批改\hfill 简明评分}}\par
\vspace{{8mm}}
{{\centering\sffamily\bfseries\fontsize{{24}}{{29}}\selectfont 评分报告\par}}
\vspace{{2mm}}
{{\sffamily\color{{muted}} 赛制：{tex(label, 'standard_label')}\hfill 日期：{date.today().isoformat()}}}\par
\vspace{{5mm}}
\begin{{tcolorbox}}[summary]
  \begin{{center}}
    {{\sffamily\bfseries\fontsize{{22}}{{27}}\selectfont\color{{green}}
      总分：{grading['total_score']}/{grading['max_score']}}}
  \end{{center}}
\end{{tcolorbox}}
\vspace{{5mm}}
{problem_blocks}
\end{{document}}
"""


def compile_pdf(build_dir: Path) -> Path:
    xelatex = find_executable("xelatex", "/Library/TeX/texbin/xelatex")
    environment = os.environ.copy()
    environment.update(
        {"openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": str(build_dir)}
    )
    command = [
        str(xelatex), "-interaction=nonstopmode", "-halt-on-error",
        "-file-line-error", "-no-shell-escape", "-jobname=report", "report.tex",
    ]
    for _ in range(2):
        result = subprocess.run(
            command, cwd=build_dir, env=environment, check=False,
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-40:])
            raise RuntimeError(f"XeLaTeX compilation failed:\n{tail}")
    output = build_dir / "report.pdf"
    if not output.is_file():
        raise RuntimeError("XeLaTeX completed without creating report.pdf")
    return output


def main() -> int:
    args = parse_args()
    grading = load_json(args.grading, "grading")
    profile = load_profile(args.profile)
    schema = load_json(args.schema, "summary schema")
    errors = sorted(Draft202012Validator(schema).iter_errors(grading), key=lambda e: list(e.path))
    if errors:
        raise ValueError("summary grading schema failed: " + errors[0].message)
    if profile.get("service_tier") != "summary_report":
        raise ValueError("summary builder requires summary_report profile")
    validate_grading_profile(grading, profile)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    build_dir = Path(tempfile.mkdtemp(prefix=".latex-build-summary-", dir=args.output.parent))
    succeeded = False
    try:
        font_source = Path(__file__).resolve().parents[1] / "assets" / "fonts"
        for filename in FONT_FILES:
            target = build_dir / "fonts" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(font_source / filename, target)
        (build_dir / "report.tex").write_text(build_tex(grading, profile), encoding="utf-8")
        compiled = compile_pdf(build_dir)
        document = fitz.open(compiled)
        try:
            if document.page_count < 1:
                raise RuntimeError("summary report is blank")
            for page_number, page in enumerate(document, start=1):
                if abs(page.rect.width - A4_WIDTH_PT) > 0.5 or abs(page.rect.height - A4_HEIGHT_PT) > 0.5:
                    raise RuntimeError(f"page {page_number} is not A4")
                if not page.get_contents():
                    raise RuntimeError(f"page {page_number} is blank")
            document.set_metadata({
                "title": text(grading.get("title"), "数学竞赛题批改"),
                "author": "数学竞赛题批改",
                "subject": "数学竞赛题简明评分报告",
                "creator": "数学竞赛题批改",
            })
            temporary = args.output.with_name(args.output.stem + ".tmp.pdf")
            document.save(temporary, garbage=4, deflate=True, clean=True)
            os.replace(temporary, args.output)
            print(f"output={args.output} pages={document.page_count} engine=xelatex")
        finally:
            document.close()
        succeeded = True
        return 0
    finally:
        if succeeded:
            shutil.rmtree(build_dir, ignore_errors=True)
        else:
            print(f"XeLaTeX build files kept at: {build_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
