#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import fitz


PAGE_WIDTH_MM = 420.0
PAGE_HEIGHT_MM = 297.0
PAGE_WIDTH_PT = 1190.55
PAGE_HEIGHT_PT = 841.89
SOURCE_AREA = (12.0, 25.5, 205.0, 282.0)
RIGHT_X = 218.0
RIGHT_WIDTH = 190.0

FONT_FILES = (
    "NotoSansCJKsc-Medium.otf",
    "NotoSerifCJKsc-Regular.otf",
)

STYLES = {
    "correct": {
        "accent": "correct",
        "fill": "correctfill",
        "head": "correcthead",
        "label": "得分点",
    },
    "warning": {
        "accent": "warning",
        "fill": "warningfill",
        "head": "warninghead",
        "label": "需核对",
    },
    "error": {
        "accent": "error",
        "fill": "errorfill",
        "head": "errorhead",
        "label": "扣分点",
    },
    "informational": {
        "accent": "info",
        "fill": "infofill",
        "head": "infohead",
        "label": "补充说明",
    },
}

SAFE_MATH_ENVIRONMENTS = frozenset(
    {
        "aligned",
        "alignedat",
        "gathered",
        "cases",
        "dcases",
        "matrix",
        "pmatrix",
        "bmatrix",
        "Bmatrix",
        "vmatrix",
        "Vmatrix",
        "smallmatrix",
    }
)

# Formulas compile in a throw-away directory with shell escape disabled and
# paranoid TeX file-access settings. Ordinary AMS math commands stay open; only
# commands that can read/write files, redefine TeX, load code, or create an
# unbounded expansion are rejected here.
DANGEROUS_TEX_COMMANDS = frozenset(
    {
        "input",
        "include",
        "import",
        "subimport",
        "usepackage",
        "requirepackage",
        "documentclass",
        "loadclass",
        "openin",
        "closein",
        "read",
        "readline",
        "scantokens",
        "openout",
        "closeout",
        "write",
        "immediate",
        "special",
        "includegraphics",
        "graphicspath",
        "href",
        "url",
        "newcommand",
        "renewcommand",
        "providecommand",
        "newenvironment",
        "renewenvironment",
        "def",
        "gdef",
        "edef",
        "xdef",
        "let",
        "futurelet",
        "csname",
        "endcsname",
        "catcode",
        "mathcode",
        "lccode",
        "uccode",
        "delcode",
        "everyjob",
        "everypar",
        "everymath",
        "everydisplay",
        "newread",
        "newwrite",
        "newcount",
        "newdimen",
        "newbox",
        "toks",
        "expandafter",
        "noexpand",
        "romannumeral",
        "loop",
        "repeat",
        "whiledo",
        "foreach",
        "jobname",
        "meaning",
        "show",
        "message",
        "errmessage",
        "typeout",
        "verb",
        "endinput",
    }
)
DANGEROUS_TEX_PREFIXES = ("pdf", "xetex", "luatex", "directlua")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an annotated XeLaTeX grading PDF")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--grading", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned or fallback


def problem_label_key(value: Any) -> str:
    """Return the comparison key used to join page labels to problem labels."""
    return "".join(unicodedata.normalize("NFKC", text(value)).split())


