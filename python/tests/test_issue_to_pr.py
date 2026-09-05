"""Tests for the worktrees_hives.issue_to_pr module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.contract import ErrorData, ErrorResponse, SuccessResponse
from worktrees_hives.errors import WhProcessError
from worktrees_hives.issue_to_pr import (
    _NEVER_MERGE_MARKER,
    IssueToPr,
    IssueToPrConfig,
    IssueToPrError,
    IssueToPrResult,
    Step,
    _is_forbidden_merge_cmd,
)
from worktrees_hives.paths import default_worktree_base

TEST_COMMIT = "a" * 40


class TestIsForbiddenMergeCmd:
    """Structural never-merge guard must not scan metadata strings."""

    def test_allows_create_with_merge_like_metadata(self):
        cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            "owner/merge",
            "--head",
            "h",
            "--base",
            "main",
            "--title",
            "t",
            "--body",
            "b",
            "--label",
            "merge",
        ]
        assert not _is_forbidden_merge_cmd(cmd)

    def test_blocks_pr_merge(self):
        assert _is_forbidden_merge_cmd(["gh", "pr", "merge", "12"])

    def test_blocks_rest_merges_endpoint(self):
        assert _is_forbidden_merge_cmd(["gh", "api", "repos/o/r/pulls/1/merges"])
        assert _is_forbidden_merge_cmd(["gh", "api", "-X", "POST", "repos/o/r/pulls/1/merge"])

    def test_allows_api_get_on_repo_named_merge(self):
        assert not _is_forbidden_merge_cmd(["gh", "api", "repos/o/merge/pulls/1", "-X", "GET"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _success_response(
    command: str = "worktree.create",
    *,
    start_commit: object = TEST_COMMIT,
    head_commit: object = TEST_COMMIT,
) -> SuccessResponse:
    branch = "feature/issue-8"
    path = str(Path(default_worktree_base()) / "acme" / "example-repo" / "issue-8")
    return SuccessResponse(
        command=command,
        data={
            "path": path,
            "branch": branch,
            "branch_ref": f"refs/heads/{branch}",
            "start_commit": start_commit,
            "head_commit": head_commit,
            "worktree_registered": True,
        },
        schema_version=2,
    )


def _error_response(
    code: str = "E001",
    message: str = "something broke",
    *,
    data: dict[str, object] | None = None,
) -> ErrorResponse:
    return ErrorResponse(
        command="worktree.create",
        error=ErrorData(code=code, message=message),
        schema_version=1,
        data={} if data is None else data,
    )


def _make_config(**overrides) -> IssueToPrConfig:
    defaults = {
        "owner": "acme",
        "repo": "example-repo",
        "issue_number": 8,
        "base_branch": "main",
        "start_point": "origin/main",
        "pr_labels": ["python", "orchestrator"],
        "pr_milestone": "B",
    }
    defaults.update(overrides)
    return IssueToPrConfig(**defaults)


def _ok() -> MagicMock:
    return MagicMock(returncode=0, stdout="", stderr="")


def _gh_cmd_from_calls(mock_run) -> list:
    """Return the gh pr create argv from mock_run call list."""
    for call in mock_run.call_args_list:
        cmd = call[0][0]
        if isinstance(cmd, list) and "pr" in cmd and "create" in cmd:
            return cmd
    raise AssertionError(f"no gh pr create in {mock_run.call_args_list!r}")


def _happy_side_effect(
    gh_stdout: str = "https://github.com/acme/example-repo/pull/42\n",
) -> list[MagicMock]:
    """push + gh pr create."""
    return [
        _ok(),  # push
        MagicMock(returncode=0, stdout=gh_stdout, stderr=""),
    ]


# ---------------------------------------------------------------------------
# IssueToPrConfig
# ---------------------------------------------------------------------------


class TestIssueToPrConfig:
    """Tests for config dataclass."""

    def test_non_branch_defaults(self):
        cfg = IssueToPrConfig(
            owner="o",
            repo="r",
            issue_number=1,
            base_branch="trunk",
            start_point="origin/trunk",
        )
        assert cfg.base_branch == "trunk"
        assert cfg.start_point == "origin/trunk"
        assert cfg.remote == "origin"
        assert cfg.pr_labels == []
        assert cfg.pr_milestone is None
        assert cfg.auto_link is True
        assert cfg.gh_path is None

    def test_frozen(self):
        cfg = _make_config()
        with pytest.raises(AttributeError):
            cfg.owner = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# IssueToPr — happy path (fully mocked)
# ---------------------------------------------------------------------------


class TestIssueToPrHappyPath:
    """Tests for the full happy-path workflow with mocked subprocess."""

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_full_workflow(self, mock_run):
        """End-to-end: worktree create → push → gh pr create."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        # git push succeeds
        # gh pr create succeeds
        mock_run.side_effect = _happy_side_effect("https://github.com/acme/example-repo/pull/42\n")

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        result = orch.run()

        assert isinstance(result, IssueToPrResult)
        assert result.branch_name == "feature/issue-8"
        assert result.pr_number == 42
        assert "pull/42" in result.pr_url
        assert orch.step == Step.PR_OPENED
        assert result.start_commit == TEST_COMMIT

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_pr_body_contains_closes_link(self, mock_run):
        """PR body must include 'Closes #N' when auto_link=True."""
        cfg = _make_config(auto_link=True)
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = _happy_side_effect("https://github.com/o/r/pull/1\n")

        IssueToPr(config=cfg, wh_client=mock_wh).run()

        gh_cmd = _gh_cmd_from_calls(mock_run)
        body = gh_cmd[gh_cmd.index("--body") + 1]
        assert "Closes #8" in body

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_pr_body_never_merge_marker(self, mock_run):
        """PR body must always contain the never-auto-merge marker."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = _happy_side_effect("https://github.com/o/r/pull/1\n")

        IssueToPr(config=cfg, wh_client=mock_wh).run()

        gh_cmd = _gh_cmd_from_calls(mock_run)
        body = gh_cmd[gh_cmd.index("--body") + 1]
        assert _NEVER_MERGE_MARKER in body

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_no_auto_link(self, mock_run):
        """When auto_link=False, 'Closes #N' is absent from body."""
        cfg = _make_config(auto_link=False)
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = _happy_side_effect("https://github.com/o/r/pull/1\n")

        IssueToPr(config=cfg, wh_client=mock_wh).run()

        gh_cmd = _gh_cmd_from_calls(mock_run)
        body = gh_cmd[gh_cmd.index("--body") + 1]
        assert "Closes #8" not in body


# ---------------------------------------------------------------------------
# IssueToPr — step failures
# ---------------------------------------------------------------------------


class TestIssueToPrStepFailures:
    """Tests for failures at each lifecycle step."""

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_worktree_wh_error(self, mock_run):
        """WhError during worktree creation propagates as IssueToPrError."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.side_effect = WhProcessError(returncode=2, stderr="bad")

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match=r"worktree_created|init"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_worktree_error_response(self, mock_run):
        """An ErrorResponse from wh is treated as failure."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _error_response(data={"path": "/tmp/residual"})

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="wh returned error") as exc_info:
            orch.run()
        assert exc_info.value.data == {"path": "/tmp/residual"}

    @pytest.mark.parametrize(
        ("start_commit", "head_commit", "message"),
        [
            (None, TEST_COMMIT, "start_commit"),
            ("malformed", TEST_COMMIT, "start_commit"),
            (TEST_COMMIT, "b" * 40, "does not equal"),
        ],
    )
    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_rejects_invalid_commit_identity_response(
        self,
        mock_run,
        start_commit: object,
        head_commit: object,
        message: str,
    ) -> None:
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response(
            start_commit=start_commit, head_commit=head_commit
        )
        with pytest.raises(IssueToPrError, match=message):
            IssueToPr(config=_make_config(), wh_client=mock_wh).run()
        mock_run.assert_not_called()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_full_sha_response_must_match_request(self, mock_run) -> None:
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response(start_commit="b" * 40, head_commit="b" * 40)
        with pytest.raises(IssueToPrError, match="requested full object id"):
            IssueToPr(config=_make_config(start_point=TEST_COMMIT), wh_client=mock_wh).run()
        mock_run.assert_not_called()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_full_sha_response_exact_match_is_accepted(self, mock_run) -> None:
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()
        mock_run.side_effect = _happy_side_effect()
        result = IssueToPr(config=_make_config(start_point=TEST_COMMIT), wh_client=mock_wh).run()
        assert result.start_commit == TEST_COMMIT

    @pytest.mark.parametrize("commit", ["a" * 40, "b" * 64])
    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_uppercase_full_sha_is_normalized_before_dispatch(self, mock_run, commit: str) -> None:
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response(start_commit=commit, head_commit=commit)
        mock_run.side_effect = _happy_side_effect()
        result = IssueToPr(config=_make_config(start_point=commit.upper()), wh_client=mock_wh).run()
        assert result.start_commit == commit
        assert mock_wh.run.call_args.args[7] == commit

    def test_rejects_abbreviated_sha_request(self) -> None:
        with pytest.raises(IssueToPrError, match="full 40- or 64-character"):
            IssueToPr(config=_make_config(start_point="abc1234"), wh_client=MagicMock())

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_git_push_failure(self, mock_run):
        """Non-zero git push exit raises IssueToPrError."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="permission denied"),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="git push failed"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_git_push_timeout(self, mock_run):
        """git push timeout raises IssueToPrError."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="git", timeout=60),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="git push timed out"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_gh_pr_create_failure(self, mock_run):
        """gh pr create failure raises IssueToPrError."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            _ok(),
            MagicMock(returncode=1, stdout="", stderr="rate limited"),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="gh pr create failed"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_gh_pr_create_timeout(self, mock_run):
        """gh pr create timeout raises IssueToPrError."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            _ok(),
            subprocess.TimeoutExpired(cmd="gh", timeout=60),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="gh pr create timed out"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_worktree_wh_error_sets_failed(self, mock_run):
        """Step should be FAILED after WhError in worktree creation."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.side_effect = WhProcessError(returncode=2, stderr="bad")

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError):
            orch.run()
        assert orch.step == Step.FAILED

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_git_push_failure_sets_failed(self, mock_run):
        """Step should be FAILED after git push failure."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="permission denied"),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError):
            orch.run()
        assert orch.step == Step.FAILED

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_git_push_permission_error(self, mock_run):
        """PermissionError from git push should be caught."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        def _side(*a, **k):
            cmd = a[0] if a else k.get("args")
            # After ensure, push uses worktree_path -C
            if cmd and len(cmd) > 3 and cmd[3] == "push":
                raise PermissionError("Permission denied")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _side

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="git not executable"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_gh_pr_permission_error(self, mock_run):
        """PermissionError from gh pr create should be caught."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            _ok(),
            PermissionError("Permission denied"),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="gh not executable"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_gh_pr_empty_output(self, mock_run):
        """Empty stdout from gh pr create should raise clear error."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            _ok(),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        with pytest.raises(IssueToPrError, match="empty output"):
            orch.run()

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_gh_pr_multiline_output(self, mock_run):
        """gh output with warnings before PR URL should still parse correctly."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = [
            _ok(),
            MagicMock(
                returncode=0,
                stdout="Warning: something\nhttps://github.com/o/r/pull/99\n",
                stderr="",
            ),
        ]

        orch = IssueToPr(config=cfg, wh_client=mock_wh)
        result = orch.run()
        assert result.pr_number == 99


# ---------------------------------------------------------------------------
# _extract_pr_number
# ---------------------------------------------------------------------------


class TestExtractPrNumber:
    """Tests for PR URL parsing."""

    def test_standard_url(self):
        assert IssueToPr._extract_pr_number("https://github.com/acme/example-repo/pull/42") == 42

    def test_trailing_slash(self):
        assert IssueToPr._extract_pr_number("https://github.com/o/r/pull/7/") == 7

    def test_invalid_url_raises(self):
        with pytest.raises(IssueToPrError, match="Could not parse PR number"):
            IssueToPr._extract_pr_number("https://github.com/not-a-pr-url")

    def test_non_numeric_raises(self):
        with pytest.raises(IssueToPrError, match="Could not parse PR number"):
            IssueToPr._extract_pr_number("https://github.com/o/r/pull/abc")


# ---------------------------------------------------------------------------
# _branch_name and _worktree_path
# ---------------------------------------------------------------------------


class TestNamingHelpers:
    """Tests for branch name and worktree path generation."""

    def test_branch_name(self):
        cfg = _make_config(issue_number=42)
        orch = IssueToPr(config=cfg, wh_client=MagicMock())
        assert orch._branch_name() == "feature/issue-42"

    def test_worktree_path_default_base(self, monkeypatch):
        monkeypatch.delenv("WH_WORKTREE_BASE", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        cfg = _make_config(owner="acme", repo="example-repo", issue_number=8)
        orch = IssueToPr(config=cfg, wh_client=MagicMock())
        path = orch._worktree_path()
        parts = Path(path).parts
        assert parts[-3:] == ("acme", "example-repo", "issue-8")
        posix_path = path.replace("\\", "/")
        if sys.platform == "win32":
            assert "worktrees-hives" in parts
            assert "worktrees" in parts
        elif sys.platform == "darwin":
            assert "Library/Application Support/worktrees-hives/worktrees" in posix_path
        else:
            assert ".local/share/worktrees-hives/worktrees" in posix_path

    def test_worktree_path_custom_base(self, monkeypatch):
        monkeypatch.setenv("WH_WORKTREE_BASE", "/tmp/custom-wt")
        cfg = _make_config(owner="o", repo="r", issue_number=5)
        orch = IssueToPr(config=cfg, wh_client=MagicMock())
        expected = str(Path("/tmp/custom-wt") / "o" / "r" / "issue-5")
        assert orch._worktree_path() == expected

    def test_custom_remote_in_push(self, monkeypatch):
        """Custom remote name should be used in git push."""
        cfg = _make_config(remote="upstream")
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        import unittest.mock

        with unittest.mock.patch("worktrees_hives.issue_to_pr.subprocess.run") as mock_run:
            mock_run.side_effect = _happy_side_effect("https://github.com/o/r/pull/1\n")

            orch = IssueToPr(config=cfg, wh_client=mock_wh)
            orch.run()

            push_cmd = None
            for call in mock_run.call_args_list:
                cmd = call[0][0]
                if isinstance(cmd, list) and "push" in cmd:
                    push_cmd = cmd
                    break
            assert push_cmd is not None
            assert "upstream" in push_cmd
            assert "origin" not in push_cmd


# ---------------------------------------------------------------------------
# _gh_pr_create_cmd
# ---------------------------------------------------------------------------


class TestGhPrCreateCmd:
    """Tests for gh command construction."""

    def test_basic_cmd(self):
        cfg = _make_config(pr_labels=[], pr_milestone=None)
        orch = IssueToPr(config=cfg, wh_client=MagicMock())
        cmd = orch._gh_pr_create_cmd("feature/issue-8", "title", "body")
        assert cmd == [
            "gh",
            "pr",
            "create",
            "--repo",
            "acme/example-repo",
            "--head",
            "feature/issue-8",
            "--base",
            "main",
            "--title",
            "title",
            "--body",
            "body",
        ]

    def test_labels_and_milestone(self):
        cfg = _make_config(pr_labels=["bug", "p1"], pr_milestone="v1.0")
        orch = IssueToPr(config=cfg, wh_client=MagicMock())
        cmd = orch._gh_pr_create_cmd("b", "t", "bd")
        assert "--label" in cmd
        assert "--milestone" in cmd
        assert cmd.count("--label") == 2

    def test_custom_gh_path(self):
        cfg = _make_config(gh_path="/usr/local/bin/gh")
        orch = IssueToPr(config=cfg, wh_client=MagicMock())
        cmd = orch._gh_pr_create_cmd("b", "t", "bd")
        assert cmd[0] == "/usr/local/bin/gh"


# ---------------------------------------------------------------------------
# Never-merge safety invariant
# ---------------------------------------------------------------------------


class TestNeverMergeSafety:
    """Verify the never-auto-merge safety invariant is enforced."""

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_no_merge_subprocess_call(self, mock_run):
        """The orchestrator must never call 'git merge' or 'gh pr merge'."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = _happy_side_effect("https://github.com/o/r/pull/1\n")

        IssueToPr(config=cfg, wh_client=mock_wh).run()

        all_cmds = [c[0][0] for c in mock_run.call_args_list]
        for cmd in all_cmds:
            if not isinstance(cmd, list) or len(cmd) < 2:
                continue
            # Check that no command is literally 'git merge' or 'gh pr merge'
            assert not (cmd[0] == "git" and cmd[1] == "merge"), (
                f"Never-merge invariant violated: {' '.join(cmd)}"
            )
            assert not (cmd[0] == "gh" and "merge" in cmd), (
                f"Never-merge invariant violated: {' '.join(cmd)}"
            )

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_marker_in_every_pr_body(self, mock_run):
        """Every PR body must contain the never-merge marker."""
        cfg = _make_config()
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()

        mock_run.side_effect = _happy_side_effect("https://github.com/o/r/pull/1\n")

        IssueToPr(config=cfg, wh_client=mock_wh).run()

        cmd = _gh_cmd_from_calls(mock_run)
        body_idx = cmd.index("--body") + 1
        assert _NEVER_MERGE_MARKER in cmd[body_idx]


