from collections.abc import Mapping, Sequence
from collections import UserDict
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.config import Environment, ServerSettings


def assert_error_object_graph_is_irreversible(
    value: object,
    sensitive_values: tuple[str, ...],
    visited: set[int] | None = None,
) -> None:
    if visited is None:
        visited = set()

    value_id = id(value)
    if value_id in visited:
        return
    visited.add(value_id)

    if callable(getattr(value, "get_secret_value", None)):
        pytest.fail(f"reversible sensitive object found: {type(value).__name__}")

    if isinstance(value, str):
        for sensitive_value in sensitive_values:
            assert sensitive_value not in value
        return
    if isinstance(value, bytes):
        for sensitive_value in sensitive_values:
            assert sensitive_value.encode() not in value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            assert_error_object_graph_is_irreversible(key, sensitive_values, visited)
            assert_error_object_graph_is_irreversible(item, sensitive_values, visited)
        return
    if isinstance(value, Sequence) or isinstance(value, (set, frozenset)):
        for item in value:
            assert_error_object_graph_is_irreversible(item, sensitive_values, visited)
        return
    if isinstance(value, BaseException):
        assert_error_object_graph_is_irreversible(
            value.args,
            sensitive_values,
            visited,
        )
        traceback = value.__traceback__
        while traceback is not None:
            assert_error_object_graph_is_irreversible(
                traceback.tb_frame.f_locals,
                sensitive_values,
                visited,
            )
            traceback = traceback.tb_next
        return

    if isinstance(value, type):
        return

    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        assert_error_object_graph_is_irreversible(
            attributes,
            sensitive_values,
            visited,
        )


def assert_validation_error_redacts(
    error: ValidationError,
    sensitive_values: tuple[str, ...],
) -> None:
    rendered_error = str(error)
    structured_errors = repr(error.errors())
    serialized_error = error.json()

    for sensitive_value in sensitive_values:
        assert sensitive_value not in rendered_error
        assert sensitive_value not in structured_errors
        assert sensitive_value not in serialized_error
    assert "**********" in structured_errors
    assert "**********" in serialized_error
    assert_error_object_graph_is_irreversible(error.errors(), sensitive_values)


def test_test_environment_accepts_sqlite(tmp_path: Path) -> None:
    settings = ServerSettings(
        environment=Environment.TEST,
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path,
        session_secret="s" * 32,
        worker_shared_key="w" * 32,
        admin_shared_key="a" * 32,
    )
    assert settings.data_dir == tmp_path


def test_production_rejects_non_mysql_without_leaking_inputs(
    tmp_path: Path,
) -> None:
    database_password = "known-production-database-password"
    database_url = f"postgresql://grader:{database_password}@db/grader"
    session_secret = "known-production-session-secret-" + "s" * 32
    worker_shared_key = "known-production-worker-key-" + "w" * 32
    admin_shared_key = "known-production-admin-key-" + "a" * 32

    with pytest.raises(
        ValidationError,
        match="production requires MySQL",
    ) as exc_info:
        ServerSettings(
            environment=Environment.PRODUCTION,
            database_url=database_url,
            data_dir=tmp_path,
            session_secret=session_secret,
            worker_shared_key=worker_shared_key,
        admin_shared_key=admin_shared_key,
        )

    assert_validation_error_redacts(
        exc_info.value,
        (
            database_url,
            database_password,
            session_secret,
            worker_shared_key,
            admin_shared_key,
        ),
    )


def test_settings_repr_hides_secrets_but_attributes_remain_strings(
    tmp_path: Path,
) -> None:
    database_password = "known-database-password"
    database_url = f"mysql+pymysql://grader:{database_password}@db/grader"
    session_secret = "known-session-secret-" + "s" * 32
    worker_shared_key = "known-worker-shared-key-" + "w" * 32
    admin_shared_key = "known-admin-shared-key-" + "a" * 32
    settings = ServerSettings(
        environment=Environment.TEST,
        database_url=database_url,
        data_dir=tmp_path,
        session_secret=session_secret,
        worker_shared_key=worker_shared_key,
        admin_shared_key=admin_shared_key,
    )

    settings_repr = repr(settings)

    assert database_password not in settings_repr
    assert session_secret not in settings_repr
    assert worker_shared_key not in settings_repr
    assert settings.database_url == database_url
    assert settings.session_secret == session_secret
    assert settings.worker_shared_key == worker_shared_key
    assert type(settings.database_url) is str
    assert type(settings.session_secret) is str
    assert type(settings.worker_shared_key) is str


