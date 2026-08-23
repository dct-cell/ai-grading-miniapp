"""``GRADER_`` is a prefix of ``GRADER_WORKER_``, and both models read ``.env``.

``ServerSettings`` (``env_prefix="GRADER_"``) and ``WorkerSettings``
(``env_prefix="GRADER_WORKER_"``) both default to loading the repository's
``.env`` file. Because pydantic-settings forbids extra inputs by default, a
single ``.env`` holding both halves of the deployment made *both* models
unconstructable:

* every ``GRADER_DATABASE_URL``-style server key looked like an extra input to
  ``WorkerSettings``, so ``python -m worker.cli`` could not start at all;
* a single ``GRADER_WORKER_INSTALLATION_ID`` line looked like an extra input to
  ``ServerSettings``, so the API server could not start either.

Putting both halves in one ``.env`` is exactly what a single-host deployment
does, so this is a deployment-time failure and not merely a test-isolation
问题. Each model must therefore ignore keys belonging to the *other* model
while still rejecting a misspelling of one of its own keys — otherwise
``GRADER_MAX_PDF_PAGE`` (missing the ``S``) would silently fall back to the
default instead of failing loudly.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from server.config import ServerSettings
from worker.config import WorkerSettings


#: One variable, two readers. ``GRADER_WORKER_SHARED_KEY`` is the server's
#: ``worker_shared_key`` (prefix ``GRADER_`` stripped) *and* the Worker's
#: ``shared_key`` (prefix ``GRADER_WORKER_`` stripped). That overlap is the
#: point — both ends must agree on the key — so it appears exactly once here.
SHARED_WORKER_KEY = "k" * 40

SERVER_LINES = (
    "GRADER_ENVIRONMENT=test",
    "GRADER_DATABASE_URL=sqlite+pysqlite:///:memory:",
    "GRADER_SESSION_SECRET=" + "s" * 40,
    f"GRADER_WORKER_SHARED_KEY={SHARED_WORKER_KEY}",
    "GRADER_ADMIN_SHARED_KEY=" + "a" * 40,
    "GRADER_PRICE_CENTS_PER_PAGE=500",
    "GRADER_SUMMARY_PRICE_CENTS_PER_PAGE=100",
    "GRADER_ANNOTATED_PRICE_CENTS_PER_PAGE=500",
    "GRADER_MAX_PDF_BYTES=26214400",
    "GRADER_MAX_PDF_PAGES=30",
    "GRADER_QUOTE_TTL_SECONDS=86400",
    "GRADER_ACCEPTANCE_TTL_SECONDS=259200",
)
WORKER_LINES = (
    "GRADER_WORKER_SERVER_BASE_URL=https://grader.example.com",
    "GRADER_WORKER_INSTALLATION_ID=install-1",
    "GRADER_WORKER_DEVICE_NAME=studio-mac",
    "GRADER_WORKER_POLL_WAIT_SECONDS=25",
)


def _write_shared_dotenv(directory: Path) -> None:
    """Write the ``.env`` a single-host deployment would realistically have."""
    data_dir = directory / "data"
    workspace_root = directory / "workspace"
    data_dir.mkdir(exist_ok=True)
    workspace_root.mkdir(exist_ok=True)
    lines = (
        *SERVER_LINES,
        f"GRADER_DATA_DIR={data_dir}",
        *WORKER_LINES,
        f"GRADER_WORKER_WORKSPACE_ROOT={workspace_root}",
    )
    (directory / ".env").write_text("\n".join(lines), encoding="utf-8")


def test_server_settings_load_from_a_dotenv_that_also_configures_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API server must start when the Worker's keys share its ``.env``."""
    _write_shared_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)

    settings = ServerSettings()

    assert settings.database_url == "sqlite+pysqlite:///:memory:"
    assert settings.worker_shared_key == SHARED_WORKER_KEY
    assert settings.max_pdf_pages == 30


def test_worker_settings_load_from_a_dotenv_that_also_configures_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python -m worker.cli`` must start in a directory holding a full .env."""
    _write_shared_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)

    settings = WorkerSettings()

    assert settings.installation_id == "install-1"
    assert settings.server_base_url == "https://grader.example.com"
    assert settings.device_name == "studio-mac"
    # Both ends read the same variable, which is how they agree on the key.
    assert settings.shared_key == SHARED_WORKER_KEY


def test_server_settings_still_reject_a_misspelled_server_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignoring the Worker's namespace must not mean ignoring typos."""
    _write_shared_dotenv(tmp_path)
    with (tmp_path / ".env").open("a", encoding="utf-8") as handle:
        # GRADER_MAX_PDF_PAGES without the trailing S.
        handle.write("\nGRADER_MAX_PDF_PAGE=5")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings()

    assert "grader_max_pdf_page" in str(exc_info.value)


def test_worker_settings_still_reject_a_misspelled_worker_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_shared_dotenv(tmp_path)
    with (tmp_path / ".env").open("a", encoding="utf-8") as handle:
        # GRADER_WORKER_DEVICE_NAME misspelled.
        handle.write("\nGRADER_WORKER_DEVICE_NAM=studio")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as exc_info:
        WorkerSettings()

    assert "grader_worker_device_nam" in str(exc_info.value)


def test_a_misspelled_server_key_is_not_hidden_by_the_worker_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo that happens to start with the Worker prefix still fails.

    ``GRADER_WORKER_SHARED_KEYY`` is not a valid Worker key either, so the
    Worker model must reject it rather than treat it as somebody else's.
    """
    _write_shared_dotenv(tmp_path)
    with (tmp_path / ".env").open("a", encoding="utf-8") as handle:
        handle.write("\nGRADER_WORKER_SHARED_KEYY=" + "z" * 40)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as exc_info:
        WorkerSettings()

    assert "grader_worker_shared_keyy" in str(exc_info.value)


def test_a_missing_field_is_still_an_error_when_no_dotenv_supplies_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests asserting on *absent* configuration must opt out of the .env.

    A developer's real ``.env`` supplies ``GRADER_DATA_DIR``, which would
    otherwise satisfy the field under test and make the assertion vacuous.
    """
    _write_shared_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings(
            _env_file=None,
            environment="test",
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="s" * 40,
            worker_shared_key="w" * 40,
            admin_shared_key="a" * 40,
        )

    missing = [error for error in exc_info.value.errors() if error["type"] == "missing"]
    assert [error["loc"] for error in missing] == [("data_dir",)]


def test_settings_error_for_an_unknown_key_does_not_leak_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra-input error path must honour the redaction contract too."""
    _write_shared_dotenv(tmp_path)
    with (tmp_path / ".env").open("a", encoding="utf-8") as handle:
        handle.write("\nGRADER_MAX_PDF_PAGE=5")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings()

    rendered = str(exc_info.value)
    assert "s" * 40 not in rendered
    assert "a" * 40 not in rendered
    assert SHARED_WORKER_KEY not in rendered