# ---------------------------------------------------------------------------
# CLI shape + remote/path/base rejection
# ---------------------------------------------------------------------------


class TestWhCreateCliShape:
    """worktree create must select v2 and match the published clap shape."""

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_wh_create_cli_shape(self, mock_run):
        cfg = _make_config(repo_path="/tmp/repo")
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()
        mock_run.side_effect = _happy_side_effect("https://github.com/acme/example-repo/pull/1\n")
        IssueToPr(config=cfg, wh_client=mock_wh).run()
        args = mock_wh.run.call_args[0]
        assert args[0:4] == ("worktree", "create", "--schema-version", "2")
        assert args[4:6] == ("--repo", "/tmp/repo")
        assert args[6:8] == ("--start-point", "origin/main")
        assert args[8] == "acme"
        assert args[9] == "example-repo"
        assert args[10] == "issue-8"
        assert args[11] == "feature/issue-8"
        # Old flag shape must not be used
        assert "--issue" not in args
        assert "--path" not in args

    @patch("worktrees_hives.issue_to_pr.subprocess.run")
    def test_exact_start_point_uses_wh_without_git_branch_subprocess(self, mock_run):
        cfg = _make_config(
            base_branch="release/1.0",
            start_point="origin/release/1.0",
            repo_path="/tmp/repo",
        )
        mock_wh = MagicMock()
        mock_wh.run.return_value = _success_response()
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # push
            MagicMock(
                returncode=0,
                stdout="https://github.com/acme/example-repo/pull/1\n",
                stderr="",
            ),
        ]
        IssueToPr(config=cfg, wh_client=mock_wh).run()
        wh_args = mock_wh.run.call_args[0]
        assert wh_args[6:8] == ("--start-point", "origin/release/1.0")
        assert all("branch" not in call[0][0] for call in mock_run.call_args_list)
        gh_cmd = _gh_cmd_from_calls(mock_run)
        assert gh_cmd[gh_cmd.index("--base") + 1] == "release/1.0"


