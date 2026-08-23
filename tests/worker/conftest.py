from __future__ import annotations

from pathlib import Path

import pytest

from worker.runtime.contracts import TaskBundle
from worker.runtime.testsupport import build_minimal_pdf


@pytest.fixture
def anyio_backend() -> str:
    """The Worker daemon is asyncio-only; anyio's trio backend is not used."""
    return "asyncio"


@pytest.fixture
def downloaded_bundle(tmp_path: Path) -> TaskBundle:
    """A bundle whose source/reference PDFs already exist on disk.

    Mirrors the post-download state the Worker daemon hands to
    ``prepare_workspace``: the bytes are staged locally and the bundle points
    at the staging paths. Tests that need a different grading standard or
    scope can call ``downloaded_bundle.model_copy(update={...})`` and write
    any extra PDFs they need.
    """
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    source = downloads / "source.pdf"
    source.write_bytes(build_minimal_pdf(page_count=1))
    reference = downloads / "reference.pdf"
    reference.write_bytes(build_minimal_pdf(page_count=1))
    return TaskBundle(
        job_id="job-1",
        order_id="order-1",
        round_number=1,
        service_tier="annotated_review",
        grading_standard="imo",
        league_scope=None,
        source_pdf=str(source),
        reference_pdf=str(reference),
        page_count=1,
        note="考生补充说明",
    )
