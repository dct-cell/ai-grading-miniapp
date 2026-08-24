from __future__ import annotations

GRADING_STAGE_SEQUENCE = (
    "preparing",
    "understanding",
    "rubric",
    "decomposing",
    "verifying",
    "scoring",
    "auditing",
    "reporting",
    "validating",
)

GRADING_STAGE_LABELS = {
    "preparing": "正在读取答卷",
    "understanding": "正在理解题目与作答",
    "rubric": "正在整理评分要点",
    "decomposing": "正在梳理解答步骤",
    "verifying": "正在核验关键推理",
    "scoring": "正在计算得分",
    "auditing": "正在复核判分",
    "reporting": "正在生成批改报告",
    "validating": "正在检查报告",
}

GRADING_STAGE_INDEX = {
    stage: index for index, stage in enumerate(GRADING_STAGE_SEQUENCE)
}