def test_secret_validation_errors_hide_invalid_inputs(tmp_path: Path) -> None:
    session_secret = "known-short-session"
    worker_shared_key = "known-short-worker"
    admin_shared_key = "known-short-admin"

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings(
            environment=Environment.TEST,
            database_url="sqlite+pysqlite:///:memory:",
            data_dir=tmp_path,
            session_secret=session_secret,
            worker_shared_key=worker_shared_key,
        admin_shared_key=admin_shared_key,
        )

    assert_validation_error_redacts(
        exc_info.value,
        (session_secret, worker_shared_key, admin_shared_key),
    )


def test_dotenv_secret_validation_errors_hide_invalid_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_secret = "known-dotenv-short-session"
    worker_shared_key = "known-dotenv-short-worker"
    admin_shared_key = "known-dotenv-short-admin"
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "GRADER_ENVIRONMENT=test",
                "GRADER_DATABASE_URL=sqlite+pysqlite:///:memory:",
                f"GRADER_DATA_DIR={tmp_path}",
                f"GRADER_SESSION_SECRET={session_secret}",
                f"GRADER_WORKER_SHARED_KEY={worker_shared_key}",
                f"GRADER_ADMIN_SHARED_KEY={admin_shared_key}",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings()

    assert_validation_error_redacts(
        exc_info.value,
        (session_secret, worker_shared_key, admin_shared_key),
    )


def test_environment_secret_validation_errors_hide_invalid_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_secret = "known-environment-short-session"
    worker_shared_key = "known-environment-valid-worker-" + "w" * 32
    admin_shared_key = "known-environment-valid-admin-" + "a" * 32
    monkeypatch.setenv("GRADER_ENVIRONMENT", "test")
    monkeypatch.setenv("GRADER_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("GRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GRADER_SESSION_SECRET", session_secret)
    monkeypatch.setenv("GRADER_WORKER_SHARED_KEY", worker_shared_key)
    monkeypatch.setenv("GRADER_ADMIN_SHARED_KEY", admin_shared_key)

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings()

    assert_validation_error_redacts(
        exc_info.value,
        (session_secret, worker_shared_key, admin_shared_key),
    )


def test_missing_non_sensitive_field_error_does_not_retain_secrets() -> None:
    database_url = "sqlite+pysqlite:///:memory:"
    session_secret = "known-missing-field-session-" + "s" * 32
    worker_shared_key = "known-missing-field-worker-" + "w" * 32
    admin_shared_key = "known-missing-field-admin-" + "a" * 32

    # ``_env_file=None`` because this test asserts that omitting ``data_dir``
    # is an error. A developer's real ``.env`` in the repository root supplies
    # ``GRADER_DATA_DIR``, which would satisfy the field and make the assertion
    # vacuous. Only the tests that assert on *absent* configuration need this;
    # the rest of the suite must keep working with a real .env present, which
    # tests/server/test_env_prefix_isolation.py covers.
    with pytest.raises(ValidationError, match="Field required") as exc_info:
        ServerSettings(
            _env_file=None,
            environment=Environment.TEST,
            database_url=database_url,
            session_secret=session_secret,
            worker_shared_key=worker_shared_key,
            admin_shared_key=admin_shared_key,
        )

    errors = exc_info.value.errors()
    assert errors[0]["loc"] == ("data_dir",)
    assert errors[0]["type"] == "missing"
    assert_validation_error_redacts(
        exc_info.value,
        (database_url, session_secret, worker_shared_key, admin_shared_key),
    )


def test_invalid_data_dir_preserves_sanitized_path_validation_error() -> None:
    database_url = "sqlite+pysqlite:///:memory:"
    session_secret = "known-path-error-session-" + "s" * 32
    worker_shared_key = "known-path-error-worker-" + "w" * 32
    admin_shared_key = "known-path-error-admin-" + "a" * 32

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings(
            environment=Environment.TEST,
            database_url=database_url,
            data_dir=123,
            session_secret=session_secret,
            worker_shared_key=worker_shared_key,
        admin_shared_key=admin_shared_key,
        )

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "path_type"
    assert errors[0]["loc"] == ("data_dir",)
    assert errors[0]["msg"] == (
        "Input is not a valid path for <class 'pathlib.Path'>"
    )
    assert_validation_error_redacts(
        exc_info.value,
        (database_url, session_secret, worker_shared_key, admin_shared_key),
    )


def test_grader_environment_variables_load_as_plain_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "sqlite+pysqlite:///:memory:"
    session_secret = "environment-session-secret-" + "s" * 32
    worker_shared_key = "environment-worker-key-" + "w" * 32
    admin_shared_key = "environment-admin-key-" + "a" * 32
    monkeypatch.setenv("GRADER_ENVIRONMENT", "test")
    monkeypatch.setenv("GRADER_DATABASE_URL", database_url)
    monkeypatch.setenv("GRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GRADER_SESSION_SECRET", session_secret)
    monkeypatch.setenv("GRADER_WORKER_SHARED_KEY", worker_shared_key)
    monkeypatch.setenv("GRADER_ADMIN_SHARED_KEY", admin_shared_key)

    settings = ServerSettings()

    assert settings.database_url == database_url
    assert settings.session_secret == session_secret
    assert settings.worker_shared_key == worker_shared_key
    assert type(settings.database_url) is str
    assert type(settings.session_secret) is str
    assert type(settings.worker_shared_key) is str


@pytest.mark.parametrize(
    "field_name",
    ("database_url", "session_secret", "worker_shared_key", "admin_shared_key"),
)
def test_explicit_none_for_sensitive_field_raises_sanitized_validation_error(
    field_name: str,
    tmp_path: Path,
) -> None:
    sensitive_values: dict[str, object] = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "known-valid-session-secret-" + "s" * 32,
        "worker_shared_key": "known-valid-worker-key-" + "w" * 32,
        "admin_shared_key": "known-valid-admin-key-" + "a" * 32,
    }
    sensitive_values[field_name] = None

    with pytest.raises(
        ValidationError,
        match="Input should be a valid string",
    ) as exc_info:
        ServerSettings(
            environment=Environment.TEST,
            data_dir=tmp_path,
            **sensitive_values,
        )

    structured_errors = repr(exc_info.value.errors())
    serialized_error = exc_info.value.json()
    assert exc_info.value.errors()[0]["type"] == "string_type"
    for other_field_name, sensitive_value in sensitive_values.items():
        if other_field_name != field_name:
            assert str(sensitive_value) not in structured_errors
            assert str(sensitive_value) not in serialized_error