def number(value: Any, fallback: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


GRADING_STANDARDS = frozenset({"imo", "cmo", "league_second_round"})
LEAGUE_SCOPES = frozenset({"auto", "full_paper", "problem_set"})


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{description} JSON must be an object")
    return payload


def load_profile(path: Path) -> dict[str, Any]:
    profile = load_json_object(path, "grading profile")
    standard = profile.get("grading_standard")
    if standard not in GRADING_STANDARDS:
        raise ValueError("grading profile has an unsupported grading_standard")
    scope = profile.get("league_scope")
    if standard == "league_second_round":
        if scope not in LEAGUE_SCOPES:
            raise ValueError("League grading profile has an invalid league_scope")
    elif scope is not None:
        raise ValueError("only a League grading profile may set league_scope")
    return profile


def _integer_score(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    numeric = float(value)
    if not numeric.is_integer():
        raise ValueError(f"{field_name} must be an integer")
    return int(numeric)


def grading_footer(profile: dict[str, Any], grading: dict[str, Any]) -> str:
    standard = profile["grading_standard"]
    if standard == "imo":
        return "数学竞赛题批改 · IMO 7 分制"
    if standard == "cmo":
        return "数学竞赛题批改 · CMO 21 分制"
    scope = grading["resolved_league_scope"]
    if scope == "full_paper":
        return "数学竞赛题批改 · 联赛二试 · 整卷 180 分"
    return "数学竞赛题批改 · 联赛二试 · 题组每题 40 分"


def validate_grading_profile(
    grading: dict[str, Any], profile: dict[str, Any]
) -> None:
    standard = profile["grading_standard"]
    if grading.get("grading_standard") != standard:
        raise ValueError("grading_standard does not match the trusted profile")

    resolved_scope = grading.get("resolved_league_scope")
    if standard == "league_second_round":
        if resolved_scope not in {"full_paper", "problem_set"}:
            raise ValueError("League grading must resolve to full_paper or problem_set")
        requested_scope = profile["league_scope"]
        if requested_scope != "auto" and resolved_scope != requested_scope:
            raise ValueError("resolved League scope does not match the trusted profile")
    elif resolved_scope is not None:
        raise ValueError("non-League grading must use a null resolved_league_scope")

    problems = grading.get("problems")
    if not isinstance(problems, list) or not problems:
        raise ValueError("problems must be a non-empty list")

    if standard == "imo":
        expected_maxima = [7] * len(problems)
        increment = 1
    elif standard == "cmo":
        expected_maxima = [21] * len(problems)
        increment = 3
    elif resolved_scope == "full_paper":
        if len(problems) != 4:
            raise ValueError("a full League paper must contain exactly four problems")
        expected_maxima = [40, 40, 50, 50]
        increment = 10
    else:
        expected_maxima = [40] * len(problems)
        increment = 10

    scores: list[int] = []
    maxima: list[int] = []
    for index, (problem, expected_maximum) in enumerate(
        zip(problems, expected_maxima, strict=True), start=1
    ):
        if not isinstance(problem, dict):
            raise ValueError(f"problems[{index}] must be an object")
        score = _integer_score(problem.get("score"), f"problems[{index}].score")
        maximum = _integer_score(
            problem.get("max_score"), f"problems[{index}].max_score"
        )
        if maximum != expected_maximum:
            raise ValueError(
                f"problems[{index}].max_score must be {expected_maximum}"
            )
        if not 0 <= score <= maximum or score % increment:
            raise ValueError(
                f"problems[{index}].score is outside the selected score bands"
            )
        scores.append(score)
        maxima.append(maximum)

    total_score = _integer_score(grading.get("total_score"), "total_score")
    max_score = _integer_score(grading.get("max_score"), "max_score")
    if total_score != sum(scores) or max_score != sum(maxima):
        raise ValueError("grading totals do not equal the sum of problem scores")

    pages = grading.get("pages")
    if pages is not None and not isinstance(pages, list):
        raise ValueError("pages must be a list")
    allowed_page_maxima = set(expected_maxima)
    for index, page in enumerate(pages or [], start=1):
        if not isinstance(page, dict):
            raise ValueError(f"pages[{index}] must be an object")
        page_score = _integer_score(page.get("score"), f"pages[{index}].score")
        page_maximum = _integer_score(
            page.get("max_score"), f"pages[{index}].max_score"
        )
        if page_maximum not in allowed_page_maxima:
            raise ValueError(f"pages[{index}].max_score does not match the profile")
        if not 0 <= page_score <= page_maximum or page_score % increment:
            raise ValueError(f"pages[{index}].score is outside the selected score bands")


def score_text(score: Any, maximum: Any) -> str:
    return f"{number(score):g} / {number(maximum, 7):g}"


def escape_tex_plain(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


def validate_math(value: str, field_name: str) -> str:
    formula = value.strip()
    if not formula:
        raise ValueError(f"{field_name} contains an empty TeX formula")
    if len(formula) > 2000:
        raise ValueError(
            f"{field_name} contains a TeX formula longer than 2000 characters"
        )
    if any(character in formula for character in ("$", "%", "#")) or "^^" in formula:
        raise ValueError(f"{field_name} contains an unsupported TeX control character")

    for character in formula:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if category == "Cc":
            raise ValueError(f"{field_name} contains a TeX control character")
        if (
            (codepoint >= 128 and category == "Sm")
            or 0x0370 <= codepoint <= 0x03FF
            or 0x1F00 <= codepoint <= 0x1FFF
            or 0x1D400 <= codepoint <= 0x1D7FF
        ):
            raise ValueError(
                f"{field_name} contains a raw Unicode math symbol; "
                "use traditional TeX commands"
            )

    depth = 0
    for index, character in enumerate(formula):
        if character not in "{}":
            continue
        preceding_slashes = 0
        cursor = index - 1
        while cursor >= 0 and formula[cursor] == "\\":
            preceding_slashes += 1
            cursor -= 1
        if preceding_slashes % 2:
            continue
        if character == "{":
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                raise ValueError(f"{field_name} contains unbalanced TeX braces")
    if depth:
        raise ValueError(f"{field_name} contains unbalanced TeX braces")

    environment_stack: list[str] = []
    environment_events = list(
        re.finditer(r"\\(begin|end)\s*\{([A-Za-z*]+)\}", formula)
    )
    begin_end_commands = re.findall(r"\\(?:begin|end)\b", formula)
    if len(environment_events) != len(begin_end_commands):
        raise ValueError(f"{field_name} contains a malformed TeX environment")
    for match in environment_events:
        action, environment = match.groups()
        if environment not in SAFE_MATH_ENVIRONMENTS:
            raise ValueError(
                f"{field_name} uses unsupported TeX environment {environment}"
            )
        if action == "begin":
            environment_stack.append(environment)
        elif not environment_stack or environment_stack.pop() != environment:
            raise ValueError(f"{field_name} contains mismatched TeX environments")
    if environment_stack:
        raise ValueError(f"{field_name} contains an unclosed TeX environment")

    for match in re.finditer(r"\\([A-Za-z@]+|.)", formula):
        command = match.group(1)
        if "@" in command:
            raise ValueError(f"{field_name} uses an unsafe internal TeX command")
        if command.isalpha():
            lowered = command.lower()
            if lowered in DANGEROUS_TEX_COMMANDS or lowered.startswith(
                DANGEROUS_TEX_PREFIXES
            ):
                raise ValueError(
                    f"{field_name} uses unsafe TeX command \\{command}"
                )
        elif command in ("[", "]", "(", ")"):
            raise ValueError(
                f"{field_name} must not add its own TeX math delimiters"
            )
    return formula


def mixed_tex(value: Any, fallback: str = "", *, field_name: str) -> str:
    raw = text(value, fallback)
    parts = raw.split("$")
    if len(parts) % 2 == 0:
        raise ValueError(f"{field_name} contains unmatched $ delimiters")
    rendered: list[str] = []
    for index, part in enumerate(parts):
        if index % 2:
            rendered.append(f"${validate_math(part, field_name)}$")
        else:
            rendered.append(escape_tex_plain(part))
    return "".join(rendered)


def display_tex(value: Any, *, field_name: str) -> str:
    formula = text(value)
    if formula.startswith("$") and formula.endswith("$") and formula.count("$") == 2:
        formula = formula[1:-1]
    return validate_math(formula, field_name)


def normalized_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(0 <= item <= 1 for item in (x0, y0, x1, y1)):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def explicit_bboxes(finding: dict[str, Any]) -> list[tuple[float, float, float, float]]:
    values = finding.get("bboxes")
    if isinstance(values, list) and values and isinstance(values[0], (list, tuple)):
        candidates = values
    elif finding.get("bbox") is not None:
        candidates = [finding.get("bbox")]
    else:
        candidates = []
    boxes: list[tuple[float, float, float, float]] = []
    for candidate in candidates:
        box = normalized_bbox(candidate)
        if box is None:
            raise ValueError("finding contains an invalid normalized bbox")
        boxes.append(box)
    return boxes


def quote_bbox(
    page: fitz.Page, finding: dict[str, Any]
) -> list[tuple[float, float, float, float]]:
    quote = text(finding.get("source_quote"))
    if not quote:
        return []
    matches = sorted(page.search_for(quote), key=lambda rect: (rect.y0, rect.x0))
    if not matches:
        return []
    lines: list[fitz.Rect] = []
    for match in matches:
        rect = fitz.Rect(match)
        if lines:
            previous = lines[-1]
            vertical_overlap = min(previous.y1, rect.y1) - max(previous.y0, rect.y0)
            close_center = abs(
                (previous.y0 + previous.y1) / 2 - (rect.y0 + rect.y1) / 2
            ) <= max(previous.height, rect.height) * 0.65
            if vertical_overlap > 0 or close_center:
                lines[-1] = previous | rect
                continue
        lines.append(rect)
    for previous, current in zip(lines, lines[1:]):
        if current.y0 - previous.y1 > max(previous.height, current.height) * 2.5:
            return []
    page_rect = page.rect
    padding = 2.0
    boxes: list[tuple[float, float, float, float]] = []
    for rect in lines:
        rect.x0 = max(page_rect.x0, rect.x0 - padding)
        rect.y0 = max(page_rect.y0, rect.y0 - padding)
        rect.x1 = min(page_rect.x1, rect.x1 + padding)
        rect.y1 = min(page_rect.y1, rect.y1 + padding)
        boxes.append(
            (
                (rect.x0 - page_rect.x0) / page_rect.width,
                (rect.y0 - page_rect.y0) / page_rect.height,
                (rect.x1 - page_rect.x0) / page_rect.width,
                (rect.y1 - page_rect.y0) / page_rect.height,
            )
        )
    return boxes


def resolve_finding_bboxes(
    page: fitz.Page, finding: dict[str, Any], *, field_name: str
) -> list[tuple[float, float, float, float]]:
    boxes = quote_bbox(page, finding) or explicit_bboxes(finding)
    if not boxes:
        raise ValueError(f"{field_name} must include source_quote, bbox, or bboxes")
    return boxes


def load_grading(path: Path) -> dict[str, Any]:
    return load_json_object(path, "grading")


def validate_and_resolve_grading(
    grading: dict[str, Any], source: fitz.Document
) -> dict[int, dict[str, Any]]:
    mixed_tex(grading.get("title"), "数学竞赛题批改", field_name="title")
    mixed_tex(
        grading.get("overall_summary"),
        "完成逐页检查。",
        field_name="overall_summary",
    )
    problems = grading.get("problems")
    if problems is not None and not isinstance(problems, list):
        raise ValueError("problems must be a list")
    problem_labels: dict[str, str] = {}
    for index, problem in enumerate(problems or [], start=1):
        if not isinstance(problem, dict):
            raise ValueError(f"problems[{index}] must be an object")
        label = text(problem.get("label"), f"第 {index} 题")
        mixed_tex(label, field_name=f"problems[{index}].label")
        label_key = problem_label_key(label)
        if label_key in problem_labels:
            raise ValueError(
                "problem labels are ambiguous after NFKC and whitespace normalization: "
                f"{problem_labels[label_key]!r} and {label!r}"
            )
        problem_labels[label_key] = label
        mixed_tex(
            problem.get("summary"),
            "已完成检查。",
            field_name=f"problems[{index}].summary",
        )

    pages = grading.get("pages")
    if pages is None:
        pages = []
    if not isinstance(pages, list):
        raise ValueError("pages must be a list")
    page_map: dict[int, dict[str, Any]] = {}
    for page_position, page_data in enumerate(pages, start=1):
        if not isinstance(page_data, dict):
            raise ValueError(f"pages[{page_position}] must be an object")
        page_number = int(number(page_data.get("page"), 0))
        if page_number < 1 or page_number > source.page_count:
            raise ValueError(f"pages[{page_position}].page is outside the input PDF")
        if page_number in page_map:
            raise ValueError(f"input page {page_number} appears more than once")
        page_problem = text(page_data.get("problem"))
        mixed_tex(page_problem, field_name=f"pages[{page_position}].problem")
        page_problem_key = problem_label_key(page_problem)
        if page_problem_key not in problem_labels:
            raise ValueError(
                f"pages[{page_position}].problem {page_problem!r} does not match "
                "exactly one problems[].label after NFKC and whitespace normalization"
            )
        for optional_field in ("page_summary",):
            if page_data.get(optional_field):
                mixed_tex(
                    page_data.get(optional_field),
                    field_name=f"pages[{page_position}].{optional_field}",
                )
        findings = page_data.get("findings") or []
        if not isinstance(findings, list):
            raise ValueError(f"pages[{page_position}].findings must be a list")
        resolved_findings: list[dict[str, Any]] = []
        for finding_position, finding in enumerate(findings, start=1):
            field_name = f"pages[{page_position}].findings[{finding_position}]"
            if not isinstance(finding, dict):
                raise ValueError(f"{field_name} must be an object")
            kind = text(finding.get("kind"), "warning")
            if kind not in STYLES:
                raise ValueError(
                    f"{field_name}.kind must be correct, informational, warning, or error"
                )
            mixed_tex(
                finding.get("title"),
                "批改意见",
                field_name=f"{field_name}.title",
            )
            mixed_tex(
                finding.get("reason"),
                "请核对此处论证。",
                field_name=f"{field_name}.reason",
            )
            if finding.get("suggestion"):
                mixed_tex(
                    finding.get("suggestion"),
                    field_name=f"{field_name}.suggestion",
                )
            if finding.get("formula"):
                display_tex(finding.get("formula"), field_name=f"{field_name}.formula")
            resolved = dict(finding)
            resolved["kind"] = kind
            resolved["_bboxes"] = resolve_finding_bboxes(
                source[page_number - 1], finding, field_name=field_name
            )
            resolved_findings.append(resolved)
        resolved_page = dict(page_data)
        resolved_page["_findings"] = resolved_findings
        resolved_page["_problem_key"] = page_problem_key
        page_map[page_number] = resolved_page
    missing_pages = sorted(set(range(1, source.page_count + 1)) - set(page_map))
    if missing_pages:
        raise ValueError(
            "grading pages must cover every input page; missing "
            + ", ".join(str(page) for page in missing_pages)
        )
    next_id_by_problem: dict[str, int] = {}
    for page_number in sorted(page_map):
        resolved_page = page_map[page_number]
        problem_key = resolved_page["_problem_key"]
        next_id = next_id_by_problem.get(problem_key, 1)
        for finding in resolved_page["_findings"]:
            finding["id"] = next_id
            next_id += 1
        next_id_by_problem[problem_key] = next_id
    return page_map


def fit_source_rect(page_rect: fitz.Rect) -> tuple[float, float, float, float]:
    area_x0, area_y0, area_x1, area_y1 = SOURCE_AREA
    area_width = area_x1 - area_x0
    area_height = area_y1 - area_y0
    scale = min(area_width / page_rect.width, area_height / page_rect.height)
    width = page_rect.width * scale
    height = page_rect.height * scale
    left = area_x0 + (area_width - width) / 2
    top = area_y0 + (area_height - height) / 2
    return left, top, width, height


def page_point(x: float, y: float) -> str:
    return (
        f"([xshift={x:.3f}mm,yshift=-{y:.3f}mm]"
        "current page.north west)"
    )


def score_style(score: Any, maximum: Any) -> str:
    maximum_value = max(number(maximum, 7), 1)
    ratio = number(score) / maximum_value
    if ratio >= 0.999:
        return "correct"
    if ratio >= 0.7:
        return "warning"
    return "error"


def problem_card_tex(problem: dict[str, Any], index: int) -> str:
    style_key = score_style(problem.get("score"), problem.get("max_score"))
    style = STYLES[style_key]
    label = mixed_tex(
        problem.get("label"), f"第 {index} 题", field_name=f"problems[{index}].label"
    )
    summary = mixed_tex(
        problem.get("summary"),
        "已完成检查。",
        field_name=f"problems[{index}].summary",
    )
    score = escape_tex_plain(
        score_text(problem.get("score"), problem.get("max_score"))
    )
    return rf"""
\begin{{tcolorbox}}[
  enhanced,colback={style['fill']},colframe={style['accent']}!25,
  boxrule=.20mm,leftrule=1.8mm,arc=1.4mm,
  left=3mm,right=3mm,top=2.4mm,bottom=2.5mm,boxsep=0mm,
  before skip=0mm,after skip=3mm
]
{{\sffamily\fontsize{{11}}{{14}}\selectfont\color{{navy}} {label}
\hfill\color{{{style['accent']}}} {score}}}\par\smallskip
{{\fontsize{{9.6}}{{14}}\selectfont\color{{ink}} {summary}}}
\end{{tcolorbox}}
"""


def cover_tex(grading: dict[str, Any], footer: str) -> str:
    title = mixed_tex(grading.get("title"), "数学竞赛题批改", field_name="title")
    score = escape_tex_plain(score_text(grading.get("total_score"), grading.get("max_score")))
    summary = mixed_tex(
        grading.get("overall_summary"),
        "完成逐页检查。",
        field_name="overall_summary",
    )
    problems = grading.get("problems") if isinstance(grading.get("problems"), list) else []
    cards = "\n".join(
        problem_card_tex(problem, index)
        for index, problem in enumerate(problems[:9], start=1)
        if isinstance(problem, dict)
    )
    if not cards:
        cards = r"\textcolor{muted}{尚无分题汇总。}"
    return rf"""
\thispagestyle{{empty}}
\begin{{tikzpicture}}[remember picture,overlay]
  \fill[paper] (current page.south west) rectangle (current page.north east);
  \fill[navy] (current page.north west) rectangle
    ([xshift=205mm]current page.south west);
  \node[anchor=north west,inner sep=0,text width=174mm]
    at {page_point(15, 20)}
    {{\sffamily\fontsize{{25}}{{32}}\selectfont\color{{white}} {title}}};
  \node[anchor=north west,inner sep=0]
    at {page_point(15, 49)}
    {{\sffamily\fontsize{{11}}{{14}}\selectfont\color{{navylight}} 逐页批改报告}};
  \node[anchor=north west,inner sep=0]
    at {page_point(15, 82)}
    {{\sffamily\fontsize{{43}}{{48}}\selectfont\color{{white}} {score}}};
  \node[anchor=north west,inner sep=0,text width=170mm]
    at {page_point(15, 126)}
    {{\begin{{minipage}}{{170mm}}\RaggedRight
      \fontsize{{10.8}}{{17}}\selectfont\color{{navytext}} {summary}
    \end{{minipage}}}};
  \node[anchor=north west,inner sep=0]
    at {page_point(220, 18)}
    {{\sffamily\fontsize{{19}}{{24}}\selectfont\color{{navy}} 评分汇总}};
  \node[anchor=north west,inner sep=0,text width=185mm]
    at {page_point(220, 39)}
    {{\begin{{minipage}}{{185mm}}\RaggedRight {cards}\end{{minipage}}}};
  \node[anchor=south west,inner sep=0]
    at ([xshift=15mm,yshift=8mm]current page.south west)
    {{\sffamily\fontsize{{8}}{{10}}\selectfont\color{{navylight}} {escape_tex_plain(footer)}}};
\end{{tikzpicture}}
\null
"""


def finding_panel_tex(finding: dict[str, Any], field_name: str) -> str:
    kind = finding["kind"]
    style = STYLES[kind]
    identifier = finding["id"]
    title_value = mixed_tex(
        finding.get("title"), "批改意见", field_name=f"{field_name}.title"
    )
    deduction = number(finding.get("deduction"))
    deduction_tex = f"\n\\hfill -{deduction:g} 分" if deduction > 0 else ""
    reason = mixed_tex(
        finding.get("reason"),
        "请核对此处论证。",
        field_name=f"{field_name}.reason",
    )
    formula = ""
    if finding.get("formula"):
        formula_value = display_tex(
            finding.get("formula"), field_name=f"{field_name}.formula"
        )
        formula = rf"\[\displaystyle {formula_value}\]"
    suggestion = ""
    if finding.get("suggestion"):
        suggestion_value = mixed_tex(
            finding.get("suggestion"), field_name=f"{field_name}.suggestion"
        )
        suggestion = rf"""
\par\smallskip
\begin{{tcolorbox}}[
  enhanced,colback=infofill,colframe=info!24,
  boxrule=.16mm,leftrule=1.2mm,arc=.8mm,
  left=2mm,right=2mm,top=1.3mm,bottom=1.5mm,boxsep=0mm,
  before skip=0mm,after skip=0mm
]
{{\sffamily\color{{info}}\fontsize{{9.3}}{{11.5}}\selectfont 修改建议}}\par
{{\fontsize{{9.3}}{{13.2}}\selectfont\color{{ink}} {suggestion_value}}}
\end{{tcolorbox}}
"""
    return rf"""
\begin{{tcolorbox}}[
  enhanced,colback={style['fill']},colframe={style['accent']}!28,
  colbacktitle={style['head']},coltitle={style['accent']},
  boxrule=.20mm,leftrule=2.1mm,arc=1.5mm,
  left=3mm,right=3mm,top=2.2mm,bottom=2.5mm,boxsep=0mm,
  toptitle=1.8mm,bottomtitle=1.6mm,
  before skip=0mm,after skip=3.2mm,
  title={{\sffamily\fontsize{{10.8}}{{14}}\selectfont
    {identifier}. {style['label']}：{title_value}{deduction_tex}}}
]
{{\fontsize{{10.2}}{{15.5}}\selectfont\color{{ink}} {reason}{formula}{suggestion}}}
\end{{tcolorbox}}
"""


def verdict_panel_tex(problem: dict[str, Any], problem_index: int) -> str:
    verdict = text(problem.get("summary"), "已完成整题检查。")
    style_key = score_style(problem.get("score"), problem.get("max_score"))
    style = STYLES[style_key]
    verdict_value = mixed_tex(
        verdict, field_name=f"problems[{problem_index}].summary"
    )
    score = escape_tex_plain(score_text(problem.get("score"), problem.get("max_score")))
    return rf"""
\begin{{tcolorbox}}[
  enhanced,colback={style['fill']},colframe={style['accent']}!28,
  colbacktitle={style['head']},coltitle={style['accent']},
  boxrule=.20mm,leftrule=2.1mm,arc=1.5mm,
  left=3mm,right=3mm,top=2.2mm,bottom=2.5mm,boxsep=0mm,
  toptitle=1.8mm,bottomtitle=1.6mm,
  before skip=0mm,after skip=3.2mm,
  title={{\sffamily\fontsize{{10.8}}{{14}}\selectfont 判分结论}}
]
{{\sffamily\color{{{style['accent']}}}\fontsize{{11}}{{14}}\selectfont {score}}}
\quad{{\fontsize{{10.2}}{{15.5}}\selectfont\color{{ink}} {verdict_value}}}
\end{{tcolorbox}}
"""


def empty_page_panel_tex(page_data: dict[str, Any], page_number: int) -> str:
    summary = mixed_tex(
        page_data.get("page_summary"),
        "本页未发现实质性问题。",
        field_name=f"pages[{page_number}].page_summary",
    )
    return rf"""
\begin{{tcolorbox}}[
  enhanced,colback=correctfill,colframe=correct!24,
  colbacktitle=correcthead,coltitle=correct,
  boxrule=.20mm,leftrule=2.1mm,arc=1.5mm,
  left=3mm,right=3mm,top=2.4mm,bottom=2.7mm,boxsep=0mm,
  toptitle=1.8mm,bottomtitle=1.6mm,
  before skip=0mm,after skip=3.2mm,
  title={{\sffamily\fontsize{{10.8}}{{14}}\selectfont 本页说明}}
]
{{\fontsize{{10.2}}{{15.5}}\selectfont\color{{ink}} {summary}}}
\end{{tcolorbox}}
"""


def marker_tex(
    source_rect: tuple[float, float, float, float], finding: dict[str, Any]
) -> str:
    source_x, source_y, source_width, source_height = source_rect
    kind = finding["kind"]
    accent = STYLES[kind]["accent"]
    identifier = finding["id"]
    commands: list[str] = []
    for box in finding["_bboxes"]:
        x0, y0, x1, y1 = box
        left = source_x + x0 * source_width
        top = source_y + y0 * source_height
        right = source_x + x1 * source_width
        bottom = source_y + y1 * source_height
        if kind not in ("correct", "informational"):
            commands.append(
                rf"\draw[{accent},rounded corners=.6mm,line width=.52mm] "
                rf"{page_point(left, top)} rectangle {page_point(right, bottom)};"
            )
    primary_x0, primary_y0, _, _ = finding["_bboxes"][0]
    primary_left = source_x + primary_x0 * source_width
    primary_top = source_y + primary_y0 * source_height
    commands.append(
        rf"\node[circle,draw={accent},fill=paper,text={accent},"
        rf"minimum size=5.3mm,inner sep=0pt,line width=.48mm,"
        rf"font=\sffamily\fontsize{{7.6}}{{8}}\selectfont] "
        rf"at {page_point(primary_left - 0.3, primary_top)} {{{identifier}}};"
    )
    return "\n".join(commands)


def spread_tex(
    source: fitz.Document,
    source_index: int,
    page_data: dict[str, Any],
    problem_data: dict[str, Any],
    problem_index: int,
    is_problem_last_page: bool,
    total_output_pages: int,
    footer: str,
) -> str:
    page_number = source_index + 1
    source_rect = fit_source_rect(source[source_index].rect)
    source_x, source_y, source_width, source_height = source_rect
    problem = mixed_tex(
        page_data.get("problem"),
        f"第 {page_number} 页批改",
        field_name=f"pages[{page_number}].problem",
    )
    score = escape_tex_plain(
        score_text(problem_data.get("score"), problem_data.get("max_score"))
    )
    badge_style = score_style(
        problem_data.get("score"), problem_data.get("max_score")
    )
    findings = page_data.get("_findings") or []
    markers = "\n".join(marker_tex(source_rect, finding) for finding in findings)
    if findings:
        panels = "\n".join(
            finding_panel_tex(
                finding, f"pages[{page_number}].findings[{position}]"
            )
            for position, finding in enumerate(findings, start=1)
        )
    else:
        panels = empty_page_panel_tex(page_data, page_number)
    if is_problem_last_page:
        panels += verdict_panel_tex(problem_data, problem_index)
    return rf"""
\thispagestyle{{empty}}
\begin{{tikzpicture}}[remember picture,overlay]
  \fill[paper] (current page.south west) rectangle (current page.north east);
  \node[anchor=north west,inner sep=0]
    at {page_point(source_x, source_y)}
    {{\includegraphics[page={page_number},width={source_width:.3f}mm,height={source_height:.3f}mm]{{submission.pdf}}}};
  \draw[divider,line width=.18mm]
    {page_point(source_x, source_y)} rectangle
    {page_point(source_x + source_width, source_y + source_height)};
  {markers}
  \node[anchor=north west,inner sep=0]
    at {page_point(12, 9.2)}
    {{\sffamily\fontsize{{10}}{{13}}\selectfont\color{{muted}} 原答卷 · 第 {page_number} 页}};
  \node[anchor=north west,inner sep=0,text width=146mm]
    at {page_point(RIGHT_X, 8.2)}
    {{\sffamily\fontsize{{16.5}}{{21}}\selectfont\color{{navy}} {problem}}};
  \fill[{badge_style},rounded corners=1.8mm]
    {page_point(374, 8.0)} rectangle {page_point(408, 19.5)};
  \node[anchor=center,inner sep=0]
    at {page_point(391, 13.75)}
    {{\sffamily\fontsize{{10.5}}{{12}}\selectfont\color{{white}} {score}}};
  \node[anchor=north west,inner sep=0,text width={RIGHT_WIDTH:.1f}mm]
    at {page_point(RIGHT_X, 28)}
    {{\begin{{minipage}}{{{RIGHT_WIDTH:.1f}mm}}\RaggedRight {panels}\end{{minipage}}}};
  \draw[divider,line width=.15mm]
    {page_point(12, 287.2)} -- {page_point(408, 287.2)};
  \node[anchor=north west,inner sep=0]
    at {page_point(12, 289.4)}
    {{\sffamily\fontsize{{7.5}}{{9}}\selectfont\color{{muted}} {escape_tex_plain(footer)}}};
  \node[anchor=north east,inner sep=0]
    at {page_point(408, 289.4)}
    {{\sffamily\fontsize{{7.5}}{{9}}\selectfont\color{{muted}} {source_index + 2} / {total_output_pages}}};
\end{{tikzpicture}}
\null
"""


def tex_preamble() -> str:
    return r"""\documentclass[10pt]{article}
\usepackage[paperwidth=420mm,paperheight=297mm,margin=0mm]{geometry}
\usepackage{xcolor}
\usepackage{fontspec}
\usepackage{xeCJK}
\usepackage{amsmath,amssymb,mathtools,bm}
\usepackage{graphicx}
\usepackage{tikz}
\usetikzlibrary{calc}
\usepackage[most]{tcolorbox}
\usepackage{ragged2e}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\defaultfontfeatures{Ligatures=TeX}
\setmainfont[
  Path={fonts/},
  BoldFont={NotoSansCJKsc-Medium.otf}
]{NotoSerifCJKsc-Regular.otf}
\setCJKmainfont[
  Path={fonts/},
  BoldFont={NotoSansCJKsc-Medium.otf}
]{NotoSerifCJKsc-Regular.otf}
\setsansfont[
  Path={fonts/},
  BoldFont={NotoSansCJKsc-Medium.otf}
]{NotoSansCJKsc-Medium.otf}
\setCJKsansfont[
  Path={fonts/},
  BoldFont={NotoSansCJKsc-Medium.otf}
]{NotoSansCJKsc-Medium.otf}
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
\definecolor{paper}{HTML}{FEFDF9}
\definecolor{navy}{HTML}{17364B}
\definecolor{navylight}{HTML}{BBC8D1}
\definecolor{navytext}{HTML}{E8EEF2}
\definecolor{ink}{HTML}{1D2932}
\definecolor{muted}{HTML}{68747C}
\definecolor{divider}{HTML}{DDDCD6}
\definecolor{correct}{HTML}{26885F}
\definecolor{correctfill}{HTML}{EDF7F1}
\definecolor{correcthead}{HTML}{DCEFE5}
\definecolor{warning}{HTML}{C17A0B}
\definecolor{warningfill}{HTML}{FFF7E8}
\definecolor{warninghead}{HTML}{F7E8C8}
\definecolor{error}{HTML}{C3474D}
\definecolor{errorfill}{HTML}{FCEFF0}
\definecolor{errorhead}{HTML}{F3DCDD}
\definecolor{info}{HTML}{357BA8}
\definecolor{infofill}{HTML}{EEF5FA}
\definecolor{infohead}{HTML}{DCEAF4}
\begin{document}
"""


def build_tex_document(
    source: fitz.Document,
    grading: dict[str, Any],
    profile: dict[str, Any],
    page_map: dict[int, dict[str, Any]],
) -> str:
    total_output_pages = source.page_count + 1
    footer = grading_footer(profile, grading)
    pages = [cover_tex(grading, footer)]
    problems = grading.get("problems") or []
    problem_by_key = {
        problem_label_key(text(problem.get("label"), f"第 {index} 题")): (index, problem)
        for index, problem in enumerate(problems, start=1)
    }
    last_page_by_problem: dict[str, int] = {}
    for page_number, page_data in page_map.items():
        key = page_data["_problem_key"]
        last_page_by_problem[key] = max(page_number, last_page_by_problem.get(key, 0))
    for source_index in range(source.page_count):
        page_number = source_index + 1
        page_data = page_map[page_number]
        problem_key = page_data["_problem_key"]
        problem_index, problem_data = problem_by_key[problem_key]
        pages.append(
            spread_tex(
                source,
                source_index,
                page_data,
                problem_data,
                problem_index,
                page_number == last_page_by_problem[problem_key],
                total_output_pages,
                footer,
            )
        )
    return tex_preamble() + "\n\\clearpage\n".join(pages) + "\n\\end{document}\n"


def find_executable(name: str, fallback: str) -> Path:
    located = shutil.which(name)
    if located:
        return Path(located)
    candidate = Path(fallback)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    raise RuntimeError(f"Required executable not found: {name}")


def link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def prepare_build_directory(build_dir: Path, input_path: Path) -> None:
    link_or_copy(input_path, build_dir / "submission.pdf")
    font_source = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    for filename in FONT_FILES:
        source = font_source / filename
        if not source.is_file():
            raise RuntimeError(f"Bundled font is missing: {source}")
        link_or_copy(source, build_dir / "fonts" / filename)


def compile_tex(build_dir: Path) -> Path:
    xelatex = find_executable("xelatex", "/Library/TeX/texbin/xelatex")
    command = [
        str(xelatex),
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-no-shell-escape",
        "-jobname=annotated",
        "report.tex",
    ]
    environment = os.environ.copy()
    environment["openin_any"] = "p"
    environment["openout_any"] = "p"
    environment["TEXMFOUTPUT"] = str(build_dir)
    latest_output = ""
    for _ in range(2):
        result = subprocess.run(
            command,
            cwd=build_dir,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        latest_output = result.stdout + "\n" + result.stderr
        if result.returncode != 0:
            tail = "\n".join(latest_output.splitlines()[-35:])
            raise RuntimeError(f"XeLaTeX compilation failed:\n{tail}")
    output = build_dir / "annotated.pdf"
    if not output.is_file():
        raise RuntimeError("XeLaTeX completed without creating annotated.pdf")
    return output


def save_final_pdf(
    compiled_path: Path,
    output_path: Path,
    grading: dict[str, Any],
    expected_pages: int,
) -> None:
    document = fitz.open(compiled_path)
    try:
        if document.page_count != expected_pages:
            raise RuntimeError(
                f"Generated PDF has {document.page_count} pages; expected {expected_pages}"
            )
        for page_number, page in enumerate(document, start=1):
            if abs(page.rect.width - PAGE_WIDTH_PT) > 0.2 or abs(
                page.rect.height - PAGE_HEIGHT_PT
            ) > 0.2:
                raise RuntimeError(f"Generated page {page_number} has the wrong page size")
            if not page.get_contents():
                raise RuntimeError(f"Generated page {page_number} is blank")
        document.set_metadata(
            {
                "title": text(grading.get("title"), "数学竞赛题批改"),
                "author": "数学竞赛题批改",
                "subject": "数学竞赛题逐页批改报告",
                "creator": "数学竞赛题批改",
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(output_path.stem + ".tmp.pdf")
        if temporary.exists():
            temporary.unlink()
        document.save(temporary, garbage=4, deflate=True, clean=True)
        os.replace(temporary, output_path)
    finally:
        document.close()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    grading_path = args.grading.resolve()
    profile_path = args.profile.resolve()
    output_path = args.output.resolve()
    grading = load_grading(grading_path)
    profile = load_profile(profile_path)
    validate_grading_profile(grading, profile)
    source = fitz.open(input_path)
    if source.needs_pass:
        source.close()
        raise SystemExit("Encrypted PDFs are not supported")
    page_map = validate_and_resolve_grading(grading, source)
    tex_source = build_tex_document(source, grading, profile, page_map)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_dir = Path(
        tempfile.mkdtemp(prefix=".latex-build-", dir=str(output_path.parent))
    )
    succeeded = False
    try:
        prepare_build_directory(build_dir, input_path)
        (build_dir / "report.tex").write_text(tex_source, encoding="utf-8")
        compiled_path = compile_tex(build_dir)
        expected_pages = source.page_count + 1
        save_final_pdf(compiled_path, output_path, grading, expected_pages)
        succeeded = True
        print(f"output={output_path} pages={expected_pages} engine=xelatex")
        return 0
    finally:
        source.close()
        if succeeded:
            shutil.rmtree(build_dir, ignore_errors=True)
        else:
            print(f"XeLaTeX build files kept at: {build_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