class TestRemotePathBaseRejection:
    """Reject option-looking remotes/bases and path escapes."""

    def test_rejects_force_remote(self):
        with pytest.raises(IssueToPrError, match="remote"):
            IssueToPr(config=_make_config(remote="--force"), wh_client=MagicMock())

    def test_rejects_short_force_remote(self):
        with pytest.raises(IssueToPrError, match="remote"):
            IssueToPr(config=_make_config(remote="-f"), wh_client=MagicMock())

    def test_rejects_force_base_branch(self):
        with pytest.raises(IssueToPrError, match="base_branch"):
            IssueToPr(config=_make_config(base_branch="--force"), wh_client=MagicMock())

    def test_rejects_option_looking_owner(self):
        with pytest.raises(IssueToPrError, match="owner"):
            IssueToPr(config=_make_config(owner="--evil"), wh_client=MagicMock())

    def test_rejects_traversal_owner(self):
        with pytest.raises(IssueToPrError, match=r"owner|separator"):
            IssueToPr(config=_make_config(owner="../evil"), wh_client=MagicMock())

    def test_worktree_path_stays_under_base(self, monkeypatch):
        monkeypatch.setenv("WH_WORKTREE_BASE", "/tmp/wt-base")
        cfg = _make_config(owner="acme", repo="example-repo", issue_number=3)
        orch = IssueToPr(config=cfg, wh_client=MagicMock())
        path = orch._worktree_path()
        expected = str(Path("/tmp/wt-base") / "acme" / "example-repo" / "issue-3")
        assert path == expected

    def test_rejects_non_positive_issue_number(self):
        with pytest.raises(IssueToPrError, match="issue_number"):
            IssueToPr(config=_make_config(issue_number=0), wh_client=MagicMock())
        with pytest.raises(IssueToPrError, match="issue_number"):
            IssueToPr(config=_make_config(issue_number=-3), wh_client=MagicMock())
