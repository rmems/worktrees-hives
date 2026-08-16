"""Unit tests for the aggregate discoveries report format (GH #16)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from worktrees_hives.aggregate import (
    AGGREGATE_SCHEMA_VERSION,
    NEVER_MERGE_LINE,
    REQUIRED_AGGREGATE_MD_SECTIONS,
    AggregateReport,
    AggregateUnit,
    UnitOutcome,
    collect_unit,
    parse_aggregate_json,
    validate_aggregate_markdown,
    write_aggregate_pair,
)
from worktrees_hives.errors import AggregateValidationError
from worktrees_hives.findings import (
    AgentRole,
    Finding,
    FindingsReport,
    FindingType,
    ReportStatus,
    write_findings_pair,
)

if TYPE_CHECKING:
    from pathlib import Path


def _valid_report(hypothesis_id: str = "H-001", **overrides: object) -> FindingsReport:
    base = dict(
        hypothesis_id=hypothesis_id,
        agent_id="fable-lab",
        role=AgentRole.AGENT,
        worktree=f"/tmp/wt/{hypothesis_id}",
        status=ReportStatus.COMPLETED,
        findings=(
            Finding(
                type=FindingType.DISCOVERY,
                summary="Cache locality dominates",
                evidence=("bench.txt",),
            ),
            Finding(type=FindingType.NULL_RESULT, summary="No effect from unrolling"),
            Finding(type=FindingType.ERROR, summary="One flaky harness crash"),
        ),
        artifacts=("findings.json", "findings.md"),
    )
    base.update(overrides)
    return FindingsReport(**base)  # type: ignore[arg-type]


def _write_pair(tmp_path: Path, hypothesis_id: str, **overrides: object) -> tuple[Path, Path]:
    report = _valid_report(hypothesis_id, **overrides)
    jpath = tmp_path / hypothesis_id / "findings.json"
    mpath = tmp_path / hypothesis_id / "findings.md"
    write_findings_pair(report, jpath, mpath)
    return jpath, mpath


class TestCollectUnit:
    def test_valid_pair_is_reported(self, tmp_path: Path) -> None:
        jpath, mpath = _write_pair(tmp_path, "H-001")
        unit = collect_unit("H-001", jpath, mpath)
        assert unit.outcome is UnitOutcome.REPORTED
        assert not unit.is_failure
        assert unit.report is not None
        assert unit.finding_count(FindingType.DISCOVERY) == 1

    def test_missing_json_is_missing_report_failure(self, tmp_path: Path) -> None:
        _, mpath = _write_pair(tmp_path, "H-001")
        unit = collect_unit("H-001", tmp_path / "H-001" / "nope.json", mpath)
        assert unit.outcome is UnitOutcome.MISSING_REPORT
        assert unit.is_failure
        assert unit.report is None
        assert unit.detail is not None and "missing" in unit.detail

    def test_missing_markdown_is_missing_report_failure(self, tmp_path: Path) -> None:
        jpath, _ = _write_pair(tmp_path, "H-001")
        unit = collect_unit("H-001", jpath, tmp_path / "H-001" / "nope.md")
        assert unit.outcome is UnitOutcome.MISSING_REPORT
        assert unit.is_failure

    def test_invalid_json_is_invalid_report_failure(self, tmp_path: Path) -> None:
        jpath, mpath = _write_pair(tmp_path, "H-001")
        jpath.write_text("{not json", encoding="utf-8")
        unit = collect_unit("H-001", jpath, mpath)
        assert unit.outcome is UnitOutcome.INVALID_REPORT
        assert unit.is_failure
        assert unit.report is None

    def test_hypothesis_mismatch_is_invalid_report(self, tmp_path: Path) -> None:
        jpath, mpath = _write_pair(tmp_path, "H-OTHER")
        unit = collect_unit("H-001", jpath, mpath)
        assert unit.outcome is UnitOutcome.INVALID_REPORT
        assert unit.report is None
        assert unit.detail is not None and "H-OTHER" in unit.detail

    def test_timeout_overrides_valid_pair_but_keeps_report(self, tmp_path: Path) -> None:
        jpath, mpath = _write_pair(tmp_path, "H-001")
        unit = collect_unit("H-001", jpath, mpath, timed_out=True)
        assert unit.outcome is UnitOutcome.TIMEOUT
        assert unit.is_failure
        assert unit.report is not None
        assert unit.detail == "worker timeout"

    def test_timeout_with_missing_pair(self, tmp_path: Path) -> None:
        unit = collect_unit("H-001", tmp_path / "a.json", tmp_path / "a.md", timed_out=True)
        assert unit.outcome is UnitOutcome.TIMEOUT
        assert unit.report is None

    def test_empty_hypothesis_id_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(AggregateValidationError):
            collect_unit("   ", tmp_path / "a.json", tmp_path / "a.md")


class TestAggregateUnitInvariants:
    def test_reported_requires_report(self) -> None:
        with pytest.raises(AggregateValidationError):
            AggregateUnit(
                hypothesis_id="H-001",
                findings_json="a.json",
                findings_md="a.md",
                outcome=UnitOutcome.REPORTED,
            )

    def test_missing_report_must_not_carry_report(self) -> None:
        with pytest.raises(AggregateValidationError):
            AggregateUnit(
                hypothesis_id="H-001",
                findings_json="a.json",
                findings_md="a.md",
                outcome=UnitOutcome.MISSING_REPORT,
                report=_valid_report(),
            )

    def test_report_hypothesis_must_match_unit(self) -> None:
        with pytest.raises(AggregateValidationError, match="does not match"):
            AggregateUnit(
                hypothesis_id="H-001",
                findings_json="a.json",
                findings_md="a.md",
                outcome=UnitOutcome.REPORTED,
                report=_valid_report("H-002"),
            )

    def test_direct_construction_rejects_bad_schema_version(self) -> None:
        with pytest.raises(AggregateValidationError, match="unsupported"):
            AggregateReport(units=(), schema_version=AGGREGATE_SCHEMA_VERSION + 1)

    def test_direct_construction_rejects_blank_attribution(self) -> None:
        with pytest.raises(AggregateValidationError, match="attribution"):
            AggregateReport(units=(), attribution="  ")

    def test_from_dict_preserves_field_whitespace(self) -> None:
        raw = {
            "hypothesis_id": "H-1",
            "findings_json": "runs/H-1/findings.json ",
            "findings_md": "runs/H-1/findings.md ",
            "outcome": "missing_report",
        }
        unit = AggregateUnit.from_dict(raw)
        assert unit.findings_json == "runs/H-1/findings.json "
        assert unit.to_dict() == raw


class TestAggregateMarkdown:
    def _mixed_report(self, tmp_path: Path, attribution: str | None = None) -> AggregateReport:
        jpath, mpath = _write_pair(tmp_path, "H-001")
        ok_unit = collect_unit("H-001", jpath, mpath)
        missing_unit = collect_unit(
            "H-002", tmp_path / "H-002" / "findings.json", tmp_path / "H-002" / "findings.md"
        )
        jpath3, mpath3 = _write_pair(tmp_path, "H-003")
        timeout_unit = collect_unit("H-003", jpath3, mpath3, timed_out=True)
        return AggregateReport(units=(ok_unit, missing_unit, timeout_unit), attribution=attribution)

    def test_renders_required_sections_and_validates(self, tmp_path: Path) -> None:
        md = self._mixed_report(tmp_path).to_markdown()
        validate_aggregate_markdown(md)
        for section in REQUIRED_AGGREGATE_MD_SECTIONS:
            assert section in md

    def test_each_row_links_to_per_run_findings_md(self, tmp_path: Path) -> None:
        report = self._mixed_report(tmp_path)
        md = report.to_markdown()
        for unit in report.units:
            assert f"[findings.md](<{unit.findings_md}>)" in md

    def test_missing_report_shows_as_failure(self, tmp_path: Path) -> None:
        md = self._mixed_report(tmp_path).to_markdown()
        assert "**`missing_report` (failure)**" in md
        assert "`H-002` — `missing_report`" in md

    def test_summary_counts(self, tmp_path: Path) -> None:
        report = self._mixed_report(tmp_path)
        counts = report.counts()
        assert counts["units"] == 3
        assert counts["reported"] == 1
        assert counts["failures"] == 2
        assert counts["missing_report"] == 1
        assert counts["timeout"] == 1
        # Reported + timed-out-with-report both contribute findings.
        assert counts["discoveries"] == 2
        assert counts["null_results"] == 2
        assert counts["errors"] == 2
        md = report.to_markdown()
        assert "3 total · 1 reported · 2 failures" in md

    def test_never_merge_language_present(self, tmp_path: Path) -> None:
        md = self._mixed_report(tmp_path).to_markdown()
        assert NEVER_MERGE_LINE in md

    def test_attribution_footer_only_when_agent_authored(self, tmp_path: Path) -> None:
        md_plain = self._mixed_report(tmp_path).to_markdown()
        assert "## Attribution" not in md_plain
        md_agent = self._mixed_report(tmp_path, attribution="Fable 5 (agent)").to_markdown()
        assert "## Attribution" in md_agent
        assert "Fable 5 (agent)" in md_agent

    def test_pipes_in_hypothesis_id_do_not_break_table(self, tmp_path: Path) -> None:
        unit = collect_unit("H|X", tmp_path / "x.json", tmp_path / "x.md")
        md = AggregateReport(units=(unit,)).to_markdown()
        assert "H\\|X" in md
        validate_aggregate_markdown(md)

    def test_pipe_in_findings_path_is_encoded_in_link(self, tmp_path: Path) -> None:
        unit = collect_unit("H-1", tmp_path / "a|b" / "f.json", tmp_path / "a|b" / "f.md")
        md = AggregateReport(units=(unit,)).to_markdown()
        assert f"[findings.md](<{tmp_path}/a%7Cb/f.md>)" in md

    def test_backslash_in_findings_path_is_encoded_in_link(self) -> None:
        unit = collect_unit("H-1", "runs\\a.json", "runs\\a.md")
        md = AggregateReport(units=(unit,)).to_markdown()
        assert "[findings.md](<runs%5Ca.md>)" in md


class TestValidateAggregateMarkdown:
    def test_empty_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="empty"):
            validate_aggregate_markdown("  \n ")

    def test_missing_section_fails(self, tmp_path: Path) -> None:
        md = AggregateReport(units=()).to_markdown()
        broken = md.replace("## Failures", "## Whatever")
        with pytest.raises(AggregateValidationError, match="Failures"):
            validate_aggregate_markdown(broken)

    def test_missing_never_merge_language_fails(self) -> None:
        md = AggregateReport(units=()).to_markdown()
        broken = md.replace(NEVER_MERGE_LINE, "Merging is a human decision.")
        with pytest.raises(AggregateValidationError, match="never-merge"):
            validate_aggregate_markdown(broken)

    def test_headings_inside_code_fences_do_not_count(self) -> None:
        md = AggregateReport(units=()).to_markdown()
        broken = md.replace("## Runs", "```\n## Runs\n```")
        with pytest.raises(AggregateValidationError, match="Runs"):
            validate_aggregate_markdown(broken)


class TestAggregateJson:
    def test_round_trip(self, tmp_path: Path) -> None:
        jpath, mpath = _write_pair(tmp_path, "H-001")
        report = AggregateReport(
            units=(
                collect_unit("H-001", jpath, mpath),
                collect_unit("H-002", tmp_path / "b.json", tmp_path / "b.md"),
            ),
            attribution="Fable 5 (agent)",
        )
        again = parse_aggregate_json(report.to_json())
        assert again == report

    def test_counts_embedded_but_rederived(self, tmp_path: Path) -> None:
        report = AggregateReport(
            units=(collect_unit("H-1", tmp_path / "a.json", tmp_path / "a.md"),)
        )
        raw = json.loads(report.to_json())
        assert raw["counts"]["failures"] == 1
        raw["counts"]["failures"] = 0  # tampered derived copy is ignored
        again = AggregateReport.from_dict(raw)
        assert again.counts()["failures"] == 1

    def test_empty_text_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="empty"):
            parse_aggregate_json("")

    def test_non_object_root_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="object"):
            parse_aggregate_json("[1, 2]")

    def test_missing_schema_version_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="schema_version"):
            AggregateReport.from_dict({"units": []})

    def test_wrong_schema_version_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="unsupported"):
            AggregateReport.from_dict({"schema_version": AGGREGATE_SCHEMA_VERSION + 1, "units": []})

    def test_bool_schema_version_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="int"):
            AggregateReport.from_dict({"schema_version": True, "units": []})

    def test_missing_units_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="units"):
            AggregateReport.from_dict({"schema_version": AGGREGATE_SCHEMA_VERSION})

    def test_bad_outcome_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="outcome"):
            AggregateUnit.from_dict(
                {
                    "hypothesis_id": "H-1",
                    "findings_json": "a.json",
                    "findings_md": "a.md",
                    "outcome": "exploded",
                }
            )

    def test_non_finite_number_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="non-finite"):
            parse_aggregate_json('{"schema_version": NaN, "units": []}')

    def test_blank_attribution_fails(self) -> None:
        with pytest.raises(AggregateValidationError, match="attribution"):
            AggregateReport.from_dict(
                {"schema_version": AGGREGATE_SCHEMA_VERSION, "units": [], "attribution": " "}
            )


class TestWriteAggregatePair:
    def test_writes_both_files(self, tmp_path: Path) -> None:
        report = AggregateReport(
            units=(collect_unit("H-1", tmp_path / "a.json", tmp_path / "a.md"),)
        )
        jout = tmp_path / "out" / "aggregate.json"
        mout = tmp_path / "out" / "aggregate.md"
        write_aggregate_pair(report, jout, mout)
        assert parse_aggregate_json(jout.read_text(encoding="utf-8")) == report
        validate_aggregate_markdown(mout.read_text(encoding="utf-8"))
