"""Thin tests for claim policy + WhClient worktree glue (no raw git)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from worktrees_hives.claim import (
    ClaimError,
    ClaimExistsError,
    ClaimManager,
    ClaimResult,
    IsolationError,
    _default_worktree_base,
    _validate_ref,
    _validate_segment,
)
from worktrees_hives.contract import ErrorData, ErrorResponse, SuccessResponse
from worktrees_hives.errors import PolicyError, WhBinaryNotFoundError, WhProcessError
from worktrees_hives.paths import default_worktree_base

TEST_OWNER = "acme"
TEST_REPO = "example-repo"
TEST_COMMIT = "a" * 40


def _ok_create(
    path: str = "/tmp/wt-base/acme/example-repo/gh-1",
    branch: str = "hive/gh-1",
    start_commit: object = TEST_COMMIT,
    head_commit: object = TEST_COMMIT,
) -> SuccessResponse:
    return SuccessResponse(
        command="worktree.create",
        data={
            "path": path,
            "branch": branch,
            "branch_ref": f"refs/heads/{branch}",
            "repo_root": "/tmp/repo",
            "start_commit": start_commit,
            "head_commit": head_commit,
            "worktree_registered": True,
        },
        schema_version=2,
    )


def _manager(
    mock_wh: MagicMock | None = None,
    *,
    worktree_base: str = "/tmp/wt-base",
    allowed_owners: frozenset[str] | None = frozenset({TEST_OWNER}),
    repo_root: str = "/tmp/repo",
) -> tuple[ClaimManager, MagicMock]:
    wh = mock_wh if mock_wh is not None else MagicMock()
    mgr = ClaimManager(
        wh_client=wh,
        worktree_base=worktree_base,
        repo_root=repo_root,
        allowed_owners=allowed_owners,
    )
    return mgr, wh


# ---------------------------------------------------------------------------
# Validation / path
# ---------------------------------------------------------------------------


class TestValidation:
    def test_segment_accepts_plain(self):
        _validate_segment("owner", "acme")
        _validate_segment("job_id", "gh-42")

    def test_segment_rejects_traversal(self):
        with pytest.raises(ClaimError, match="segment"):
            _validate_segment("owner", "../evil")
        with pytest.raises(ClaimError, match="segment"):
            _validate_segment("repo", "a/b")

    def test_segment_rejects_option_looking(self):
        with pytest.raises(ClaimError, match="segment"):
            _validate_segment("owner", "--force")

    def test_ref_rejects_force(self):
        with pytest.raises(ClaimError, match="ref"):
            _validate_ref("branch", "--force")

    def test_derive_path(self):
        mgr, _ = _manager()
        path = mgr.derive_path(TEST_OWNER, TEST_REPO, "gh-8")
        expected = os.path.abspath(os.path.join("/tmp/wt-base", TEST_OWNER, TEST_REPO, "gh-8"))
        assert path == expected

    def test_default_worktree_base_is_shared_helper(self):
        assert _default_worktree_base is default_worktree_base

    def test_default_worktree_base_darwin_branch(self, monkeypatch):
        """macOS branch of claim's default must run on every CI runner."""
        monkeypatch.delenv("WH_WORKTREE_BASE", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        home = "/claim-test-home"
        monkeypatch.setattr(os.path, "expanduser", lambda p: home if p == "~" else p)
        result = _default_worktree_base(platform="darwin")
        assert result == os.path.join(
            home,
            "Library",
            "Application Support",
            "worktrees-hives",
            "worktrees",
        )


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_denied_owner(self):
        mgr, wh = _manager(allowed_owners=frozenset({TEST_OWNER}))
        with pytest.raises(ClaimError, match="allowlist"):
            mgr.claim_issue("other-org", TEST_REPO, 1, base_ref="origin/main")
        wh.run.assert_not_called()

    def test_empty_allowlist_allows_any(self):
        mgr, wh = _manager(allowed_owners=frozenset())
        wh.run.return_value = _ok_create(
            path="/tmp/wt-base/other/example-repo/gh-2",
            branch="hive/gh-2",
        )
        result = mgr.claim_issue("other", TEST_REPO, 2, base_ref="origin/main")
        assert result.owner == "other"
        wh.run.assert_called()


# ---------------------------------------------------------------------------
# claim_issue / claim_pr
# ---------------------------------------------------------------------------


class TestClaimIssue:
    def test_happy_path_calls_wh_create(self):
        mgr, wh = _manager()
        path = "/tmp/wt-base/acme/example-repo/gh-8"
        wh.run.return_value = _ok_create(path=path, branch="hive/gh-8")
        result = mgr.claim_issue(TEST_OWNER, TEST_REPO, 8, base_ref="origin/release")
        assert result.branch == "hive/gh-8"
        assert result.job_id == "gh-8"
        assert result.issue_number == 8
        assert result.owns_branch is True
        assert result.worktree_path == path
        assert result.start_commit == TEST_COMMIT
        args = wh.run.call_args[0]
        assert args[0:4] == ("worktree", "create", "--schema-version", "2")
        assert args[4:6] == ("--repo", os.path.abspath("/tmp/repo"))
        assert args[6:8] == ("--start-point", "origin/release")
        assert args[8:12] == (TEST_OWNER, TEST_REPO, "gh-8", "hive/gh-8")

    @pytest.mark.parametrize("commit", ["a" * 40, "b" * 64])
    def test_uppercase_full_start_point_is_normalized_before_dispatch(self, commit: str):
        mgr, wh = _manager()
        wh.run.return_value = _ok_create(start_commit=commit, head_commit=commit)
        result = mgr.claim_issue(
            TEST_OWNER,
            TEST_REPO,
            1,
            base_ref=commit.upper(),
        )
        assert result.start_commit == commit
        assert wh.run.call_args.args[7] == commit

    def test_rejects_non_positive_issue(self):
        mgr, wh = _manager()
        with pytest.raises(ClaimError, match="issue_number"):
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 0, base_ref="origin/main")
        wh.run.assert_not_called()

    def test_exists_raises_without_wh(self, tmp_path: Path):
        base = tmp_path / "wt"
        existing = base / TEST_OWNER / TEST_REPO / "gh-1"
        existing.mkdir(parents=True)
        mgr, wh = _manager(worktree_base=str(base))
        with pytest.raises(ClaimExistsError):
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 1, base_ref="origin/main")
        wh.run.assert_not_called()

    def test_branch_mismatch_isolation(self):
        mgr, wh = _manager()
        wh.run.return_value = _ok_create(branch="wrong-branch")
        with pytest.raises(ClaimError, match="branch identity"):
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 1, base_ref="origin/main")


