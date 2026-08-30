"""Lab findings report contract: JSON schema + Markdown template (GH #82).

Owns validation and serialization only. CLI (`lab run` / `lab batch`) and
worktree job allocation are separate issues.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from worktrees_hives.errors import FindingsValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

# Findings report document schema (independent of the wh CLI envelope).
FINDINGS_SCHEMA_VERSION: int = 1

# Required Markdown section titles (ATX headings, case-insensitive match on text).
REQUIRED_MD_SECTIONS: tuple[str, ...] = (
    "Hypothesis",
    "Method",
    "Discoveries",
    "Null results",
    "Errors",
    "Evidence",
    "Attribution",
)

# `\s` here would also match newlines, letting `#\nfoo` span lines and fabricate
# a heading from invalid Markdown, so only spaces/tabs separate the hashes.
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Opening fences may carry an info string (```` ```python ````); closing fences
# may only be followed by whitespace. Treating a marker line with trailing text
# as a closer would end the block early and leak its headings as sections.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})", re.MULTILINE)
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$", re.MULTILINE)


class FindingType(StrEnum):
    """Kind of a single finding entry."""

    DISCOVERY = "discovery"
    NULL_RESULT = "null_result"
    ERROR = "error"


class AgentRole(StrEnum):
    """Who produced the report."""

    AGENT = "agent"
    SUBAGENT = "subagent"


class ReportStatus(StrEnum):
    """Overall outcome of the hypothesis run."""

    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class Finding:
    """One structured finding line item."""

    type: FindingType
    summary: str
    detail: str | None = None
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-compatible dict."""
        out: dict[str, Any] = {
            "type": str(self.type),
            "summary": self.summary,
            "evidence": list(self.evidence),
        }
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Finding:
        """Parse one finding object; fail closed on bad types."""
        if not isinstance(raw, dict):
            raise FindingsValidationError(f"finding must be an object, got {type(raw).__name__}")
        ftype = raw.get("type")
        if not isinstance(ftype, str):
            raise FindingsValidationError("finding.type is required and must be a string")
        try:
            kind = FindingType(ftype)
        except ValueError as exc:
            raise FindingsValidationError(
                f"finding.type must be one of {[e.value for e in FindingType]}, got {ftype!r}"
            ) from exc
        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise FindingsValidationError("finding.summary is required non-empty string")
        detail = raw.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise FindingsValidationError("finding.detail must be a string or omitted")
        evidence_raw = raw.get("evidence", [])
        if not isinstance(evidence_raw, list) or not all(isinstance(x, str) for x in evidence_raw):
            raise FindingsValidationError("finding.evidence must be a list of strings")
        return cls(
            type=kind,
            summary=summary.strip(),
            detail=detail,
            evidence=tuple(evidence_raw),
        )


