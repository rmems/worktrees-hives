"""Unit tests for lab findings report schema (GH #82)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from worktrees_hives.errors import FindingsValidationError
from worktrees_hives.findings import (
    FINDINGS_SCHEMA_VERSION,
    REQUIRED_MD_SECTIONS,
    AgentRole,
    Finding,
    FindingsReport,
    FindingType,
    ReportStatus,
    empty_findings_markdown_template,
    load_findings_pair,
    parse_findings_json,
    validate_findings_markdown,
    write_findings_pair,
)

if TYPE_CHECKING:
    from pathlib import Path


def _valid_report(**overrides: object) -> FindingsReport:
    base = dict(
        hypothesis_id="H-001",
        agent_id="grok-lab",
        role=AgentRole.AGENT,
        worktree="/tmp/wt/H-001",
        status=ReportStatus.COMPLETED,
        findings=(
            Finding(
                type=FindingType.DISCOVERY,
                summary="Path sandbox holds",
                detail="No write outside worktree",
                evidence=("test_paths.py",),
            ),
            Finding(
                type=FindingType.NULL_RESULT,
                summary="No merge path exercised",
            ),
        ),
        artifacts=("findings.json", "findings.md"),
        budgets={"max_fix_commits": 3},
    )
    base.update(overrides)
    return FindingsReport(**base)  # type: ignore[arg-type]


class TestFindingsReportJson:
    def test_round_trip_dict(self) -> None:
        report = _valid_report()
        again = FindingsReport.from_dict(report.to_dict())
        assert again == report
        assert again.schema_version == FINDINGS_SCHEMA_VERSION

    def test_parse_json_text(self) -> None:
        text = _valid_report().to_json()
        parsed = parse_findings_json(text)
        assert parsed.hypothesis_id == "H-001"
        assert parsed.role is AgentRole.AGENT
        assert len(parsed.findings) == 2

    def test_rejects_empty_json(self) -> None:
        with pytest.raises(FindingsValidationError, match="empty"):
            parse_findings_json("   ")

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(FindingsValidationError, match="not valid JSON"):
            parse_findings_json("{not json")

    def test_rejects_nan_in_json(self) -> None:
        text = '{"hypothesis_id": "H-001", "budgets": {"value": NaN}}'
        with pytest.raises(FindingsValidationError, match="non-finite number"):
            parse_findings_json(text)

    def test_rejects_infinity_in_json(self) -> None:
        text = '{"hypothesis_id": "H-001", "budgets": {"value": Infinity}}'
        with pytest.raises(FindingsValidationError, match="non-finite number"):
            parse_findings_json(text)

    def test_rejects_negative_infinity_in_json(self) -> None:
        text = '{"hypothesis_id": "H-001", "budgets": {"value": -Infinity}}'
        with pytest.raises(FindingsValidationError, match="non-finite number"):
            parse_findings_json(text)

    def test_rejects_missing_required_fields(self) -> None:
        with pytest.raises(FindingsValidationError, match="hypothesis_id"):
            FindingsReport.from_dict(
                {
                    "schema_version": FINDINGS_SCHEMA_VERSION,
                    "agent_id": "a",
                    "role": "agent",
                    "worktree": "/w",
                    "status": "completed",
                    "findings": [],
                    "artifacts": [],
                }
            )

    def test_rejects_bad_role(self) -> None:
        raw = _valid_report().to_dict()
        raw["role"] = "human"
        with pytest.raises(FindingsValidationError, match="role"):
            FindingsReport.from_dict(raw)

    def test_rejects_bad_finding_type(self) -> None:
        raw = _valid_report().to_dict()
        raw["findings"] = [{"type": "opinion", "summary": "x", "evidence": []}]
        with pytest.raises(FindingsValidationError, match=r"finding\.type"):
            FindingsReport.from_dict(raw)

    def test_rejects_missing_findings_key(self) -> None:
        raw = _valid_report().to_dict()
        del raw["findings"]
        with pytest.raises(FindingsValidationError, match="findings is required"):
            FindingsReport.from_dict(raw)

    def test_rejects_missing_schema_version(self) -> None:
        raw = _valid_report().to_dict()
        del raw["schema_version"]
        with pytest.raises(FindingsValidationError, match="schema_version is required"):
            FindingsReport.from_dict(raw)

    def test_rejects_unsupported_schema_version(self) -> None:
        raw = _valid_report().to_dict()
        raw["schema_version"] = 99
        with pytest.raises(FindingsValidationError, match="schema_version"):
            FindingsReport.from_dict(raw)

    def test_budgets_optional(self) -> None:
        report = _valid_report(budgets=None)
        d = report.to_dict()
        assert "budgets" not in d
        again = FindingsReport.from_dict(d)
        assert again.budgets is None

    def test_budgets_immutable_after_construction(self) -> None:
        """Mutating the input dict or output dict should not affect the report."""
        input_budgets = {"max_fix_commits": 3, "tokens": 1000}
        raw_dict = _valid_report().to_dict()
        raw_dict["budgets"] = input_budgets

        report = FindingsReport.from_dict(raw_dict)
        original_json = report.to_json()

        # Mutate the input dict
        input_budgets["max_fix_commits"] = 999
        assert report.to_json() == original_json

        # Mutate a dict returned by to_dict()
        output_dict = report.to_dict()
        output_dict["budgets"]["tokens"] = 9999  # type: ignore[index]
        assert report.to_json() == original_json


class TestFindingsMarkdown:
    def test_template_has_all_sections(self) -> None:
        md = empty_findings_markdown_template()
        validate_findings_markdown(md)
        for title in REQUIRED_MD_SECTIONS:
            assert title.lower() in md.lower()

    def test_rendered_markdown_validates(self) -> None:
        md = _valid_report().to_markdown()
        validate_findings_markdown(md)
        assert "Path sandbox holds" in md
        assert "Attribution" in md

    def test_rejects_empty_markdown(self) -> None:
        with pytest.raises(FindingsValidationError, match="empty"):
            validate_findings_markdown("")

    def test_rejects_missing_section(self) -> None:
        md = "# Hypothesis\n\nx\n\n# Method\n\ny\n"
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    def test_ignores_headings_in_fenced_code_blocks(self) -> None:
        """Headings inside fenced code blocks should not count."""
        md = (
            "# Hypothesis\n\ntest\n\n"
            "# Method\n\ntest\n\n"
            "```\n# Discoveries\n# Null results\n```\n\n"
            "# Errors\n\ntest\n\n"
            "# Evidence\n\ntest\n\n"
            "# Attribution\n\ntest\n"
        )
        # Should fail because Discoveries and Null results are only in code blocks
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    @pytest.mark.parametrize("indent", [" ", "  ", "   "])
    def test_ignores_headings_in_indented_fenced_code_blocks(self, indent: str) -> None:
        """CommonMark permits up to three spaces before a fenced code block."""
        md = (
            "# Hypothesis\n\ntest\n\n"
            "# Method\n\ntest\n\n"
            f"{indent}```\n{indent}# Discoveries\n{indent}# Null results\n{indent}```\n\n"
            "# Errors\n\ntest\n\n"
            "# Evidence\n\ntest\n\n"
            "# Attribution\n\ntest\n"
        )
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    def test_closing_fence_cannot_be_shorter_than_opening_fence(self) -> None:
        """A shorter fence must not close a longer fenced code block."""
        md = (
            "# Hypothesis\n\ntest\n\n"
            "# Method\n\ntest\n\n"
            "````\nnot a section\n```\n# Discoveries\n````\n\n"
            "# Errors\n\ntest\n\n"
            "# Evidence\n\ntest\n\n"
            "# Attribution\n\ntest\n"
        )
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    def test_fence_with_info_string_cannot_close_code_block(self) -> None:
        """A closing fence may contain only spaces or tabs after its marker."""
        md = (
            "# Hypothesis\n\ntest\n\n"
            "```text\n   ```python\n# Discoveries\n```\n\n"
            "# Method\n\ntest\n\n"
            "# Errors\n\ntest\n\n"
            "# Evidence\n\ntest\n\n"
            "# Attribution\n\ntest\n"
        )
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    def test_heading_separator_cannot_cross_lines(self) -> None:
        """An ATX heading marker and its text must be on the same line."""
        md = " #\nDiscoveries\n" + empty_findings_markdown_template().replace("# Discoveries\n", "")
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    def test_ignores_headings_in_list_item_fenced_code_blocks(self) -> None:
        """Fenced code blocks directly nested in list items still hide headings."""
        md = (
            "# Hypothesis\n\ntest\n\n"
            "# Method\n\ntest\n\n"
            "- ```text\n  # Discoveries\n  # Null results\n  ```\n\n"
            "# Errors\n\ntest\n\n"
            "# Evidence\n\ntest\n\n"
            "# Attribution\n\ntest\n"
        )
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    def test_backtick_in_info_string_is_not_a_fence(self) -> None:
        """Backtick fence info strings cannot contain backticks."""
        md = empty_findings_markdown_template().replace(
            "# Discoveries\n", "   ```bad`info\n# Discoveries\n"
        )
        validate_findings_markdown(md)

    def test_trailing_hashes_need_whitespace_separator(self) -> None:
        """Unseparated trailing hashes remain part of an ATX heading's text."""
        md = empty_findings_markdown_template().replace("# Discoveries\n", "# Discoveries###\n")
        with pytest.raises(FindingsValidationError, match="missing required sections"):
            validate_findings_markdown(md)

    def test_headings_with_trailing_hashes(self) -> None:
        """ATX headings with optional closing hashes should be recognized."""
        md = (
            "# Hypothesis #\n\ntest\n\n"
            "# Method ##\n\ntest\n\n"
            "# Discoveries ###\n\ntest\n\n"
            "# Null results\n\ntest\n\n"
            "# Errors #####\n\ntest\n\n"
            "# Evidence\n\ntest\n\n"
            "# Attribution #\n\ntest\n"
        )
        # Should pass - all required sections are present with valid ATX closing
        validate_findings_markdown(md)


