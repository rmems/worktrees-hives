"""Tests for single-unit lab run (GH #80)."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from worktrees_hives.cli import main
from worktrees_hives.errors import PolicyError
from worktrees_hives.findings import (
    AgentRole,
    Finding,
    FindingsReport,
    FindingType,
    ReportStatus,
    write_findings_pair,
)
from worktrees_hives.lab_jobs import LabJob, LabJobStatus
from worktrees_hives.lab_run import (
    assert_command_allowed,
    findings_paths,
    run_lab_unit,
)

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "acme"
REPO = "example-repo"


def _job(path: str, *, hypothesis_id: str = "H-001") -> LabJob:
    now = "2026-08-12T00:00:00Z"
    return LabJob(
        job_id=f"lab-{hypothesis_id}",
        hypothesis_id=hypothesis_id,
        agent_id="grok",
        role=AgentRole.AGENT,
        owner=OWNER,
        repo=REPO,
        branch=f"lab/{hypothesis_id}",
        worktree_path=path,
        status=LabJobStatus.ALLOCATED,
        created_at=now,
        updated_at=now,
    )


def _report(worktree: str) -> FindingsReport:
    return FindingsReport(
        hypothesis_id="H-001",
        agent_id="grok",
        role=AgentRole.AGENT,
        worktree=worktree,
        status=ReportStatus.COMPLETED,
        findings=(
            Finding(type=FindingType.DISCOVERY, summary="ok"),
            Finding(type=FindingType.NULL_RESULT, summary="no merge"),
        ),
        artifacts=("findings.json", "findings.md"),
    )


class TestNeverMerge:
    def test_denies_gh_pr_merge(self) -> None:
        with pytest.raises(PolicyError, match=r"NEVER_MERGE|merge"):
            assert_command_allowed("gh pr merge 1 --squash")

    def test_denies_gh_pr_merge_with_global_repo(self) -> None:
        with pytest.raises(PolicyError, match=r"NEVER_MERGE|merge"):
            assert_command_allowed("gh -R acme/repo pr merge 1")

    def test_denies_gh_api_merge_slashless(self) -> None:
        with pytest.raises(PolicyError, match=r"NEVER_MERGE|merge"):
            assert_command_allowed("gh api repos/acme/repo/merges -f base=main -f head=feature")

    def test_denies_gh_api_pulls_merge(self) -> None:
        with pytest.raises(PolicyError, match=r"NEVER_MERGE|merge"):
            assert_command_allowed("gh api -X PUT repos/acme/repo/pulls/123/merge")

    def test_denies_force_refspec(self) -> None:
        with pytest.raises(PolicyError, match=r"FORCE_PUSH|force"):
            assert_command_allowed(["git", "push", "origin", "+HEAD:main"])

    def test_denies_git_c_alias_force(self) -> None:
        with pytest.raises(PolicyError, match=r"FORCE_PUSH|force|alias"):
            assert_command_allowed(
                "git -c alias.overwrite='push --force' overwrite origin HEAD:main"
            )

    def test_denies_git_c_alias_force_casefold(self) -> None:
        with pytest.raises(PolicyError, match=r"FORCE_PUSH|force|alias"):
            assert_command_allowed(
                "git -c Alias.overwrite='push --force' overwrite origin HEAD:main"
            )

    def test_denies_git_push_mirror(self) -> None:
        with pytest.raises(PolicyError, match=r"FORCE_PUSH|force|mirror"):
            assert_command_allowed(["git", "push", "--mirror", "origin"])

    def test_allows_git_push_then_docker_f(self) -> None:
        # Shell-operator boundary: -f after && is not git force.
        assert_command_allowed("git push origin main && docker build -f Dockerfile .")

    def test_denies_force_after_refspec(self) -> None:
        with pytest.raises(PolicyError, match=r"BARE_FORCE|force|FORCE_PUSH"):
            assert_command_allowed(["git", "push", "origin", "main", "--force"])

    def test_denies_short_f_after_remote(self) -> None:
        with pytest.raises(PolicyError, match=r"BARE_FORCE|force"):
            assert_command_allowed("git push origin -f main")

    def test_denies_path_qualified_git(self) -> None:
        with pytest.raises(PolicyError, match=r"BARE_FORCE|force"):
            assert_command_allowed(["/usr/bin/git", "push", "origin", "--force"])

    def test_denies_git_c_global_then_force(self) -> None:
        with pytest.raises(PolicyError, match=r"BARE_FORCE|force"):
            assert_command_allowed(["git", "-C", "/tmp/repo", "push", "origin", "-f"])

    def test_denies_shell_wrapper_force(self) -> None:
        with pytest.raises(PolicyError, match=r"BARE_FORCE|force"):
            assert_command_allowed("sh -c 'git push origin main --force'")

    @pytest.mark.parametrize(
        "command",
        [
            "bash -o posix -c 'git push origin main --force'",
            "bash --rcfile /dev/null -c 'git push origin main --force'",
        ],
    )
    def test_denies_shell_wrapper_force_after_option_values(self, command: str) -> None:
        with pytest.raises(PolicyError, match=r"FORCE_PUSH|force"):
            assert_command_allowed(command)

    def test_allows_shell_wrapper_without_force_after_option_value(self) -> None:
        assert_command_allowed("bash --rcfile /dev/null -c 'printf ready'")

    def test_denies_force_with_lease_in_free_command(self) -> None:
        # Free-form --command must not force-push; lease only via Rust git-safe.
        with pytest.raises(PolicyError, match=r"FORCE_PUSH|force"):
            assert_command_allowed(["git", "push", "--force-with-lease", "origin", "HEAD"])

    def test_allows_unrelated_docker_force(self) -> None:
        # Must not false-positive across unrelated push tools.
        assert_command_allowed("apt-get install -y git && docker push --force myimg")

    def test_empty_command_skips_allocate(self) -> None:
        mgr = MagicMock()
        with pytest.raises(Exception, match="empty"):
            run_lab_unit(
                mgr,
                owner=OWNER,
                repo=REPO,
                start_point="origin/main",
                hypothesis_id="H-001",
                agent_id="grok",
                role=AgentRole.AGENT,
                command="   ",
            )
        mgr.allocate.assert_not_called()

    def test_cli_has_no_merge_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["merge"])

    def test_denied_command_skips_allocate(self, tmp_path: Path) -> None:
        mgr = MagicMock()
        with pytest.raises(PolicyError):
            run_lab_unit(
                mgr,
                owner=OWNER,
                repo=REPO,
                start_point="origin/main",
                hypothesis_id="H-001",
                agent_id="grok",
                role=AgentRole.AGENT,
                command="gh pr merge 99",
            )
        mgr.allocate.assert_not_called()


class TestRunLabUnit:
    def test_happy_path_loads_findings(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        write_findings_pair(_report(str(wt)), *findings_paths(wt))
        mgr = MagicMock()
        mgr.allocate.return_value = _job(str(wt))
        result = run_lab_unit(
            mgr,
            owner=OWNER,
            repo=REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok",
            role=AgentRole.AGENT,
        )
        assert result.ok is True
        assert result.report is not None
        assert result.report.status is ReportStatus.COMPLETED
        mgr.allocate.assert_called_once()

    def test_missing_findings_fails(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        mgr = MagicMock()
        mgr.allocate.return_value = _job(str(wt))
        result = run_lab_unit(
            mgr,
            owner=OWNER,
            repo=REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok",
            role="agent",
        )
        assert result.ok is False
        assert result.error_code == "FINDINGS_INVALID"
        assert "missing" in (result.error_message or "").lower()

    def test_mismatched_hypothesis_fails(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        bad = _report(str(wt))
        # hypothesis_id is frozen; rebuild
        from dataclasses import replace

        bad = replace(bad, hypothesis_id="OTHER")
        write_findings_pair(bad, *findings_paths(wt))
        mgr = MagicMock()
        mgr.allocate.return_value = _job(str(wt))
        result = run_lab_unit(
            mgr,
            owner=OWNER,
            repo=REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok",
            role=AgentRole.AGENT,
        )
        assert result.ok is False
        assert "hypothesis_id" in (result.error_message or "")

    def test_command_failure(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        mgr = MagicMock()
        mgr.allocate.return_value = _job(str(wt))
        result = run_lab_unit(
            mgr,
            owner=OWNER,
            repo=REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok",
            role=AgentRole.AGENT,
            command=[sys.executable, "-c", "raise SystemExit(1)"],
            teardown_on_error=True,
        )
        assert result.ok is False
        assert result.error_code == "COMMAND_FAILED"
        assert result.command_exit != 0
        mgr.teardown.assert_called_once()

    def test_command_wipes_stale_findings(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        write_findings_pair(_report(str(wt)), *findings_paths(wt))
        mgr = MagicMock()
        mgr.allocate.return_value = _job(str(wt))
        # Successful no-op command does not rewrite findings → invalid (stale wiped).
        result = run_lab_unit(
            mgr,
            owner=OWNER,
            repo=REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok",
            role=AgentRole.AGENT,
            command=[sys.executable, "-c", "pass"],
        )
        assert result.ok is False
        assert result.error_code == "FINDINGS_INVALID"

    def test_command_fail_with_fresh_findings(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        mgr = MagicMock()
        mgr.allocate.return_value = _job(str(wt))
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            f"sys.path[:0] = {sys.path!r}\n"
            f"wt = Path({str(wt)!r})\n"
            "from worktrees_hives.findings import (\n"
            "  AgentRole, Finding, FindingsReport, FindingType, ReportStatus, write_findings_pair)\n"
            "from worktrees_hives.lab_run import findings_paths\n"
            "r = FindingsReport(\n"
            "  hypothesis_id='H-001', agent_id='grok', role=AgentRole.AGENT,\n"
            f"  worktree={str(wt)!r}, status=ReportStatus.FAILED,\n"
            "  findings=(Finding(type=FindingType.ERROR, summary='boom'),),\n"
            ")\n"
            "write_findings_pair(r, *findings_paths(wt))\n"
            "raise SystemExit(2)\n"
        )
        result = run_lab_unit(
            mgr,
            owner=OWNER,
            repo=REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok",
            role=AgentRole.AGENT,
            command=[sys.executable, "-c", script],
        )
        assert result.ok is False
        assert result.error_code == "COMMAND_FAILED"
        assert result.report is not None
        assert result.report.status is ReportStatus.FAILED


class TestCliLabRun:
    def test_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["lab", "run", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "hypothesis" in out.lower()
        assert "findings" in out.lower()
        assert "skip-findings" not in out

    def test_json_envelope_success(self, tmp_path: Path, monkeypatch, capsys) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        write_findings_pair(_report(str(wt)), *findings_paths(wt))
        job = _job(str(wt))

        forwarded: dict[str, object] = {}

        def fake_run(manager, **kwargs):
            from worktrees_hives.lab_run import LabRunResult

            forwarded.update(kwargs)
            return LabRunResult(
                job=job,
                report=_report(str(wt)),
                findings_json=str(wt / "findings.json"),
                findings_md=str(wt / "findings.md"),
                command_exit=None,
                ok=True,
            )

        monkeypatch.setattr("worktrees_hives.cli.run_lab_unit", fake_run)
        monkeypatch.setattr("worktrees_hives.cli.WhClient", MagicMock)
        monkeypatch.setattr(
            "worktrees_hives.cli.LabJobManager",
            lambda *a, **k: MagicMock(),
        )
        code = main(
            [
                "--json",
                "lab",
                "run",
                "--owner",
                OWNER,
                "--repo",
                REPO,
                "--start-point",
                "origin/main",
                "--hypothesis-id",
                "H-001",
                "--agent-id",
                "grok",
            ]
        )
        assert code == 0
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is True
        assert env["schema_version"] == 2
        assert env["command"] == "lab.run"
        assert env["data"]["job_id"] == job.job_id
        assert forwarded["start_point"] == "origin/main"

    def test_policy_denied_command_exit_2(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("worktrees_hives.cli.WhClient", MagicMock)
        monkeypatch.setattr(
            "worktrees_hives.cli.LabJobManager",
            lambda *a, **k: MagicMock(),
        )
        code = main(
            [
                "--json",
                "lab",
                "run",
                "--owner",
                OWNER,
                "--repo",
                REPO,
                "--start-point",
                "origin/main",
                "--hypothesis-id",
                "H-001",
                "--agent-id",
                "grok",
                "--command",
                "gh pr merge 1",
            ]
        )
        assert code == 2
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is False
        assert env["error"]["code"] in {"NEVER_MERGE", "FORCE_PUSH", "BARE_FORCE_PUSH"}

    def test_json_envelope_findings_failure(self, tmp_path: Path, monkeypatch, capsys) -> None:
        job = _job(str(tmp_path / "wt"))

        def fake_run(manager, **kwargs):
            from worktrees_hives.lab_run import LabRunResult

            return LabRunResult(
                job=job,
                report=None,
                findings_json=str(tmp_path / "findings.json"),
                findings_md=str(tmp_path / "findings.md"),
                command_exit=None,
                ok=False,
                error_code="FINDINGS_INVALID",
                error_message="findings JSON missing",
            )

        monkeypatch.setattr("worktrees_hives.cli.run_lab_unit", fake_run)
        monkeypatch.setattr("worktrees_hives.cli.WhClient", MagicMock)
        monkeypatch.setattr(
            "worktrees_hives.cli.LabJobManager",
            lambda *a, **k: MagicMock(),
        )
        code = main(
            [
                "--json",
                "lab",
                "run",
                "--owner",
                OWNER,
                "--repo",
                REPO,
                "--start-point",
                "origin/main",
                "--hypothesis-id",
                "H-001",
                "--agent-id",
                "grok",
            ]
        )
        assert code == 1
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is False
        assert env["error"]["code"] == "FINDINGS_INVALID"