class TestClaimPr:
    def test_happy_path(self):
        mgr, wh = _manager()
        path = "/tmp/wt-base/acme/example-repo/pr-9"
        wh.run.return_value = _ok_create(path=path, branch="feature/pr-head")
        result = mgr.claim_pr(
            TEST_OWNER,
            TEST_REPO,
            9,
            head_branch="feature/pr-head",
            head_sha=TEST_COMMIT,
        )
        assert result.pr_number == 9
        assert result.job_id == "pr-9"
        assert result.owns_branch is False
        assert result.branch == "feature/pr-head"
        assert result.start_commit == TEST_COMMIT
        args = wh.run.call_args[0]
        assert args[6:8] == ("--start-point", TEST_COMMIT)
        assert args[-1] == "feature/pr-head"
        assert args[-2] == "pr-9"

    def test_rejects_bad_sha(self):
        mgr, wh = _manager()
        with pytest.raises(ClaimError, match="head_sha"):
            mgr.claim_pr(TEST_OWNER, TEST_REPO, 1, head_branch="feat", head_sha="not-hex!!")
        wh.run.assert_not_called()

    def test_rejects_abbreviated_sha(self):
        mgr, wh = _manager()
        with pytest.raises(ClaimError, match="head_sha"):
            mgr.claim_pr(TEST_OWNER, TEST_REPO, 1, head_branch="feat", head_sha="abc1234")
        wh.run.assert_not_called()

    @pytest.mark.parametrize("commit", ["a" * 40, "b" * 64])
    def test_uppercase_full_sha_is_normalized_before_dispatch(self, commit: str):
        mgr, wh = _manager()
        wh.run.return_value = _ok_create(
            path="/tmp/wt-base/acme/example-repo/pr-9",
            branch="feature/pr-head",
            start_commit=commit,
            head_commit=commit,
        )
        result = mgr.claim_pr(
            TEST_OWNER,
            TEST_REPO,
            9,
            head_branch="feature/pr-head",
            head_sha=commit.upper(),
        )
        assert result.start_commit == commit
        assert wh.run.call_args.args[7] == commit

    def test_full_sha_response_must_match_request(self):
        mgr, wh = _manager()
        wh.run.return_value = _ok_create(
            branch="feature/pr-head", start_commit="b" * 40, head_commit="b" * 40
        )
        with pytest.raises(ClaimError, match="requested full object id"):
            mgr.claim_pr(
                TEST_OWNER,
                TEST_REPO,
                9,
                head_branch="feature/pr-head",
                head_sha=TEST_COMMIT,
            )