def test_model_validate_accepts_mapping_with_plain_string_attributes(
    tmp_path: Path,
) -> None:
    database_url = "sqlite+pysqlite:///:memory:"
    session_secret = "mapping-session-secret-" + "s" * 32
    worker_shared_key = "mapping-worker-key-" + "w" * 32
    admin_shared_key = "mapping-admin-key-" + "a" * 32
    settings = ServerSettings.model_validate(
        UserDict(
            {
                "environment": Environment.TEST,
                "database_url": database_url,
                "data_dir": tmp_path,
                "session_secret": session_secret,
                "worker_shared_key": worker_shared_key,
                "admin_shared_key": admin_shared_key,
            }
        )
    )

    assert settings.database_url == database_url
    assert settings.session_secret == session_secret
    assert settings.worker_shared_key == worker_shared_key
    assert type(settings.database_url) is str
    assert type(settings.session_secret) is str
    assert type(settings.worker_shared_key) is str


def test_mapping_short_secret_validation_error_redacts_inputs(
    tmp_path: Path,
) -> None:
    database_url = "mysql+pymysql://grader:mapping-password@db/grader"
    session_secret = "mapping-short-session"
    worker_shared_key = "mapping-valid-worker-key-" + "w" * 32
    admin_shared_key = "mapping-valid-admin-key-" + "a" * 32

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings.model_validate(
            UserDict(
                {
                    "environment": Environment.TEST,
                    "database_url": database_url,
                    "data_dir": tmp_path,
                    "session_secret": session_secret,
                    "worker_shared_key": worker_shared_key,
                "admin_shared_key": admin_shared_key,
                    "admin_shared_key": admin_shared_key,
                }
            )
        )

    assert_validation_error_redacts(
        exc_info.value,
        (database_url, session_secret, worker_shared_key, admin_shared_key),
    )


