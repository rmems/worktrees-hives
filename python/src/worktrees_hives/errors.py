"""Custom exceptions for worktrees-hives subprocess bridge."""

from __future__ import annotations


class WhError(Exception):
    """Base exception for all wh CLI errors."""


class WhBinaryNotFoundError(WhError):
    """Raised when the wh binary cannot be located on PATH or WH_BIN."""

    def __init__(self, detail: str | None = None) -> None:
        msg = detail or (
            "wh binary not found. Install wh (cargo install --path crates/wh) "
            "or set WH_BIN to the full path."
        )
        super().__init__(msg)


class WhProcessError(WhError):
    """Raised when wh exits with a non-zero status code."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"wh exited with code {returncode}: {stderr}")


class WhJsonDecodeError(WhError):
    """Raised when wh output is not valid JSON."""

    def __init__(self, raw: str, cause: Exception | None = None) -> None:
        self.raw = raw
        suffix = f": {cause}" if cause is not None else ""
        super().__init__(f"Failed to decode wh JSON output{suffix}")


class WhSchemaError(WhError):
    """Raised when the wh response envelope does not match the v1 schema."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Invalid v1 envelope: {detail}")


class PolicyError(WhError):
    """Raised when wh exits with code 2 (policy violation)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Policy violation [{code}]: {message}")


class FindingsValidationError(WhError):
    """Raised when a lab findings report (JSON and/or Markdown) is invalid."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid findings report: {detail}")


class ResearchValidationError(WhError):
    """Raised when a Research Hive experiment contract is invalid."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid research contract: {detail}")


class ResearchRoleValidationError(WhError):
    """Raised when a Research Hive role document is invalid."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid research role: {detail}")


class RoleCapabilityError(PolicyError):
    """Raised when a research role is denied a capability or command."""

    def __init__(self, role_id: str, capability: str) -> None:
        self.role_id = role_id
        self.capability = capability
        super().__init__(
            "ROLE_CAPABILITY_DENIED",
            f"role {role_id!r} is not allowed to {capability}",
        )


class AggregateValidationError(WhError):
    """Raised when an aggregate discoveries report (JSON and/or Markdown) is invalid."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid aggregate report: {detail}")
