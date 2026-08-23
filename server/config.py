from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    Field,
    ModelWrapValidatorHandler,
    PlainValidator,
    SecretStr,
    ValidationError,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict


_REDACTED_VALUE = "**********"

# Every field whose raw value must never reach a log line or a validation
# error. Keeping the list in one place means a new secret cannot be added to
# the model while quietly missing the redaction pass.
_SENSITIVE_FIELDS: tuple[str, ...] = (
    "database_url",
    "session_secret",
    "worker_shared_key",
    "admin_shared_key",
)


def _sanitize_error_context(value: Any) -> Any:
    if isinstance(value, BaseException):
        return str(value)
    if callable(getattr(value, "get_secret_value", None)):
        return _REDACTED_VALUE
    if isinstance(value, Mapping):
        return {key: _sanitize_error_context(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_sanitize_error_context(item) for item in value)
    if isinstance(value, list):
        return [_sanitize_error_context(item) for item in value]
    if isinstance(value, set):
        return {_sanitize_error_context(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_sanitize_error_context(item) for item in value)
    return value


def _unpack_sensitive_string(value: Any) -> str:
    if not isinstance(value, SecretStr):
        raise PydanticCustomError("string_type", "Input should be a valid string")
    raw_value = value.get_secret_value()
    if not isinstance(raw_value, str):
        raise PydanticCustomError("string_type", "Input should be a valid string")
    return str(raw_value)


def _validate_sensitive_secret(value: SecretStr) -> str:
    raw_value = _unpack_sensitive_string(value)
    if len(raw_value) < 32:
        raise PydanticCustomError(
            "string_too_short",
            "String should have at least {min_length} characters",
            {"min_length": 32},
        )
    return raw_value


_SensitiveString = Annotated[
    str,
    PlainValidator(_unpack_sensitive_string),
    WithJsonSchema({"type": "string"}),
]
_SensitiveSecret = Annotated[
    str,
    PlainValidator(_validate_sensitive_secret),
    WithJsonSchema({"type": "string", "minLength": 32}),
]


#: The Worker's own namespace. ``GRADER_`` is a *prefix* of ``GRADER_WORKER_``,
#: and both settings models default to reading the same ``.env``, so a
#: single-host deployment that legitimately keeps both halves of its
#: configuration in one file used to make this model unconstructable: every
#: ``GRADER_WORKER_INSTALLATION_ID``-style line looked like an extra input.
_WORKER_ENV_NAMESPACE = "grader_worker_"


def _drop_worker_namespace(data: dict[Any, Any]) -> dict[Any, Any]:
    """Drop Worker-owned keys, but keep this model's own typos failing.

    pydantic-settings strips the ``GRADER_`` prefix from variables that match a
    field, and passes through the full lowercased variable name for those that
    do not. A Worker variable therefore arrives here still carrying its
    namespace, as ``grader_worker_installation_id``.

    Only that namespace is skipped. An unrecognised ``GRADER_`` key outside it
    stays in place, so a misspelling such as ``GRADER_MAX_PDF_PAGE`` still fails
    loudly instead of silently falling back to the field default.

    ``GRADER_WORKER_SHARED_KEY`` is unaffected: it matches this model's
    ``worker_shared_key`` field, so it arrives with the prefix already stripped
    and never looks like part of the Worker's namespace. That one variable is
    deliberately shared, which is how the two ends agree on the key.
    """
    return {
        key: value
        for key, value in data.items()
        if not (isinstance(key, str) and key.lower().startswith(_WORKER_ENV_NAMESPACE))
    }


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRADER_",
        env_file=".env",
        hide_input_in_errors=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    database_url: _SensitiveString = Field(repr=False)
    data_dir: Path
    session_secret: _SensitiveSecret = Field(min_length=32, repr=False)
    worker_shared_key: _SensitiveSecret = Field(min_length=32, repr=False)
    #: Retired in Phase 07. No code path authenticates an admin with this key
    #: any more — Argon2id passwords over opaque cookie sessions replaced it —
    #: but the field stays so that an existing deployment's ``.env`` keeps
    #: loading. Phase 08 removes it along with the line in ``.env.example``.
    admin_shared_key: _SensitiveSecret = Field(min_length=32, repr=False)
    #: The exact origin the Admin SPA is served from. Every state-changing
    #: Admin request must carry a matching ``Origin`` header, so this is a
    #: security control, not a convenience: it is compared literally rather
    #: than by prefix or suffix, so ``localhost:5173.evil.example`` cannot
    #: match. Development serves the SPA from the Vite dev server; a deployment
    #: sets its real https origin.
    admin_origin: str = "http://localhost:5173"
    # ``price_cents_per_page`` remains as the annotated-review compatibility
    # fallback for existing deployments. New code reads the tier-specific
    # values below and snapshots the selected rule onto every quote.
    price_cents_per_page: int = Field(default=500, ge=1)
    summary_price_cents_per_page: int = Field(default=100, ge=1)
    annotated_price_cents_per_page: int = Field(default=500, ge=1)
    summary_report_enabled: bool = False
    max_pdf_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    max_pdf_pages: int = Field(default=30, ge=1)
    quote_ttl_seconds: int = Field(default=86400, ge=60)
    acceptance_ttl_seconds: int = Field(default=259200, ge=60)

    @model_validator(mode="before")
    @classmethod
    def redact_sensitive_inputs(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data

        redacted_data = _drop_worker_namespace(dict(data))
        for field_name in _SENSITIVE_FIELDS:
            value = redacted_data.get(field_name)
            if value is not None and not isinstance(value, SecretStr):
                redacted_data[field_name] = SecretStr(value)
        return redacted_data

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
            sanitized_line_errors = []
            for line_error in error.errors(
                include_input=False,
                include_url=False,
            ):
                sanitized_line_error = {
                    **line_error,
                    "input": _REDACTED_VALUE,
                }
                if "ctx" in sanitized_line_error:
                    sanitized_line_error["ctx"] = _sanitize_error_context(
                        sanitized_line_error["ctx"]
                    )
                sanitized_line_error["type"] = PydanticCustomError(
                    line_error["type"],
                    line_error["msg"],
                    sanitized_line_error.get("ctx"),
                )
                sanitized_line_errors.append(sanitized_line_error)
            sanitized_error = ValidationError.from_exception_data(
                cls.__name__,
                sanitized_line_errors,
                hide_input=True,
            )
        raise sanitized_error

    @field_validator("database_url")
    @classmethod
    def validate_database_url(
        cls,
        database_url: str,
        info: ValidationInfo,
    ) -> str:
        if info.data.get("environment") is Environment.PRODUCTION:
            if not database_url.startswith("mysql+pymysql://"):
                raise ValueError("production requires MySQL")
        return database_url
