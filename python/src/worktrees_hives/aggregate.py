"""Aggregate discoveries report format: Markdown + table (GH #16).

Owns the **format only** for the multi-run discoveries report: summary counts,
one table row per hypothesis run linking to its per-run ``findings.md``, an
explicit failures section, and the optional versioned aggregate JSON shape.

Does **not** own batch scheduling (#83 invokes this format), the per-run
findings schema (#82), or the single-run CLI (#80). Module ownership per the
#23 map: ``worktrees_hives.aggregate``.

**Never merges.** Aggregate reports are informational; the rendered Markdown
always carries never-merge language and validation fails without it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from worktrees_hives import findings as _findings
from worktrees_hives.errors import AggregateValidationError, FindingsValidationError
from worktrees_hives.findings import FindingsReport, FindingType, load_findings_pair

# Aggregate report document schema (independent of the wh CLI envelope and of
# the per-run findings schema version).
AGGREGATE_SCHEMA_VERSION: int = 1

# Required Markdown section titles (ATX headings, case-insensitive match on text).
REQUIRED_AGGREGATE_MD_SECTIONS: tuple[str, ...] = (
    "Aggregate discoveries",
    "Summary",
    "Runs",
    "Failures",
    "Policy",
)

# Mandatory policy language; validate_aggregate_markdown fails without it.
NEVER_MERGE_LINE: str = (
    "This aggregate report is informational only. **Never merge** and never "
    "auto-merge based on it; pull requests always require human review."
)


class UnitOutcome(StrEnum):
    """Aggregate-level disposition of one hypothesis run's report pair."""

    REPORTED = "reported"
    MISSING_REPORT = "missing_report"
    INVALID_REPORT = "invalid_report"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class AggregateUnit:
    """One hypothesis run as it appears in the aggregate report."""

    hypothesis_id: str
    findings_json: str
    findings_md: str
    outcome: UnitOutcome
    report: FindingsReport | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.hypothesis_id.strip():
            raise AggregateValidationError("hypothesis_id is required non-empty string")
        if not self.findings_json.strip() or not self.findings_md.strip():
            raise AggregateValidationError(
                "findings_json and findings_md paths are required non-empty strings"
            )
        if self.outcome is UnitOutcome.REPORTED and self.report is None:
            raise AggregateValidationError("reported unit requires a parsed report")
        if (
            self.outcome in (UnitOutcome.MISSING_REPORT, UnitOutcome.INVALID_REPORT)
            and self.report is not None
        ):
            raise AggregateValidationError(f"{self.outcome} unit must not carry a report")
        if self.report is not None and self.report.hypothesis_id != self.hypothesis_id:
            raise AggregateValidationError(
                f"unit.report hypothesis_id {self.report.hypothesis_id!r} does not match "
                f"unit hypothesis_id {self.hypothesis_id!r}"
            )

    @property
    def is_failure(self) -> bool:
        """Silence = fail: every unit without a usable report is a failure."""
        return self.outcome is not UnitOutcome.REPORTED

    def finding_count(self, kind: FindingType) -> int:
        """Count findings of one kind in this unit's report (0 without a report)."""
        if self.report is None:
            return 0
        return sum(1 for f in self.report.findings if f.type == kind)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-compatible dict."""
        out: dict[str, Any] = {
            "hypothesis_id": self.hypothesis_id,
            "findings_json": self.findings_json,
            "findings_md": self.findings_md,
            "outcome": str(self.outcome),
        }
        if self.report is not None:
            out["report"] = self.report.to_dict()
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AggregateUnit:
        """Parse one aggregate unit object; fail closed on bad types."""
        if not isinstance(raw, dict):
            raise AggregateValidationError(f"unit must be an object, got {type(raw).__name__}")
        hypothesis_id = _require_nonempty_str(raw, "hypothesis_id")
        findings_json = _require_nonempty_str(raw, "findings_json")
        findings_md = _require_nonempty_str(raw, "findings_md")
        outcome_raw = raw.get("outcome")
        if not isinstance(outcome_raw, str):
            raise AggregateValidationError("unit.outcome is required and must be a string")
        try:
            outcome = UnitOutcome(outcome_raw)
        except ValueError as exc:
            raise AggregateValidationError(
                f"unit.outcome must be one of {[e.value for e in UnitOutcome]}, got {outcome_raw!r}"
            ) from exc
        report_raw = raw.get("report")
        report: FindingsReport | None = None
        if report_raw is not None:
            if not isinstance(report_raw, dict):
                raise AggregateValidationError("unit.report must be an object or omitted")
            try:
                report = FindingsReport.from_dict(report_raw)
            except FindingsValidationError as exc:
                raise AggregateValidationError(f"unit.report invalid: {exc.detail}") from exc
            # FindingsReport.from_dict strips identifiers. Restore the raw id
            # when it already matched the unit so padded ids still round-trip.
            raw_hid = report_raw.get("hypothesis_id")
            if raw_hid == hypothesis_id and report.hypothesis_id != hypothesis_id:
                report = replace(report, hypothesis_id=hypothesis_id)
        detail = raw.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise AggregateValidationError("unit.detail must be a string or omitted")
        return cls(
            hypothesis_id=hypothesis_id,
            findings_json=findings_json,
            findings_md=findings_md,
            outcome=outcome,
            report=report,
            detail=detail,
        )


def collect_unit(
    hypothesis_id: str,
    findings_json: str | Path,
    findings_md: str | Path,
    *,
    timed_out: bool = False,
    detail: str | None = None,
) -> AggregateUnit:
    """Classify one run's report pair into an :class:`AggregateUnit`.

    Missing or invalid pairs never raise here — they classify as failures so
    the aggregate always renders (silence = fail, not silence = crash). A
    ``timed_out`` unit is always a ``timeout`` failure, even when a valid
    (possibly partial) report pair was left behind.
    """
    if not hypothesis_id.strip():
        raise AggregateValidationError("hypothesis_id is required non-empty string")
    hypothesis_id = hypothesis_id.strip()
    jpath = Path(findings_json)
    mpath = Path(findings_md)

    outcome = UnitOutcome.REPORTED
    report: FindingsReport | None = None
    note = detail
    missing = [str(p) for p in (jpath, mpath) if not p.is_file()]
    if missing:
        outcome = UnitOutcome.MISSING_REPORT
        note = note or ("missing report file(s): " + ", ".join(missing))
    else:
        try:
            report = load_findings_pair(jpath, mpath)
        except FindingsValidationError as exc:
            outcome = UnitOutcome.INVALID_REPORT
            note = note or exc.detail
        else:
            if report.hypothesis_id != hypothesis_id:
                outcome = UnitOutcome.INVALID_REPORT
                note = note or (
                    f"report hypothesis_id {report.hypothesis_id!r} does not match "
                    f"unit hypothesis_id {hypothesis_id!r}"
                )
                report = None
    if timed_out:
        # A timed-out unit keeps a valid report for context (partial evidence),
        # but its aggregate outcome is always the timeout failure.
        outcome = UnitOutcome.TIMEOUT
        note = note or "worker timeout"
    return AggregateUnit(
        hypothesis_id=hypothesis_id,
        findings_json=str(jpath),
        findings_md=str(mpath),
        outcome=outcome,
        report=report,
        detail=note,
    )


@dataclass(frozen=True, slots=True)
class AggregateReport:
    """Versioned multi-run aggregate discoveries document."""

    units: tuple[AggregateUnit, ...]
    attribution: str | None = None
    schema_version: int = AGGREGATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Direct construction must uphold the same invariants from_dict enforces,
        # so to_json() can never emit a payload parse_aggregate_json() rejects.
        if self.schema_version != AGGREGATE_SCHEMA_VERSION:
            raise AggregateValidationError(
                f"unsupported schema_version {self.schema_version} "
                f"(expected {AGGREGATE_SCHEMA_VERSION})"
            )
        if self.attribution is not None and not self.attribution.strip():
            raise AggregateValidationError("attribution must be a non-empty string or None")

    def counts(self) -> dict[str, int]:
        """Derived summary counts (also embedded, informationally, in JSON)."""
        by_outcome = {outcome: 0 for outcome in UnitOutcome}
        for unit in self.units:
            by_outcome[unit.outcome] += 1
        return {
            "units": len(self.units),
            "reported": by_outcome[UnitOutcome.REPORTED],
            "missing_report": by_outcome[UnitOutcome.MISSING_REPORT],
            "invalid_report": by_outcome[UnitOutcome.INVALID_REPORT],
            "timeout": by_outcome[UnitOutcome.TIMEOUT],
            "failures": sum(1 for u in self.units if u.is_failure),
            "discoveries": sum(u.finding_count(FindingType.DISCOVERY) for u in self.units),
            "null_results": sum(u.finding_count(FindingType.NULL_RESULT) for u in self.units),
            "errors": sum(u.finding_count(FindingType.ERROR) for u in self.units),
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-compatible dict.

        ``counts`` is derived from ``units`` and included for consumers;
        :meth:`from_dict` re-derives it and never trusts the embedded copy.
        """
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "counts": self.counts(),
            "units": [u.to_dict() for u in self.units],
        }
        if self.attribution is not None:
            out["attribution"] = self.attribution
        return out

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize as JSON text."""
        try:
            return (
                json.dumps(self.to_dict(), indent=indent, sort_keys=False, allow_nan=False) + "\n"
            )
        except ValueError as exc:
            raise AggregateValidationError(f"cannot serialize report to JSON: {exc}") from exc

    def to_markdown(self) -> str:
        """Render the canonical aggregate Markdown: summary, table, failures, policy."""
        c = self.counts()
        lines: list[str] = [
            "# Aggregate discoveries",
            "",
            "## Summary",
            "",
            f"- Runs: {c['units']} total · {c['reported']} reported · "
            f"{c['failures']} failures (missing / invalid / timeout)",
            f"- Failure breakdown: {c['missing_report']} missing report · "
            f"{c['invalid_report']} invalid report · {c['timeout']} timeouts",
            f"- Findings across collected reports: {c['discoveries']} discoveries · "
            f"{c['null_results']} null results · {c['errors']} errors",
            "",
            "## Runs",
            "",
            "| Hypothesis | Unit outcome | Report status | Discoveries | "
            "Null results | Errors | Per-run report |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
        for unit in self.units:
            link = f"[findings.md]({_link_dest(unit.findings_md)})"
            outcome_cell = f"`{unit.outcome}`"
            if unit.is_failure:
                outcome_cell = f"**{outcome_cell} (failure)**"
            if unit.report is not None:
                status_cell = f"`{unit.report.status}`"
                d = unit.finding_count(FindingType.DISCOVERY)
                n = unit.finding_count(FindingType.NULL_RESULT)
                e = unit.finding_count(FindingType.ERROR)
                counts_cells = f"{d} | {n} | {e}"
            else:
                status_cell = "—"
                counts_cells = "— | — | —"
            lines.append(
                f"| `{_cell_code(unit.hypothesis_id)}` | {outcome_cell} | "
                f"{status_cell} | {counts_cells} | {link} |"
            )
        lines += ["", "## Failures", ""]
        failures = [u for u in self.units if u.is_failure]
        if failures:
            for unit in failures:
                why = unit.detail if unit.detail else "no detail recorded"
                lines.append(
                    f"- `{_cell_code(unit.hypothesis_id)}` — `{unit.outcome}`: {_cell_inline(why)}"
                )
        else:
            lines.append("_None._")
        lines += ["", "## Policy", "", NEVER_MERGE_LINE]
        if self.attribution is not None:
            lines += ["", "## Attribution", "", f"— {_cell_inline(self.attribution)}"]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AggregateReport:
        """Parse and validate an aggregate JSON object (fail closed)."""
        if not isinstance(raw, dict):
            raise AggregateValidationError(f"report must be an object, got {type(raw).__name__}")
        _SCHEMA_VERSION_SENTINEL = object()
        schema_version = raw.get("schema_version", _SCHEMA_VERSION_SENTINEL)
        if schema_version is _SCHEMA_VERSION_SENTINEL:
            raise AggregateValidationError("schema_version is required")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise AggregateValidationError("schema_version must be an int")
        if schema_version != AGGREGATE_SCHEMA_VERSION:
            raise AggregateValidationError(
                f"unsupported schema_version {schema_version} (expected {AGGREGATE_SCHEMA_VERSION})"
            )
        counts_raw = raw.get("counts")
        if counts_raw is not None and not isinstance(counts_raw, dict):
            raise AggregateValidationError("counts must be an object or omitted")
        units_raw = raw.get("units")
        if units_raw is None:
            raise AggregateValidationError("units is required (use [] if empty)")
        if not isinstance(units_raw, list):
            raise AggregateValidationError("units must be a list")
        units = tuple(AggregateUnit.from_dict(item) for item in units_raw)
        attribution = raw.get("attribution")
        if attribution is not None and (
            not isinstance(attribution, str) or not attribution.strip()
        ):
            raise AggregateValidationError("attribution must be a non-empty string or omitted")
        return cls(units=units, attribution=attribution, schema_version=schema_version)


def parse_aggregate_json(text: str) -> AggregateReport:
    """Decode JSON text into a validated :class:`AggregateReport`."""
    if not text or not text.strip():
        raise AggregateValidationError("aggregate JSON is empty")

    def _reject_non_finite(value: str) -> float:
        raise AggregateValidationError(f"aggregate JSON contains non-finite number: {value}")

    try:
        raw = json.loads(text, parse_constant=_reject_non_finite)
    except json.JSONDecodeError as exc:
        raise AggregateValidationError(f"aggregate JSON is not valid JSON: {exc}") from exc
    except AggregateValidationError:
        raise
    if not isinstance(raw, dict):
        raise AggregateValidationError("aggregate JSON root must be an object")
    return AggregateReport.from_dict(raw)


def validate_aggregate_markdown(text: str) -> None:
    """Fail closed if aggregate Markdown misses sections or never-merge language."""
    if not text or not text.strip():
        raise AggregateValidationError("aggregate Markdown is empty")
    headings = _findings._extract_headings_outside_code_blocks(text)
    missing = [
        s for s in REQUIRED_AGGREGATE_MD_SECTIONS if _findings._normalize_heading(s) not in headings
    ]
    if missing:
        raise AggregateValidationError(
            "aggregate Markdown missing required sections: " + ", ".join(missing)
        )
    normalized = " ".join(text.lower().split())
    if "never merge" not in normalized:
        raise AggregateValidationError("aggregate Markdown missing never-merge language")


def write_aggregate_pair(
    report: AggregateReport,
    json_path: str | Path,
    markdown_path: str | Path,
) -> None:
    """Write validated JSON + rendered Markdown for an aggregate report.

    Both payloads are fully prepared and validated **before** any file is
    written, so a validation failure never leaves a JSON-only half pair on disk.
    """
    jpath = Path(json_path)
    mpath = Path(markdown_path)
    json_text = report.to_json()
    md = report.to_markdown()
    validate_aggregate_markdown(md)
    jpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text(json_text, encoding="utf-8")
    mpath.write_text(md, encoding="utf-8")


def _require_nonempty_str(raw: dict[str, Any], key: str) -> str:
    """Require a non-blank string but return it unmodified.

    Parsing must not mutate identifiers or paths (a run directory may
    legitimately end in whitespace), so JSON round-trips stay byte-identical.
    """
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise AggregateValidationError(f"unit.{key} is required non-empty string")
    return val


def _cell_inline(text: str) -> str:
    """Escape free text for a Markdown table cell / list item (incl. pipes)."""
    return _findings._escape_md_inline(text).replace("|", "\\|").replace("\n", " ")


def _cell_code(text: str) -> str:
    """Escape text for an inline code span inside a table cell."""
    return _findings._escape_md_code(text).replace("|", "\\|").replace("\n", " ")


def _link_dest(path: str) -> str:
    """Angle-bracket link destination; tolerates spaces in report paths.

    Percent-encodes ``|`` (splits GFM table cells before inline parsing),
    ``\\`` (CommonMark backslash-escapes apply inside ``<...>`` destinations),
    the bracket delimiters, and newlines.
    """
    escaped = (
        path.replace("\\", "%5C")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace("|", "%7C")
        .replace("\n", "%0A")
    )
    return "<" + escaped + ">"