@dataclass(frozen=True, slots=True)
class FindingsReport:
    """Versioned lab findings document (JSON side of the contract)."""

    hypothesis_id: str
    agent_id: str
    role: AgentRole
    worktree: str
    status: ReportStatus
    findings: tuple[Finding, ...] = ()
    artifacts: tuple[str, ...] = ()
    budgets: Mapping[str, Any] | None = None
    schema_version: int = FINDINGS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-compatible dict."""
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "agent_id": self.agent_id,
            "role": str(self.role),
            "worktree": self.worktree,
            "status": str(self.status),
            "findings": [f.to_dict() for f in self.findings],
            "artifacts": list(self.artifacts),
        }
        if self.budgets is not None:
            out["budgets"] = dict(self.budgets)
        return out

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize as JSON text."""
        try:
            return (
                json.dumps(self.to_dict(), indent=indent, sort_keys=False, allow_nan=False) + "\n"
            )
        except ValueError as exc:
            raise FindingsValidationError(f"cannot serialize report to JSON: {exc}") from exc

    def to_markdown(self) -> str:
        """Render the canonical Markdown template filled from this report."""
        discoveries = [f for f in self.findings if f.type == FindingType.DISCOVERY]
        nulls = [f for f in self.findings if f.type == FindingType.NULL_RESULT]
        errors = [f for f in self.findings if f.type == FindingType.ERROR]

        def _bullets(items: list[Finding]) -> str:
            if not items:
                return "_None._\n"
            lines: list[str] = []
            for item in items:
                line = f"- **{_escape_md_inline(item.summary)}**"
                if item.detail:
                    line += f" — {_escape_md_inline(item.detail)}"
                lines.append(line)
                for ev in item.evidence:
                    lines.append(f"  - evidence: {_escape_md_inline(ev)}")
            return "\n".join(lines) + "\n"

        evidence_lines: list[str] = []
        for item in self.findings:
            for ev in item.evidence:
                evidence_lines.append(f"- {_escape_md_inline(ev)}")
        for art in self.artifacts:
            evidence_lines.append(f"- artifact: {_escape_md_inline(art)}")
        evidence_body = "\n".join(evidence_lines) + "\n" if evidence_lines else "_None._\n"

        return (
            f"# Hypothesis\n\n"
            f"`{_escape_md_code(self.hypothesis_id)}`\n\n"
            f"# Method\n\n"
            f"Role: `{self.role}` · Agent: `{_escape_md_code(self.agent_id)}` · "
            f"Worktree: `{_escape_md_code(self.worktree)}` · Status: `{self.status}`\n\n"
            f"# Discoveries\n\n"
            f"{_bullets(discoveries)}\n"
            f"# Null results\n\n"
            f"{_bullets(nulls)}\n"
            f"# Errors\n\n"
            f"{_bullets(errors)}\n"
            f"# Evidence\n\n"
            f"{evidence_body}\n"
            f"# Attribution\n\n"
            f"— {_escape_md_inline(self.agent_id)} ({self.role}) · "
            f"worktree `{_escape_md_code(self.worktree)}`\n"
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FindingsReport:
        """Parse and validate a findings JSON object (fail closed)."""
        if not isinstance(raw, dict):
            raise FindingsValidationError(f"report must be an object, got {type(raw).__name__}")

        _SCHEMA_VERSION_SENTINEL = object()
        schema_version = raw.get("schema_version", _SCHEMA_VERSION_SENTINEL)
        if schema_version is _SCHEMA_VERSION_SENTINEL:
            raise FindingsValidationError("schema_version is required")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise FindingsValidationError("schema_version must be an int")
        if schema_version != FINDINGS_SCHEMA_VERSION:
            raise FindingsValidationError(
                f"unsupported schema_version {schema_version} (expected {FINDINGS_SCHEMA_VERSION})"
            )

        hypothesis_id = _require_nonempty_str(raw, "hypothesis_id")
        agent_id = _require_nonempty_str(raw, "agent_id")
        worktree = _require_nonempty_str(raw, "worktree")

        role_raw = raw.get("role")
        if not isinstance(role_raw, str):
            raise FindingsValidationError("role is required and must be a string")
        try:
            role = AgentRole(role_raw)
        except ValueError as exc:
            raise FindingsValidationError(
                f"role must be one of {[e.value for e in AgentRole]}, got {role_raw!r}"
            ) from exc

        status_raw = raw.get("status")
        if not isinstance(status_raw, str):
            raise FindingsValidationError("status is required and must be a string")
        try:
            status = ReportStatus(status_raw)
        except ValueError as exc:
            raise FindingsValidationError(
                f"status must be one of {[e.value for e in ReportStatus]}, got {status_raw!r}"
            ) from exc

        findings_raw = raw.get("findings")
        if findings_raw is None:
            raise FindingsValidationError("findings is required (use [] if empty)")
        if not isinstance(findings_raw, list):
            raise FindingsValidationError("findings must be a list")
        findings = tuple(Finding.from_dict(item) for item in findings_raw)

        artifacts_raw = raw.get("artifacts")
        if artifacts_raw is None:
            raise FindingsValidationError("artifacts is required (use [] if empty)")
        if not isinstance(artifacts_raw, list) or not all(
            isinstance(x, str) for x in artifacts_raw
        ):
            raise FindingsValidationError("artifacts must be a list of strings")

        budgets_raw = raw.get("budgets")
        if budgets_raw is not None and not isinstance(budgets_raw, dict):
            raise FindingsValidationError("budgets must be an object or omitted")
        # Shallow-copy then freeze so callers cannot mutate report.budgets via the input dict.
        budgets = MappingProxyType(dict(budgets_raw)) if budgets_raw is not None else None

        return cls(
            hypothesis_id=hypothesis_id,
            agent_id=agent_id,
            role=role,
            worktree=worktree,
            status=status,
            findings=findings,
            artifacts=tuple(artifacts_raw),
            budgets=budgets,
            schema_version=schema_version,
        )


def parse_findings_json(text: str) -> FindingsReport:
    """Decode JSON text into a validated :class:`FindingsReport`."""
    if not text or not text.strip():
        raise FindingsValidationError("findings JSON is empty")

    def _reject_non_finite(value: str) -> float:
        raise FindingsValidationError(f"findings JSON contains non-finite number: {value}")

    try:
        raw = json.loads(text, parse_constant=_reject_non_finite)
    except json.JSONDecodeError as exc:
        raise FindingsValidationError(f"findings JSON is not valid JSON: {exc}") from exc
    except FindingsValidationError:
        raise
    if not isinstance(raw, dict):
        raise FindingsValidationError("findings JSON root must be an object")
    return FindingsReport.from_dict(raw)


def validate_findings_markdown(text: str) -> None:
    """Fail closed if Markdown is missing required section headings."""
    if not text or not text.strip():
        raise FindingsValidationError("findings Markdown is empty")
    headings = _extract_headings_outside_code_blocks(text)
    missing = [s for s in REQUIRED_MD_SECTIONS if _normalize_heading(s) not in headings]
    if missing:
        raise FindingsValidationError(
            "findings Markdown missing required sections: " + ", ".join(missing)
        )


def load_findings_pair(
    json_path: str | Path,
    markdown_path: str | Path,
) -> FindingsReport:
    """Load and validate both sides of the contract; fail if either is missing/invalid.

    Returns the parsed JSON report after both JSON and Markdown validate.
    """
    jpath = Path(json_path)
    mpath = Path(markdown_path)
    if not jpath.is_file():
        raise FindingsValidationError(f"findings JSON missing: {jpath}")
    if not mpath.is_file():
        raise FindingsValidationError(f"findings Markdown missing: {mpath}")
    try:
        json_text = jpath.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FindingsValidationError(f"findings JSON is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise FindingsValidationError(f"findings JSON unreadable: {exc}") from exc
    try:
        md_text = mpath.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FindingsValidationError(f"findings Markdown is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise FindingsValidationError(f"findings Markdown unreadable: {exc}") from exc
    report = parse_findings_json(json_text)
    validate_findings_markdown(md_text)
    return report


def write_findings_pair(
    report: FindingsReport,
    json_path: str | Path,
    markdown_path: str | Path,
) -> None:
    """Write validated JSON + rendered Markdown for a report.

    Both payloads are fully prepared and validated **before** any file is written,
    so a validation failure never leaves a JSON-only half pair on disk.
    """
    jpath = Path(json_path)
    mpath = Path(markdown_path)
    json_text = report.to_json()
    md = report.to_markdown()
    validate_findings_markdown(md)
    jpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text(json_text, encoding="utf-8")
    mpath.write_text(md, encoding="utf-8")


def empty_findings_markdown_template() -> str:
    """Return the empty Markdown skeleton with all required sections."""
    parts = [f"# {title}\n\n_TODO._\n" for title in REQUIRED_MD_SECTIONS]
    return "\n".join(parts)


def _require_nonempty_str(raw: dict[str, Any], key: str) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise FindingsValidationError(f"{key} is required non-empty string")
    return val.strip()


def _extract_headings_outside_code_blocks(text: str) -> set[str]:
    """Extract normalized ATX headings, ignoring those inside fenced code blocks."""
    # Find all fence positions
    fences = list(_FENCE_OPEN_RE.finditer(text))
    closes = list(_FENCE_CLOSE_RE.finditer(text))
    code_block_ranges: list[tuple[int, int]] = []

    # Pair up fences to find code block ranges
    i = 0
    while i < len(fences):
        start_fence = fences[i]
        start_pos = start_fence.start()
        fence_type = start_fence.group(1)[0]
        fence_length = len(start_fence.group(1))

        # Find matching closing fence: same marker type, at least the opener's
        # length, and no trailing text after the marker.
        close = None
        for c in closes:
            same_type = c.group(1)[0] == fence_type
            long_enough = len(c.group(1)) >= fence_length
            if c.start() > start_fence.end() and same_type and long_enough:
                close = c
                break
        if close is not None:
            code_block_ranges.append((start_pos, close.end()))
            # Resume after the closing fence; it must not be re-parsed as an
            # opener of a new block.
            i += 1
            while i < len(fences) and fences[i].start() < close.end():
                i += 1
        else:
            # No closing fence found, treat rest of document as code block
            code_block_ranges.append((start_pos, len(text)))
            break

    def _is_in_code_block(pos: int) -> bool:
        return any(start <= pos < end for start, end in code_block_ranges)

    headings: set[str] = set()
    for match in _HEADING_RE.finditer(text):
        if not _is_in_code_block(match.start()):
            heading_text = match.group(2)
            # Strip optional trailing hashes (ATX closing sequence)
            heading_text = re.sub(r"\s*#+\s*$", "", heading_text)
            headings.add(_normalize_heading(heading_text))

    return headings


def _normalize_heading(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _escape_md_inline(text: str) -> str:
    """Escape characters that break bold/list Markdown when embedding free text."""
    return (
        text.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _escape_md_code(text: str) -> str:
    """Escape backticks inside inline code spans."""
    return text.replace("`", "'")