class TestVerifiedCommitResponse:
    @pytest.mark.parametrize(
        ("start_commit", "head_commit", "message"),
        [
            (None, TEST_COMMIT, "start_commit"),
            ("not-a-sha", TEST_COMMIT, "start_commit"),
            (TEST_COMMIT, "b" * 40, "does not equal"),
        ],
    )
    def test_missing_malformed_or_mismatched_response(
        self, start_commit: object, head_commit: object, message: str
    ) -> None:
        mgr, wh = _manager()
        wh.run.return_value = _ok_create(start_commit=start_commit, head_commit=head_commit)
        with pytest.raises(ClaimError, match=message):
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 1, base_ref="origin/main")


# ---------------------------------------------------------------------------
# Errors from wh
# ---------------------------------------------------------------------------


class TestWhFailures:
    def test_missing_binary(self):
        mgr, wh = _manager()
        wh.run.side_effect = WhBinaryNotFoundError("no wh")
        with pytest.raises(ClaimError, match="wh binary not found"):
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 1, base_ref="origin/main")

    def test_process_error(self):
        mgr, wh = _manager()
        wh.run.side_effect = WhProcessError(returncode=1, stderr="boom")
        with pytest.raises(ClaimError, match="wh exited 1"):
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 1, base_ref="origin/main")

    def test_policy_error(self):
        mgr, wh = _manager()
        wh.run.side_effect = PolicyError("sandbox", "path escape", data={"path": "/tmp/residual"})
        with pytest.raises(ClaimError, match="policy") as exc_info:
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 1, base_ref="origin/main")
        assert exc_info.value.data == {"path": "/tmp/residual"}

    def test_error_response(self):
        mgr, wh = _manager()
        wh.run.return_value = ErrorResponse(
            command="worktree.create",
            error=ErrorData(code="E", message="nope"),
            schema_version=1,
            data={"path": "/tmp/residual"},
        )
        with pytest.raises(ClaimError, match="worktree create failed") as exc_info:
            mgr.claim_issue(TEST_OWNER, TEST_REPO, 1, base_ref="origin/main")
        assert exc_info.value.data == {"path": "/tmp/residual"}


# ---------------------------------------------------------------------------
# cleanup / verify
# ---------------------------------------------------------------------------


class TestCleanupAndVerify:
    def test_cleanup_calls_remove(self):
        mgr, wh = _manager()
        wh.run.return_value = SuccessResponse(
            command="worktree.remove",
            data={"removed": "/tmp/wt"},
            schema_version=1,
        )
        result = ClaimResult(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            job_id="gh-1",
            branch="hive/gh-1",
            worktree_path="/tmp/wt/acme/example-repo/gh-1",
        )
        mgr.cleanup(result, force=True)
        args = wh.run.call_args[0]
        assert args[0:2] == ("worktree", "remove")
        assert args[2] == result.worktree_path
        assert "--force" in args

    def test_cleanup_prune(self):
        mgr, wh = _manager()
        wh.run.return_value = SuccessResponse(command="worktree.remove", data={}, schema_version=1)
        result = ClaimResult(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            job_id="gh-1",
            branch="hive/gh-1",
            worktree_path="/tmp/p",
        )
        mgr.cleanup(result, prune=True)
        assert wh.run.call_count == 2
        prune_args = wh.run.call_args_list[1][0]
        assert prune_args[0:2] == ("worktree", "prune")
        assert "--repo" in prune_args

    def test_verify_isolation_missing_path(self, tmp_path: Path):
        mgr, _ = _manager()
        result = ClaimResult(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            job_id="gh-1",
            branch="hive/gh-1",
            worktree_path=str(tmp_path / "missing"),
        )
        with pytest.raises(IsolationError, match="missing"):
            mgr.verify_isolation(result)

    def test_verify_isolation_ok(self, tmp_path: Path):
        d = tmp_path / "wt"
        d.mkdir()
        mgr, _ = _manager()
        result = ClaimResult(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            job_id="gh-1",
            branch="hive/gh-1",
            worktree_path=str(d),
        )
        mgr.verify_isolation(result)
