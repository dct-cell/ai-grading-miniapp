from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlparse

from pydantic import (
    Field,
    ModelWrapValidatorHandler,
    PlainValidator,
    SecretStr,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict


_REDACTED_VALUE = "**********"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MINIMUM_SHARED_KEY_CHARS = 32

#: The server's namespace, mirroring ``server.config``. ``GRADER_`` is a prefix
#: of this model's ``GRADER_WORKER_``, and both models default to reading the
#: same ``.env``. A Worker whose working directory holds a full deployment
#: ``.env`` used to refuse to start at all, because each server variable looked
#: like an extra input. The rule is duplicated rather than imported because
#: ``worker/`` must not depend on ``server/``.
_SERVER_ENV_PREFIX = "grader_"
_WORKER_ENV_PREFIX = "grader_worker_"


def _drop_server_namespace(data: dict[Any, Any]) -> dict[Any, Any]:
    """Drop server-owned keys, but keep this model's own typos failing.

    This model's real fields arrive with the ``GRADER_WORKER_`` prefix already
    stripped (``installation_id``), so anything still carrying a ``grader_``
    prefix belongs to somebody else. Keys under ``grader_worker_`` are kept:
    an unrecognised one is a misspelling of a Worker setting and must fail
    loudly rather than silently fall back to a default.
    """
    return {
        key: value
        for key, value in data.items()
        if not (
            isinstance(key, str)
            and key.lower().startswith(_SERVER_ENV_PREFIX)
            and not key.lower().startswith(_WORKER_ENV_PREFIX)
        )
    }


def _unpack_secret(value: Any) -> str:
    if not isinstance(value, SecretStr):
        raise PydanticCustomError("string_type", "Input should be a valid string")
    raw_value = value.get_secret_value()
    if not isinstance(raw_value, str):
        raise PydanticCustomError("string_type", "Input should be a valid string")
    if len(raw_value) < MINIMUM_SHARED_KEY_CHARS:
        raise PydanticCustomError(
            "string_too_short",
            "String should have at least {min_length} characters",
            {"min_length": MINIMUM_SHARED_KEY_CHARS},
        )
    return str(raw_value)


_SensitiveSecret = Annotated[
    str,
    PlainValidator(_unpack_secret),
    WithJsonSchema({"type": "string", "minLength": MINIMUM_SHARED_KEY_CHARS}),
]


class WorkerSettings(BaseSettings):
    """Local Worker configuration.

    The shared key and installation id live only in the protected local config
    written by the installer; they are never shipped inside a distributed
    package. The key is wrapped so that no error path or repr can echo it.
    """

    model_config = SettingsConfigDict(
        env_prefix="GRADER_WORKER_",
        env_file=".env",
        hide_input_in_errors=True,
    )

    server_base_url: str
    shared_key: _SensitiveSecret = Field(repr=False)
    installation_id: str = Field(min_length=1, max_length=64)
    worker_id: str | None = None
    workspace_root: Path
    device_name: str = Field(default="", max_length=128)
    worker_version: str = "3.0.0"
    # Production-safe default. The local demo constructs FakeGrader explicitly;
    # a deployed Worker must never silently deliver placeholder scoring.
    runtime_mode: Literal["fake", "codex"] = "codex"
    codex_bin: str = "codex"
    grading_timeout_seconds: int = Field(default=60 * 60, ge=60, le=2 * 60 * 60)
    poll_wait_seconds: int = Field(default=25, ge=0, le=25)
    renew_interval_seconds: int = Field(default=20, ge=1)
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    # Reserved for the Harness runtime (parallel subagents). The legacy
    # Codex runtime is single-process and ignores this value; setting it
    # wider than 1 only takes effect once a Harness implementation is
    # wired up. Allowed range is 1..3 per the Phase 04 contract.
    max_codex_sessions_per_job: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="before")
    @classmethod
    def wrap_sensitive_inputs(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        wrapped = _drop_server_namespace(dict(data))
        value = wrapped.get("shared_key")
        if value is not None and not isinstance(value, SecretStr):
            wrapped["shared_key"] = SecretStr(value)
        return wrapped

    @model_validator(mode="wrap")
    @classmethod
    def sanitize_validation_errors(
        cls,
        data: Any,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        try:
            return handler(data)
        except ValidationError as error:
            sanitized = []
            for line_error in error.errors(include_input=False, include_url=False):
                entry = {**line_error, "input": _REDACTED_VALUE}
                entry["type"] = PydanticCustomError(
                    line_error["type"],
                    line_error["msg"],
                    entry.get("ctx"),
                )
                sanitized.append(entry)
            rebuilt = ValidationError.from_exception_data(
                cls.__name__,
                sanitized,
                hide_input=True,
            )
        raise rebuilt

    @field_validator("server_base_url")
    @classmethod
    def require_https_off_localhost(cls, server_base_url: str) -> str:
        parsed = urlparse(server_base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("server base url must be http or https")
        if not parsed.hostname:
            raise ValueError("server base url must include a host")
        if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
            raise ValueError("plain http is only allowed for a local server")
        return server_base_url.rstrip("/")
