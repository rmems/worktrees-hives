"""Typed response envelope matching the Rust wh-core contract v1."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Schema version must match wh-core contract::SCHEMA_VERSION.
SCHEMA_VERSION: int = 1

_CANONICAL_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HEX_START_POINT_RE = re.compile(r"^[0-9a-fA-F]+$")


def validate_canonical_commit(value: object, *, field_name: str) -> str:
    """Return a canonical full object id or raise ``WhSchemaError``."""
    from worktrees_hives.errors import WhSchemaError

    if not isinstance(value, str) or not _CANONICAL_COMMIT_RE.fullmatch(value):
        raise WhSchemaError(
            f"{field_name} must be a canonical lowercase 40- or 64-character commit id"
        )
    return value


def validate_start_point_request(start_point: str) -> None:
    """Reject ambiguous abbreviated object ids while allowing symbolic refs.

    A caller-supplied all-hex value is interpreted as an object id and must be
    a full SHA-1 or SHA-256 id. Symbolic refs are resolved by the Rust boundary.
    """
    from worktrees_hives.errors import WhSchemaError

    if _HEX_START_POINT_RE.fullmatch(start_point) and not _CANONICAL_COMMIT_RE.fullmatch(
        start_point
    ):
        raise WhSchemaError(
            "all-hex start_point must be a full lowercase 40- or 64-character object id"
        )


def require_verified_worktree_commits(data: dict[str, Any], *, requested_start_point: str) -> str:
    """Validate and return the Rust-verified worktree commit identity.

    Both additive v1 fields are mandatory for worktree-create consumers. The
    verified worker HEAD must exactly equal the resolved start commit. When the
    request itself was a full object id, the response must equal it exactly;
    symbolic refs are intentionally compared only after Rust resolves them.
    """
    from worktrees_hives.errors import WhSchemaError

    validate_start_point_request(requested_start_point)
    start_commit = validate_canonical_commit(
        data.get("start_commit"), field_name="worktree.create data.start_commit"
    )
    head_commit = validate_canonical_commit(
        data.get("head_commit"), field_name="worktree.create data.head_commit"
    )
    if head_commit != start_commit:
        raise WhSchemaError("worktree.create verified head_commit does not equal start_commit")
    if (
        _CANONICAL_COMMIT_RE.fullmatch(requested_start_point)
        and start_commit != requested_start_point
    ):
        raise WhSchemaError(
            "worktree.create start_commit does not equal the requested full object id"
        )
    return start_commit


@dataclass(frozen=True, slots=True)
class ErrorData:
    """Structured error payload from a failed wh command."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Response:
    """The v1 JSON envelope returned by `wh --json`."""

    ok: bool
    schema_version: int
    command: str
    data: dict[str, Any]
    error: ErrorData | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Response:
        """Parse a raw dict (already JSON-decoded) into a Response.

        Raises WhSchemaError if required fields are missing or malformed.
        """
        from worktrees_hives.errors import WhSchemaError

        if not isinstance(raw, dict):
            raise WhSchemaError(f"Expected a JSON object, got {type(raw).__name__}")

        ok = raw.get("ok")
        if ok is None:
            raise WhSchemaError("'ok' is required")
        if not isinstance(ok, bool):
            raise WhSchemaError(f"'ok' must be a bool, got {type(ok).__name__}")

        schema_version = raw.get("schema_version")
        if schema_version is None:
            raise WhSchemaError("'schema_version' is required")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise WhSchemaError(
                f"'schema_version' must be an int, got {type(schema_version).__name__}"
            )
        if schema_version != SCHEMA_VERSION:
            raise WhSchemaError(
                f"Unsupported schema version: {schema_version} (expected {SCHEMA_VERSION})"
            )

        command = raw.get("command")
        if command is None:
            raise WhSchemaError("'command' is required")
        if not isinstance(command, str):
            raise WhSchemaError(f"'command' must be a str, got {type(command).__name__}")

        data = raw.get("data")
        if data is None:
            raise WhSchemaError("'data' is required")
        if not isinstance(data, dict):
            raise WhSchemaError(f"'data' must be a dict, got {type(data).__name__}")

        error_raw = raw.get("error")
        error: ErrorData | None = None
        if error_raw is not None:
            if not isinstance(error_raw, dict):
                raise WhSchemaError(
                    f"'error' must be a dict or null, got {type(error_raw).__name__}"
                )
            code = error_raw.get("code")
            message = error_raw.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                raise WhSchemaError("'error.code' and 'error.message' must be strings")
            error = ErrorData(code=code, message=message)

        # ok/error exclusivity: success envelopes must not carry error payloads;
        # failure envelopes must include a structured error object.
        if ok and error is not None:
            raise WhSchemaError("'error' must be null when ok is true")
        if not ok and error is None:
            raise WhSchemaError("'error' is required when ok is false")

        return cls(
            ok=ok,
            schema_version=schema_version,
            command=command,
            data=data,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class SuccessResponse:
    """Convenience wrapper for a successful Response."""

    command: str
    data: dict[str, Any]
    schema_version: int


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    """Convenience wrapper for a failed Response."""

    command: str
    error: ErrorData
    schema_version: int


def classify(response: Response) -> SuccessResponse | ErrorResponse:
    """Lift a Response into a typed Success or Error variant."""
    if response.ok:
        return SuccessResponse(
            command=response.command,
            data=response.data,
            schema_version=response.schema_version,
        )
    # error must be present when ok=False per the v1 contract.
    if response.error is None:
        from worktrees_hives.errors import WhSchemaError

        raise WhSchemaError("'error' is required when 'ok' is false")
    return ErrorResponse(
        command=response.command,
        error=response.error,
        schema_version=response.schema_version,
    )
