"""Typed response envelopes matching the supported Rust wh-core boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Schema versions must match wh-core contract::{SCHEMA_VERSION, EXACT_BASE_SCHEMA_VERSION}.
SCHEMA_VERSION: int = 1
EXACT_BASE_SCHEMA_VERSION: int = 2
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({SCHEMA_VERSION, EXACT_BASE_SCHEMA_VERSION})

_CANONICAL_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FULL_COMMIT_REQUEST_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_HEX_START_POINT_RE = re.compile(r"^[0-9a-fA-F]+$")


def validate_canonical_commit(value: object, *, field_name: str) -> str:
    """Return a canonical full object id or raise ``WhSchemaError``."""
    from worktrees_hives.errors import WhSchemaError

    if not isinstance(value, str) or not _CANONICAL_COMMIT_RE.fullmatch(value):
        raise WhSchemaError(
            f"{field_name} must be a canonical lowercase 40- or 64-character commit id"
        )
    return value


def validate_start_point_request(start_point: str) -> str:
    """Return a normalized start point, rejecting ambiguous object ids.

    A caller-supplied all-hex value is interpreted as an object id and must be
    a full SHA-1 or SHA-256 id. Full ids are normalized to lowercase; symbolic
    refs are returned unchanged and resolved by the Rust boundary.
    """
    from worktrees_hives.errors import WhSchemaError

    if _FULL_COMMIT_REQUEST_RE.fullmatch(start_point):
        return start_point.lower()
    if _HEX_START_POINT_RE.fullmatch(start_point):
        raise WhSchemaError("all-hex start_point must be a full 40- or 64-character object id")
    return start_point


def require_verified_worktree_commits(data: dict[str, Any], *, requested_start_point: str) -> str:
    """Validate and return the Rust-verified worktree commit identity.

    Both v2 commit-identity fields are mandatory for worktree-create consumers. The
    verified worker HEAD must exactly equal the resolved start commit. When the
    request itself was a full object id, the response must equal it exactly;
    symbolic refs are intentionally compared only after Rust resolves them.
    """
    from worktrees_hives.errors import WhSchemaError

    normalized_start_point = validate_start_point_request(requested_start_point)
    start_commit = validate_canonical_commit(
        data.get("start_commit"), field_name="worktree.create data.start_commit"
    )
    head_commit = validate_canonical_commit(
        data.get("head_commit"), field_name="worktree.create data.head_commit"
    )
    if head_commit != start_commit:
        raise WhSchemaError("worktree.create verified head_commit does not equal start_commit")
    if (
        _CANONICAL_COMMIT_RE.fullmatch(normalized_start_point)
        and start_commit != normalized_start_point
    ):
        raise WhSchemaError(
            "worktree.create start_commit does not equal the requested full object id"
        )
    return start_commit


@dataclass(frozen=True, slots=True)
class WorktreeCreateRequest:
    """Typed inputs for the exact-base v2 ``wh worktree create`` command."""

    owner: str
    repo: str
    job_id: str
    branch: str
    start_point: str

    def cli_args(self, repo_root: str) -> tuple[str, ...]:
        """Return the structured argv consumed by :class:`WhClient`."""
        return (
            "worktree",
            "create",
            "--schema-version",
            str(EXACT_BASE_SCHEMA_VERSION),
            "--repo",
            repo_root,
            "--start-point",
            self.start_point,
            self.owner,
            self.repo,
            self.job_id,
            self.branch,
        )


@dataclass(frozen=True, slots=True)
class VerifiedWorktreeCreation:
    """Required, validated identity from an exact-base v2 create response."""

    path: str
    branch: str
    branch_ref: str
    start_commit: str
    worktree_registered: bool


def _required_nonempty_string(value: object, *, field_name: str) -> str:
    from worktrees_hives.errors import WhSchemaError

    if not isinstance(value, str) or not value:
        raise WhSchemaError(f"{field_name} must be a non-empty string")
    return value


def parse_verified_worktree_creation(
    data: dict[str, Any],
    *,
    requested_start_point: str,
    expected_path: str,
    expected_branch: str,
) -> VerifiedWorktreeCreation:
    """Validate the complete exact-base creation and registration identity."""
    from worktrees_hives.errors import WhSchemaError

    start_commit = require_verified_worktree_commits(
        data, requested_start_point=requested_start_point
    )
    path = _required_nonempty_string(data.get("path"), field_name="worktree.create data.path")
    branch = _required_nonempty_string(data.get("branch"), field_name="worktree.create data.branch")
    branch_ref = _required_nonempty_string(
        data.get("branch_ref"), field_name="worktree.create data.branch_ref"
    )
    expected_branch_ref = f"refs/heads/{expected_branch}"
    if Path(path).resolve() != Path(expected_path).resolve():
        raise WhSchemaError("worktree.create path does not equal the expected worktree path")
    if branch != expected_branch or branch_ref != expected_branch_ref:
        raise WhSchemaError("worktree.create branch identity does not equal the request")
    if data.get("worktree_registered") is not True:
        raise WhSchemaError("worktree.create did not prove the expected worktree registration")
    return VerifiedWorktreeCreation(
        path=path,
        branch=branch,
        branch_ref=branch_ref,
        start_commit=start_commit,
        worktree_registered=True,
    )


@dataclass(frozen=True, slots=True)
class ErrorData:
    """Structured error payload from a failed wh command."""

    code: str
    message: str


def _parse_envelope_object(raw: object) -> dict[str, Any]:
    from worktrees_hives.errors import WhSchemaError

    if not isinstance(raw, dict):
        raise WhSchemaError(f"Expected a JSON object, got {type(raw).__name__}")
    return raw


def _require_field(raw: dict[str, Any], field_name: str) -> object:
    from worktrees_hives.errors import WhSchemaError

    value = raw.get(field_name)
    if value is None:
        raise WhSchemaError(f"'{field_name}' is required")
    return value


def _parse_ok(raw: dict[str, Any]) -> bool:
    from worktrees_hives.errors import WhSchemaError

    ok = _require_field(raw, "ok")
    if not isinstance(ok, bool):
        raise WhSchemaError(f"'ok' must be a bool, got {type(ok).__name__}")
    return ok


def _parse_schema_version(raw: dict[str, Any]) -> int:
    from worktrees_hives.errors import WhSchemaError

    schema_version = _require_field(raw, "schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise WhSchemaError(f"'schema_version' must be an int, got {type(schema_version).__name__}")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise WhSchemaError(
            "Unsupported schema version: "
            f"{schema_version} (supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    return schema_version


def _parse_command(raw: dict[str, Any]) -> str:
    from worktrees_hives.errors import WhSchemaError

    command = _require_field(raw, "command")
    if not isinstance(command, str):
        raise WhSchemaError(f"'command' must be a str, got {type(command).__name__}")
    return command


def _parse_data(raw: dict[str, Any]) -> dict[str, Any]:
    from worktrees_hives.errors import WhSchemaError

    data = _require_field(raw, "data")
    if not isinstance(data, dict):
        raise WhSchemaError(f"'data' must be a dict, got {type(data).__name__}")
    return data


def _parse_error(raw: dict[str, Any]) -> ErrorData | None:
    from worktrees_hives.errors import WhSchemaError

    error_raw = raw.get("error")
    if error_raw is None:
        return None
    if not isinstance(error_raw, dict):
        raise WhSchemaError(f"'error' must be a dict or null, got {type(error_raw).__name__}")
    code = error_raw.get("code")
    message = error_raw.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise WhSchemaError("'error.code' and 'error.message' must be strings")
    return ErrorData(code=code, message=message)


def _validate_ok_error_exclusivity(ok: bool, error: ErrorData | None) -> None:
    from worktrees_hives.errors import WhSchemaError

    if error is None:
        if not ok:
            raise WhSchemaError("'error' is required when ok is false")
        return
    if ok:
        raise WhSchemaError("'error' must be null when ok is true")


@dataclass(frozen=True, slots=True)
class Response:
    """A supported JSON envelope returned by `wh --json`."""

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
        envelope = _parse_envelope_object(raw)
        ok = _parse_ok(envelope)
        schema_version = _parse_schema_version(envelope)
        command = _parse_command(envelope)
        data = _parse_data(envelope)
        error = _parse_error(envelope)
        _validate_ok_error_exclusivity(ok, error)

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
    data: dict[str, Any] = field(default_factory=dict)


def classify(response: Response) -> SuccessResponse | ErrorResponse:
    """Lift a Response into a typed Success or Error variant."""
    if response.ok:
        return SuccessResponse(
            command=response.command,
            data=response.data,
            schema_version=response.schema_version,
        )
    # error must be present when ok=False for every supported boundary.
    if response.error is None:
        from worktrees_hives.errors import WhSchemaError

        raise WhSchemaError("'error' is required when 'ok' is false")
    return ErrorResponse(
        command=response.command,
        error=response.error,
        schema_version=response.schema_version,
        data=response.data,
    )
