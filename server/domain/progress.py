from enum import StrEnum
from typing import Final


class ProgressStage(StrEnum):
    """Stable, user-facing grading progress identifiers."""

    QUEUED = "queued"
    ASSIGNED = "assigned"
    PREPARING = "preparing"
    UNDERSTANDING = "understanding"
    RUBRIC = "rubric"
    DECOMPOSING = "decomposing"
    VERIFYING = "verifying"
    SCORING = "scoring"
    AUDITING = "auditing"
    REPORTING = "reporting"
    VALIDATING = "validating"
    UPLOADING = "uploading"
    SYSTEM_PROCESSING = "system_processing"


RUNTIME_PROGRESS_STAGES: Final[frozenset[str]] = frozenset(
    {
        ProgressStage.PREPARING,
        ProgressStage.UNDERSTANDING,
        ProgressStage.RUBRIC,
        ProgressStage.DECOMPOSING,
        ProgressStage.VERIFYING,
        ProgressStage.SCORING,
        ProgressStage.AUDITING,
        ProgressStage.REPORTING,
        ProgressStage.VALIDATING,
    }
)

