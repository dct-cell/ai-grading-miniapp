from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal


ServiceTier = Literal["summary_report", "annotated_review"]

SUMMARY_REPORT: Final[ServiceTier] = "summary_report"
ANNOTATED_REVIEW: Final[ServiceTier] = "annotated_review"
SUPPORTED_SERVICE_TIERS: Final[frozenset[str]] = frozenset(
    {SUMMARY_REPORT, ANNOTATED_REVIEW}
)


@dataclass(frozen=True)
class ServiceTierDefinition:
    id: ServiceTier
    label: str
    description: str
    delivery_label: str
    output_filename: str


SERVICE_TIER_DEFINITIONS: Final[dict[ServiceTier, ServiceTierDefinition]] = {
    SUMMARY_REPORT: ServiceTierDefinition(
        id=SUMMARY_REPORT,
        label="简明评分",
        description="给出总分、分题判断和主要问题。",
        delivery_label="A4 评分报告",
        output_filename="report.pdf",
    ),
    ANNOTATED_REVIEW: ServiceTierDefinition(
        id=ANNOTATED_REVIEW,
        label="逐页精批",
        description="在答卷对应位置标注，并给出逐页批改报告。",
        delivery_label="逐页批改报告",
        output_filename="annotated.pdf",
    ),
}


def require_service_tier(value: str) -> ServiceTier:
    if value not in SUPPORTED_SERVICE_TIERS:
        raise ValueError("请选择简明评分或逐页精批。")
    return value  # type: ignore[return-value]


def service_tier_label(value: str) -> str:
    return SERVICE_TIER_DEFINITIONS[require_service_tier(value)].label
