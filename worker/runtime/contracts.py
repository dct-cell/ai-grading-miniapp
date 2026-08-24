from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "GRADING_STANDARDS",
    "LEAGUE_SCOPES",
    "RUNTIME_ERROR_CODES",
    "TaskBundle",
    "RuntimeResult",
    "GradingRuntime",
]


GRADING_STANDARDS = frozenset({"league_second_round", "cmo", "imo"})
LEAGUE_SCOPES = frozenset({"auto", "full_paper", "problem_set"})
SERVICE_TIERS = frozenset({"summary_report", "annotated_review"})

# The seven stable error codes the Worker exposes to the server. Legacy
# ``CodexRunError.code`` values map onto this set; the adapter never leaks
# the legacy names through the control plane.
RUNTIME_ERROR_CODES = frozenset(
    {
        "runtime_auth_failed",
        "runtime_unavailable",
        "runtime_timeout",
        "runtime_invalid_json",
        "runtime_invalid_pdf",
        "runtime_cancelled",
        "runtime_misconfigured",
    }
)


class TaskBundle(BaseModel):
    """Inputs for one grading run, frozen before the runtime is invoked.

    The bundle is the only channel through which untrusted student data and
    product configuration enter the runtime. The runtime must not read
    server-side state directly.
    """

    model_config = {"extra": "forbid"}

    job_id: str
    order_id: str
    round_number: Literal[1, 2]
    service_tier: Literal["summary_report", "annotated_review"]
    grading_standard: Literal["league_second_round", "cmo", "imo"]
    league_scope: Literal["auto", "full_paper", "problem_set"] | None = None
    league_problem_number: Literal[1, 2, 3, 4] | None = None
    source_pdf: str
    reference_pdf: str | None = None
    page_count: int = Field(ge=1)
    note: str = Field(max_length=4000)

    @model_validator(mode="after")
    def _validate_scope_pairing(self) -> TaskBundle:
        # The server freezes league scope before leasing.  A missing scope on
        # a league job is therefore a broken paid-product snapshot, not a hint
        # for the Worker to guess a default.
        if self.grading_standard == "league_second_round" and self.league_scope is None:
            raise ValueError(
                "league_scope is required for league_second_round"
            )
        if self.grading_standard != "league_second_round" and self.league_scope is not None:
            raise ValueError(
                "league_scope may only be set for league_second_round"
            )
        if (
            self.grading_standard != "league_second_round"
            and self.league_problem_number is not None
        ):
            raise ValueError(
                "league_problem_number may only be set for league_second_round"
            )
        if self.league_scope == "full_paper" and self.league_problem_number is not None:
            raise ValueError(
                "league_problem_number is only valid for a standalone League problem"
            )
        return self


class RuntimeResult(BaseModel):
    """Public artifacts a ``GradingRuntime`` produces for one task.

    The daemon uploads only these paths and checksums to the server. Raw
    internal analysis artifacts stay in the workspace and are removed once
    the server commits the result.
    """

    model_config = {"arbitrary_types_allowed": True}

    manifest_path: Path
    result_json_path: Path
    result_pdf_path: Path
    result_json_sha256: str
    result_pdf_sha256: str
    output_page_count: int = Field(ge=1)

    @classmethod
    def from_workspace(cls, workspace: Path) -> RuntimeResult:
        """Build a result by reading the canonical output paths.

        Raises ``ValueError`` when any required artifact is missing so the
        adapter fails fast instead of uploading partial work.
        """
        manifest_path = workspace / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("manifest is missing")

        manifest = _read_json_object(manifest_path, description="manifest")
        output_pdf = manifest.get("output_pdf")
        if output_pdf not in {"output/report.pdf", "output/annotated.pdf"}:
            raise ValueError("manifest output_pdf is invalid")

        pdf_path = workspace / Path(output_pdf).name
        if not pdf_path.is_file():
            raise ValueError("result PDF is missing")

        json_path = workspace / "grading.json"
        if not json_path.is_file():
            raise ValueError("result JSON is missing")

        page_count = _manifest_page_count(manifest)

        return cls(
            manifest_path=manifest_path,
            result_json_path=json_path,
            result_pdf_path=pdf_path,
            result_json_sha256=_sha256_path(json_path),
            result_pdf_sha256=_sha256_path(pdf_path),
            output_page_count=page_count,
        )


T = TypeVar("T")


@runtime_checkable
class GradingRuntime(Protocol):
    """The stable seam between the Worker daemon and a grading implementation.

    Implementations own process spawning and tool invocation. They must not
    touch the server, the lease, or the upload protocol — the daemon drives
    those. ``progress`` forwards stage updates to the lease renewer and must
    be awaited even when the implementation has nothing to say.
    """

    async def run(
        self,
        workspace: Path,
        bundle: TaskBundle,
        progress: Callable[[str], Awaitable[None]],
    ) -> RuntimeResult: ...


def _read_json_object(path: Path, *, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{description} is missing") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{description} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{description} is not a JSON object")
    return payload


def _manifest_page_count(manifest: dict[str, Any]) -> int:
    page_count = manifest.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ValueError("manifest page_count is not a positive integer")
    return page_count


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