class TestFindingsPairIO:
    def test_write_and_load_pair(self, tmp_path: Path) -> None:
        report = _valid_report()
        j = tmp_path / "findings.json"
        m = tmp_path / "findings.md"
        write_findings_pair(report, j, m)
        loaded = load_findings_pair(j, m)
        assert loaded == report
        assert j.is_file() and m.is_file()
        # JSON is parseable plain text
        assert json.loads(j.read_text())["hypothesis_id"] == "H-001"

    def test_load_fails_if_json_missing(self, tmp_path: Path) -> None:
        m = tmp_path / "findings.md"
        m.write_text(empty_findings_markdown_template(), encoding="utf-8")
        with pytest.raises(FindingsValidationError, match="JSON missing"):
            load_findings_pair(tmp_path / "nope.json", m)

    def test_load_fails_if_markdown_missing(self, tmp_path: Path) -> None:
        j = tmp_path / "findings.json"
        j.write_text(_valid_report().to_json(), encoding="utf-8")
        with pytest.raises(FindingsValidationError, match="Markdown missing"):
            load_findings_pair(j, tmp_path / "nope.md")

    def test_load_fails_if_markdown_invalid(self, tmp_path: Path) -> None:
        j = tmp_path / "findings.json"
        m = tmp_path / "findings.md"
        j.write_text(_valid_report().to_json(), encoding="utf-8")
        m.write_text("# Only Hypothesis\n\nhi\n", encoding="utf-8")
        with pytest.raises(FindingsValidationError, match="Markdown"):
            load_findings_pair(j, m)

    def test_load_fails_on_invalid_utf8_in_json(self, tmp_path: Path) -> None:
        j = tmp_path / "findings.json"
        m = tmp_path / "findings.md"
        # Write invalid UTF-8 bytes
        j.write_bytes(b"\xff\xfe{invalid utf-8}")
        m.write_text(empty_findings_markdown_template(), encoding="utf-8")
        with pytest.raises(FindingsValidationError, match="not valid UTF-8"):
            load_findings_pair(j, m)

    def test_load_fails_on_invalid_utf8_in_markdown(self, tmp_path: Path) -> None:
        j = tmp_path / "findings.json"
        m = tmp_path / "findings.md"
        j.write_text(_valid_report().to_json(), encoding="utf-8")
        # Write invalid UTF-8 bytes
        m.write_bytes(b"\xff\xfe# Invalid UTF-8")
        with pytest.raises(FindingsValidationError, match="not valid UTF-8"):
            load_findings_pair(j, m)


class TestReviewFollowups:
    def test_markdown_escapes_bold_in_summary(self) -> None:
        report = _valid_report(
            findings=(
                Finding(
                    type=FindingType.DISCOVERY,
                    summary="uses **stars**",
                    detail="and _under_",
                ),
            )
        )
        md = report.to_markdown()
        assert r"\*\*stars\*\*" in md
        assert r"\_under\_" in md
        assert "uses **" not in md  # raw unescaped bold markers from summary must not appear

    def test_write_pair_does_not_leave_json_if_md_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _valid_report()
        j = tmp_path / "findings.json"
        m = tmp_path / "findings.md"

        def boom(_text: str) -> None:
            raise FindingsValidationError("forced md failure")

        monkeypatch.setattr(
            "worktrees_hives.findings.validate_findings_markdown",
            boom,
        )
        with pytest.raises(FindingsValidationError, match="forced md failure"):
            write_findings_pair(report, j, m)
        assert not j.exists()
        assert not m.exists()