def test_mapping_production_validation_error_redacts_inputs(
    tmp_path: Path,
) -> None:
    database_url = "postgresql://grader:mapping-password@db/grader"
    session_secret = "mapping-production-session-" + "s" * 32
    worker_shared_key = "mapping-production-worker-" + "w" * 32
    admin_shared_key = "mapping-production-admin-" + "a" * 32

    with pytest.raises(
        ValidationError,
        match="production requires MySQL",
    ) as exc_info:
        ServerSettings.model_validate(
            UserDict(
                {
                    "environment": Environment.PRODUCTION,
                    "database_url": database_url,
                    "data_dir": tmp_path,
                    "session_secret": session_secret,
                    "worker_shared_key": worker_shared_key,
                "admin_shared_key": admin_shared_key,
                    "admin_shared_key": admin_shared_key,
                }
            )
        )

    assert_validation_error_redacts(
        exc_info.value,
        (database_url, session_secret, worker_shared_key, admin_shared_key),
    )


def test_sensitive_settings_json_schema_preserves_string_constraints() -> None:
    properties = ServerSettings.model_json_schema()["properties"]

    assert properties["database_url"]["type"] == "string"
    for field_name in ("session_secret", "worker_shared_key", "admin_shared_key"):
        field_schema = properties[field_name]
        assert field_schema["type"] == "string"
        assert field_schema["minLength"] == 32

    for field_name in (
        "database_url",
        "session_secret",
        "worker_shared_key",
        "admin_shared_key",
    ):
        field_schema = properties[field_name]
        assert "default" not in field_schema
        assert "examples" not in field_schema


@pytest.mark.parametrize(
    "field_name",
    ("database_url", "session_secret", "worker_shared_key", "admin_shared_key"),
)
def test_custom_string_sensitive_field_normalizes_to_builtin_string(
    field_name: str,
    tmp_path: Path,
) -> None:
    class CustomStr(str):
        pass

    expected_values = {
        "database_url": "mysql+pymysql://grader:custom-password@db/grader",
        "session_secret": "custom-session-secret-" + "s" * 32,
        "worker_shared_key": "custom-worker-key-" + "w" * 32,
        "admin_shared_key": "custom-admin-key-" + "a" * 32,
    }
    input_values = expected_values.copy()
    input_values[field_name] = CustomStr(input_values[field_name])

    settings = ServerSettings(
        environment=Environment.TEST,
        data_dir=tmp_path,
        **input_values,
    )

    for sensitive_field, expected_value in expected_values.items():
        actual_value = getattr(settings, sensitive_field)
        assert actual_value == expected_value
        assert type(actual_value) is str
        assert expected_value not in repr(settings)


@pytest.mark.parametrize(
    "field_name",
    ("database_url", "session_secret", "worker_shared_key", "admin_shared_key"),
)
@pytest.mark.parametrize(
    ("invalid_value", "sensitive_marker"),
    (
        pytest.param(987654321, "987654321", id="int"),
        pytest.param(b"known-invalid-bytes", "known-invalid-bytes", id="bytes"),
        pytest.param(["known-invalid-list"], "known-invalid-list", id="list"),
        pytest.param(
            {"secret": "known-invalid-dict"},
            "known-invalid-dict",
            id="dict",
        ),
    ),
)
def test_non_string_sensitive_input_raises_redacted_string_type_error(
    field_name: str,
    invalid_value: object,
    sensitive_marker: str,
    tmp_path: Path,
) -> None:
    sensitive_values: dict[str, object] = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "known-valid-session-secret-" + "s" * 32,
        "worker_shared_key": "known-valid-worker-key-" + "w" * 32,
        "admin_shared_key": "known-valid-admin-key-" + "a" * 32,
    }
    sensitive_values[field_name] = invalid_value

    with pytest.raises(ValidationError) as exc_info:
        ServerSettings(
            environment=Environment.TEST,
            data_dir=tmp_path,
            **sensitive_values,
        )

    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == (field_name,)
    assert errors[0]["type"] == "string_type"
    other_sensitive_values = tuple(
        str(value)
        for other_field_name, value in sensitive_values.items()
        if other_field_name != field_name
    )
    assert_validation_error_redacts(
        exc_info.value,
        (sensitive_marker, *other_sensitive_values),
    )


def test_env_example_documents_every_setting() -> None:
    """A new setting must be documented, or deployments silently miss it."""
    from pathlib import Path

    from server.config import ServerSettings
    from worker.config import WorkerSettings

    documented = {
        line.split("=", 1)[0].removeprefix("GRADER_").lower()
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    server_fields = set(ServerSettings.model_fields)
    assert server_fields.issubset(documented)
    worker_only = documented - server_fields
    assert {
        name
        for name in worker_only
        if not name.startswith("worker_")
        or name.removeprefix("worker_") not in WorkerSettings.model_fields
    } == set()
