"""Tests for the babysit module.

All tests mock subprocess calls so no real GitHub API is hit.
Generic owners (acme) only — no product org hardcoding.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.babysit import (
    ALLOWED_OWNERS,
    BabysitCycle,
    BabysitResult,
    CheckRun,
    PRState,
    ReviewThread,
    _graphql_query,
    babysit_multiple,
    classify_pr,
    fetch_pr_checks,
    fetch_pr_status,
    fetch_review_threads,
    load_allowed_owners_from_env,
    resolve_thread,
)


@pytest.fixture(autouse=True)
def _default_allowed_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed allowlist: default test scope is acme unless a test overrides."""
    monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pr_data(
    state: str = "OPEN",
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
    review_decision: str | None = None,
    checks: list[dict] | None = None,
    head_ref: str = "feature/test",
    base_ref: str = "main",
    head_ref_oid: str | None = "abc1234567890",
    is_draft: bool = False,
) -> dict:
    return {
        "state": state,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "reviewDecision": review_decision,
        "statusCheckRollup": checks or [],
        "headRefName": head_ref,
        "baseRefName": base_ref,
        "headRefOid": head_ref_oid,
        "isDraft": is_draft,
    }


def _make_check(name: str, state: str, conclusion: str | None = None) -> dict:
    d = {"name": name, "state": state}
    if conclusion:
        d["conclusion"] = conclusion
    return d


def _make_thread(thread_id: str, resolved: bool = False, body: str = "Please fix this") -> dict:
    return {
        "isResolved": resolved,
        "id": thread_id,
        "comments": {
            "nodes": [
                {
                    "author": {"login": "reviewer-bot"},
                    "path": "src/main.rs",
                    "line": 42,
                    "body": body,
                    "databaseId": 12345,
                    "url": "https://github.com/test/repo/pull/1#discussion_r12345",
                }
            ]
        },
    }


def _mock_gh_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    mock = MagicMock()
    mock.stdout = stdout
    mock.returncode = returncode
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# CheckRun model
# ---------------------------------------------------------------------------


class TestCheckRun:
    def test_failure_conclusion(self) -> None:
        cr = CheckRun(name="ci", state="COMPLETED", conclusion="FAILURE")
        assert cr.is_failure
        assert not cr.is_success
        assert not cr.is_pending

    def test_success_conclusion(self) -> None:
        cr = CheckRun(name="ci", state="COMPLETED", conclusion="SUCCESS")
        assert cr.is_success
        assert not cr.is_failure
        assert not cr.is_pending

    def test_pending_state(self) -> None:
        cr = CheckRun(name="ci", state="IN_PROGRESS")
        assert cr.is_pending
        assert not cr.is_failure
        assert not cr.is_success

    def test_error_conclusion(self) -> None:
        cr = CheckRun(name="ci", state="COMPLETED", conclusion="ERROR")
        assert cr.is_failure

    def test_action_required(self) -> None:
        cr = CheckRun(name="ci", state="COMPLETED", conclusion="ACTION_REQUIRED")
        assert cr.is_failure

    def test_cancelled_is_failure(self) -> None:
        cr = CheckRun(name="ci", state="COMPLETED", conclusion="CANCELLED")
        assert cr.is_failure
        assert not cr.is_pending


# ---------------------------------------------------------------------------
# ReviewThread model
# ---------------------------------------------------------------------------


class TestReviewThread:
    def test_properties_from_first_comment(self) -> None:
        rt = ReviewThread(
            thread_id="T1",
            comments=[
                {
                    "author": {"login": "bot"},
                    "path": "lib.rs",
                    "line": 10,
                    "body": "Fix this",
                    "databaseId": 99,
                    "url": "https://example.com",
                }
            ],
        )
        assert rt.path == "lib.rs"
        assert rt.line == 10
        assert rt.body == "Fix this"
        assert rt.database_id == 99
        assert rt.author_login == "bot"

    def test_empty_comments(self) -> None:
        rt = ReviewThread(thread_id="T1")
        assert rt.first_comment is None
        assert rt.path is None
        assert rt.line is None
        assert rt.body == ""
        assert rt.database_id is None
        assert rt.author_login == ""

    def test_missing_author(self) -> None:
        rt = ReviewThread(
            thread_id="T1",
            comments=[{"body": "hello"}],
        )
        assert rt.author_login == ""


# ---------------------------------------------------------------------------
# BabysitResult model
# ---------------------------------------------------------------------------


class TestBabysitResult:
    def test_merge_ready_when_healthy(self) -> None:
        r = BabysitResult(pr_number=1, state=PRState.HEALTHY)
        assert r.is_merge_ready

    def test_not_merge_ready_when_failed(self) -> None:
        r = BabysitResult(pr_number=1, state=PRState.CI_FAILED)
        assert not r.is_merge_ready

    def test_summary_includes_blockers(self) -> None:
        r = BabysitResult(
            pr_number=5,
            state=PRState.CI_FAILED,
            fix_commits_used=2,
            checks_failed=1,
            residual_blockers=["CI failure: lint"],
        )
        s = r.summary()
        assert "PR #5" in s
        assert "ci_failed" in s
        assert "Fixes: 2" in s
        assert "CI failure: lint" in s


# ---------------------------------------------------------------------------
# classify_pr
# ---------------------------------------------------------------------------


class TestClassifyPR:
    def test_unknown_mergeable_is_pending(self) -> None:
        assert classify_pr(_make_pr_data(mergeable="UNKNOWN")) == PRState.PENDING_CI

    def test_startup_failure_is_failure(self) -> None:
        cr = CheckRun(name="job", state="COMPLETED", conclusion="STARTUP_FAILURE")
        assert cr.is_failure

    def test_merged(self) -> None:
        data = _make_pr_data(state="MERGED")
        assert classify_pr(data) == PRState.MERGED

    def test_closed(self) -> None:
        data = _make_pr_data(state="CLOSED")
        assert classify_pr(data) == PRState.CLOSED

    def test_conflicting(self) -> None:
        data = _make_pr_data(mergeable="CONFLICTING")
        assert classify_pr(data) == PRState.CONFLICTING

    def test_dirty_merge_state(self) -> None:
        data = _make_pr_data(merge_state="DIRTY")
        assert classify_pr(data) == PRState.CONFLICTING

    def test_behind_is_not_conflicting(self) -> None:
        data = _make_pr_data(merge_state="BEHIND")
        assert classify_pr(data) == PRState.BEHIND
        assert classify_pr(data) != PRState.CONFLICTING

    def test_ci_failed(self) -> None:
        checks = [_make_check("lint", "COMPLETED", "FAILURE")]
        data = _make_pr_data(checks=checks)
        assert classify_pr(data) == PRState.CI_FAILED

    def test_changes_requested(self) -> None:
        data = _make_pr_data(review_decision="CHANGES_REQUESTED")
        assert classify_pr(data) == PRState.CHANGES_REQUESTED

    def test_pending_ci(self) -> None:
        checks = [_make_check("test", "IN_PROGRESS")]
        data = _make_pr_data(checks=checks)
        assert classify_pr(data) == PRState.PENDING_CI

    def test_expected_requested_stale_are_pending(self) -> None:
        for state in ("EXPECTED", "REQUESTED", "STALE"):
            data = _make_pr_data(checks=[{"name": "required-ext", "state": state}])
            assert classify_pr(data) == PRState.PENDING_CI, state

    def test_healthy(self) -> None:
        checks = [_make_check("test", "COMPLETED", "SUCCESS")]
        data = _make_pr_data(checks=checks, review_decision="APPROVED")
        assert classify_pr(data) == PRState.HEALTHY

    def test_open_no_checks(self) -> None:
        data = _make_pr_data()
        assert classify_pr(data) == PRState.HEALTHY

    def test_draft(self) -> None:
        data = _make_pr_data(is_draft=True)
        assert classify_pr(data) == PRState.DRAFT

    def test_review_required(self) -> None:
        data = _make_pr_data(review_decision="REVIEW_REQUIRED")
        assert classify_pr(data) == PRState.REVIEW_REQUIRED

    def test_ci_failed_via_state_field(self) -> None:
        checks = [{"name": "ci", "state": "FAILURE"}]
        data = _make_pr_data(checks=checks)
        assert classify_pr(data) == PRState.CI_FAILED

    def test_follow_up_comment_actionable(self) -> None:
        rt = ReviewThread(
            thread_id="T1",
            comments=[
                {"body": "What does this do?", "databaseId": 1},
                {"body": "please update this to handle nulls", "databaseId": 2},
            ],
        )
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        assert cycle._is_actionable(rt)

    def test_question_only_not_actionable(self) -> None:
        rt = ReviewThread(
            thread_id="T1",
            comments=[{"body": "What does this do?", "databaseId": 1}],
        )
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        assert not cycle._is_actionable(rt)


# ---------------------------------------------------------------------------
# fetch_pr_status
# ---------------------------------------------------------------------------


class TestFetchPRStatus:
    @patch("worktrees_hives.babysit._run_gh")
    def test_returns_parsed_json(self, mock_gh: MagicMock) -> None:
        expected = _make_pr_data(state="OPEN")
        mock_gh.return_value = _mock_gh_result(json.dumps(expected))
        result = fetch_pr_status("acme", "repo", 1)
        assert result["state"] == "OPEN"
        mock_gh.assert_called_once()

    @patch("worktrees_hives.babysit._run_gh")
    def test_raises_on_invalid_json(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = _mock_gh_result("not json")
        with pytest.raises(ValueError, match="Failed to parse"):
            fetch_pr_status("acme", "repo", 1)


# ---------------------------------------------------------------------------
# fetch_pr_checks
# ---------------------------------------------------------------------------


class TestFetchPRChecks:
    @patch("worktrees_hives.babysit._run_gh")
    def test_returns_check_runs(self, mock_gh: MagicMock) -> None:
        checks = [
            {"name": "test", "state": "SUCCESS", "link": "https://ci.example.com"},
            {"name": "lint", "state": "FAILURE", "link": "https://ci.example.com/lint"},
        ]
        mock_gh.return_value = _mock_gh_result(json.dumps(checks))
        result = fetch_pr_checks("acme", "repo", 1)
        assert len(result) == 2
        assert result[0].name == "test"
        assert result[1].name == "lint"

    @patch("worktrees_hives.babysit._run_gh")
    def test_uses_check_false(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = _mock_gh_result("[]")
        fetch_pr_checks("acme", "repo", 1)
        call_args = mock_gh.call_args
        assert call_args[1].get("check") is False

    @patch("worktrees_hives.babysit._run_gh")
    def test_empty_stdout_nonzero_raises(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = _mock_gh_result("", returncode=1, stderr="HTTP 401")
        with pytest.raises(ValueError, match="Failed to fetch PR checks"):
            fetch_pr_checks("acme", "repo", 1)

    @patch("worktrees_hives.babysit._run_gh")
    def test_no_checks_message_returns_empty(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = _mock_gh_result(
            "", returncode=1, stderr="no checks reported on the pull request"
        )
        assert fetch_pr_checks("acme", "repo", 1) == []

    @patch("worktrees_hives.babysit._run_gh")
    def test_timed_out_maps_to_failure_conclusion(self, mock_gh: MagicMock) -> None:
        checks = [{"name": "slow", "state": "TIMED_OUT", "link": "https://ci.example.com"}]
        mock_gh.return_value = _mock_gh_result(json.dumps(checks))
        result = fetch_pr_checks("acme", "repo", 1)
        assert len(result) == 1
        assert result[0].conclusion == "TIMED_OUT"
        assert result[0].is_failure


# ---------------------------------------------------------------------------
# fetch_review_threads
# ---------------------------------------------------------------------------


class TestFetchReviewThreads:
    @patch("worktrees_hives.babysit._graphql_query")
    def test_unresolved_only(self, mock_gql: MagicMock) -> None:
        mock_gql.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                _make_thread("T1", resolved=False),
                                _make_thread("T2", resolved=True),
                            ],
                        }
                    }
                }
            }
        }
        threads = fetch_review_threads("acme", "repo", 1)
        assert len(threads) == 1
        assert threads[0].thread_id == "T1"

    @patch("worktrees_hives.babysit._graphql_query")
    def test_pagination(self, mock_gql: MagicMock) -> None:
        page1 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                            "nodes": [_make_thread("T1", resolved=False)],
                        }
                    }
                }
            }
        }
        page2 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [_make_thread("T2", resolved=False)],
                        }
                    }
                }
            }
        }
        mock_gql.side_effect = [page1, page2]
        threads = fetch_review_threads("acme", "repo", 1)
        assert len(threads) == 2
        assert threads[0].thread_id == "T1"
        assert threads[1].thread_id == "T2"
        assert mock_gql.call_count == 2

    @patch("worktrees_hives.babysit._graphql_query")
    def test_raises_on_bad_structure(self, mock_gql: MagicMock) -> None:
        mock_gql.return_value = {"data": {"unexpected": True}}
        with pytest.raises(ValueError, match="Unexpected GraphQL response"):
            fetch_review_threads("acme", "repo", 1)

    @patch("worktrees_hives.babysit._graphql_query")
    def test_query_requests_comments_first_50(self, mock_gql: MagicMock) -> None:
        mock_gql.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }
        }
        fetch_review_threads("acme", "repo", 1)
        query = mock_gql.call_args[0][0]
        assert "comments(first: 50)" in query


# ---------------------------------------------------------------------------
# Owner allowlist
# ---------------------------------------------------------------------------


class TestOwnerAllowlist:
    def test_default_allowed_owners_empty(self) -> None:
        assert frozenset() == ALLOWED_OWNERS

    def test_empty_allowlist_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        with pytest.raises(ValueError, match="Owner allowlist empty"):
            BabysitCycle(owner="any-org", repo="repo", pr_number=1)

    def test_env_allowlist_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme,example-org")
        with pytest.raises(ValueError, match="not in allowed owners"):
            BabysitCycle(owner="evil-org", repo="repo", pr_number=1)
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        assert cycle.owner == "acme"

    def test_constructor_allowed_owners(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        with pytest.raises(ValueError, match="not in allowed owners"):
            BabysitCycle(
                owner="evil-org",
                repo="repo",
                pr_number=1,
                allowed_owners=frozenset({"acme"}),
            )
        cycle = BabysitCycle(
            owner="acme",
            repo="repo",
            pr_number=1,
            allowed_owners=frozenset({"acme"}),
        )
        assert cycle.owner == "acme"

    def test_load_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme, example-org")
        assert load_allowed_owners_from_env() == frozenset({"acme", "example-org"})


class TestClassifyBlocked:
    def test_blocked_merge_state_is_blocked_not_threads(self) -> None:
        state = classify_pr(
            {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [],
                "reviewDecision": None,
                "isDraft": False,
            }
        )
        assert state == PRState.BLOCKED
        assert state != PRState.UNRESOLVED_THREADS


# ---------------------------------------------------------------------------
# BabysitCycle
# ---------------------------------------------------------------------------


class TestBabysitCycle:
    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_merged_pr_removed(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(state="MERGED")
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.MERGED
        assert "merged" in result.residual_blockers[0]

    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_conflicting_pr(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(mergeable="CONFLICTING")
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.CONFLICTING
        assert any("conflict" in b.lower() for b in result.residual_blockers)

    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_behind_processes_ci_and_residual(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(merge_state="BEHIND")
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.BEHIND
        assert any("behind" in b.lower() for b in result.residual_blockers)
        # Must not claim merge conflicts for BEHIND
        assert not any("merge conflicts" in b.lower() for b in result.residual_blockers)
        mock_checks.assert_called_once()
        mock_threads.assert_called_once()

    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_blocked_residual_separate_from_threads(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(merge_state="BLOCKED")
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.BLOCKED
        assert any("branch protection" in b.lower() for b in result.residual_blockers)
        assert not any(b.lower() == "unresolved review threads" for b in result.residual_blockers)

    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks")
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_ci_failures_dont_consume_fix_budget(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(checks=[])
        mock_checks.return_value = [
            CheckRun(name="test", state="COMPLETED", conclusion="FAILURE"),
            CheckRun(name="lint", state="COMPLETED", conclusion="SUCCESS"),
        ]
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.checks_failed == 1
        assert result.checks_passed == 1
        assert result.fix_commits_used == 0

    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit._run_gh")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_actionable_threads_resolved(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_gh: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_status.return_value = _make_pr_data()
        mock_gh.return_value = _mock_gh_result('"abc12345"')
        mock_threads.return_value = [
            ReviewThread(
                thread_id="T1",
                comments=[
                    {
                        "author": {"login": "bot"},
                        "path": "main.rs",
                        "line": 10,
                        "body": "Please fix this bug",
                        "databaseId": 100,
                        "url": "https://example.com",
                    }
                ],
            ),
        ]
        cycle = BabysitCycle(
            owner="acme",
            repo="repo",
            pr_number=1,
            fix_handler=lambda _t: "abc12345",
        )
        result = cycle.run()
        assert result.threads_resolved == 1
        assert result.threads_remaining == 0
        mock_reply.assert_called_once()
        mock_resolve.assert_called_once_with("T1", owner="acme", allowed_owners=frozenset({"acme"}))

    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit._run_gh")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_fix_handler_exception_is_treated_as_no_fix(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_gh: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """An unlisted exception from the fix handler must not abort the cycle."""
        mock_status.return_value = _make_pr_data()
        mock_threads.return_value = [
            ReviewThread(
                thread_id="T1",
                comments=[
                    {
                        "author": {"login": "bot"},
                        "path": "main.rs",
                        "line": 10,
                        "body": "Please fix this bug",
                        "databaseId": 100,
                        "url": "https://example.com",
                    }
                ],
            ),
        ]

        def broken_handler(_thread: ReviewThread) -> str:
            raise ConnectionError("network down")

        cycle = BabysitCycle(
            owner="acme",
            repo="repo",
            pr_number=1,
            fix_handler=broken_handler,
        )
        result = cycle.run()
        assert result.threads_resolved == 0
        assert result.threads_remaining == 1
        mock_resolve.assert_not_called()
        mock_reply.assert_called_once()
        assert any("real code fix" in b for b in result.residual_blockers)

    @patch("worktrees_hives.babysit.post_pr_comment")
    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit._run_gh")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_configured_fix_budget_enforced(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_gh: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
        mock_post: MagicMock,
    ) -> None:
        mock_status.return_value = _make_pr_data()
        mock_gh.return_value = _mock_gh_result('"abc12345"')
        mock_threads.return_value = [
            ReviewThread(
                thread_id=f"T{i}",
                comments=[
                    {
                        "author": {"login": "bot"},
                        "path": "main.rs",
                        "line": i,
                        "body": "Please fix this",
                        "databaseId": 100 + i,
                        "url": "https://example.com",
                    }
                ],
            )
            for i in range(4)
        ]
        # Distinct SHAs so the commit budget counts real unique pushes.
        shas = iter(["sha1", "sha2", "sha3", "sha4"])
        cycle = BabysitCycle(
            owner="acme",
            repo="repo",
            pr_number=1,
            max_fixes=3,
            fix_handler=lambda _t: next(shas),
        )
        result = cycle.run()
        assert result.threads_resolved == 3
        assert result.threads_remaining == 1
        assert cycle._fixes_used == 3
        assert result.state == PRState.UNRESOLVED_THREADS
        mock_post.assert_called()

    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_no_resolve_without_pushed_fix(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """FIX_AND_REPLY without a fix_handler must not claim addressed/resolve."""
        mock_status.return_value = _make_pr_data()
        mock_threads.return_value = [
            ReviewThread(
                thread_id="T1",
                comments=[
                    {
                        "author": {"login": "bot"},
                        "path": "main.rs",
                        "line": 10,
                        "body": "Please fix this bug",
                        "databaseId": 100,
                        "url": "https://example.com",
                    }
                ],
            ),
        ]
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.threads_resolved == 0
        assert result.threads_remaining == 1
        mock_resolve.assert_not_called()
        mock_reply.assert_called_once()
        assert any("real code fix" in b for b in result.residual_blockers)

    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit._run_gh")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks")
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_non_actionable_threads_skipped(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_gh: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_status.return_value = _make_pr_data()
        mock_gh.return_value = _mock_gh_result('"abc12345"')
        mock_threads.return_value = [
            ReviewThread(
                thread_id="T1",
                comments=[
                    {
                        "author": {"login": "bot"},
                        "path": "main.rs",
                        "line": 10,
                        "body": "LGTM, looks great!",
                        "databaseId": 100,
                        "url": "https://example.com",
                    }
                ],
            ),
        ]
        mock_checks.return_value = []
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.threads_resolved == 0
        assert result.threads_remaining == 1
        mock_reply.assert_not_called()
        mock_resolve.assert_not_called()

    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit._run_gh")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks")
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_healthy_when_all_green(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_gh: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_status.return_value = _make_pr_data(
            checks=[_make_check("test", "COMPLETED", "SUCCESS")]
        )
        mock_checks.return_value = [
            CheckRun(name="test", state="COMPLETED", conclusion="SUCCESS"),
        ]
        mock_threads.return_value = []
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.HEALTHY
        assert result.is_merge_ready

    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit._run_gh")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks")
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_pending_ci_state(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_gh: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_status.return_value = _make_pr_data(checks=[_make_check("test", "IN_PROGRESS")])
        mock_checks.return_value = [
            CheckRun(name="test", state="IN_PROGRESS"),
        ]
        mock_threads.return_value = []
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.PENDING_CI

    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_changes_requested_preserved(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(review_decision="CHANGES_REQUESTED")
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.CHANGES_REQUESTED
        assert not result.is_merge_ready

    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_review_required_preserved(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(review_decision="REVIEW_REQUIRED")
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.REVIEW_REQUIRED
        assert not result.is_merge_ready

    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_draft_pr_blocked(
        self, mock_status: MagicMock, mock_checks: MagicMock, mock_threads: MagicMock
    ) -> None:
        mock_status.return_value = _make_pr_data(is_draft=True)
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.DRAFT
        assert not result.is_merge_ready


# ---------------------------------------------------------------------------
# babysit_multiple
# ---------------------------------------------------------------------------


class TestBabysitMultiple:
    @patch("worktrees_hives.babysit.BabysitCycle.run")
    def test_returns_one_result_per_pr(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            BabysitResult(pr_number=1, state=PRState.HEALTHY),
            BabysitResult(pr_number=2, state=PRState.CI_FAILED),
        ]
        results = babysit_multiple("acme", "repo", [1, 2])
        assert len(results) == 2
        assert results[0].pr_number == 1
        assert results[1].pr_number == 2

    @patch("worktrees_hives.babysit.BabysitCycle")
    def test_passes_fix_handler_and_max_fixes(self, mock_cycle_cls: MagicMock) -> None:
        mock_cycle_cls.return_value.run.return_value = BabysitResult(
            pr_number=1, state=PRState.HEALTHY
        )
        handler = lambda _t: "deadbeef"  # noqa: E731
        babysit_multiple(
            "acme",
            "repo",
            [1],
            fix_handler=handler,
            max_fixes=2,
        )
        kwargs = mock_cycle_cls.call_args.kwargs
        assert kwargs["fix_handler"] is handler
        assert kwargs["max_fixes"] == 2

    @patch("worktrees_hives.babysit.BabysitCycle")
    def test_catches_per_pr_exceptions(self, mock_cycle_cls: MagicMock) -> None:
        good = MagicMock()
        good.run.return_value = BabysitResult(pr_number=1, state=PRState.HEALTHY)
        bad = MagicMock()
        bad.run.side_effect = RuntimeError("boom")
        mock_cycle_cls.side_effect = [good, bad]
        results = babysit_multiple("acme", "repo", [1, 2])
        assert len(results) == 2
        assert results[0].state == PRState.HEALTHY
        assert results[1].state == PRState.UNKNOWN
        assert any("boom" in b for b in results[1].residual_blockers)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_attribution(self) -> None:
        from worktrees_hives.babysit import DEFAULT_ATTRIBUTION

        assert "agent" in DEFAULT_ATTRIBUTION.lower()

    def test_allowed_owners_empty_by_default(self) -> None:
        assert frozenset() == ALLOWED_OWNERS


# ---------------------------------------------------------------------------
# GraphQL error handling / CI timeout / re-check after fix
# ---------------------------------------------------------------------------


class TestGraphQLErrors:
    @patch("worktrees_hives.babysit._run_gh")
    def test_rejects_top_level_errors(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"errors": [{"message": "rate limited"}]}),
            stderr="",
        )
        with pytest.raises(ValueError, match="GraphQL errors: rate limited"):
            _graphql_query("query { viewer { login } }", {})

    @patch("worktrees_hives.babysit._run_gh")
    def test_resolve_thread_surfaces_graphql_errors(self, mock_gh: MagicMock) -> None:
        mock_gh.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"errors": [{"message": "stale thread"}]}),
            stderr="",
        )
        with pytest.raises(ValueError, match="stale thread"):
            resolve_thread("PRRT_stale", owner="acme")


class TestFetchPRChecksTimeout:
    @patch("worktrees_hives.babysit._run_gh")
    def test_timeout_becomes_value_error(self, mock_gh: MagicMock) -> None:
        mock_gh.side_effect = subprocess.TimeoutExpired(cmd=["gh"], timeout=30)
        with pytest.raises(ValueError, match="Timed out fetching PR checks"):
            fetch_pr_checks("acme", "repo", 1)


class TestBabysitCycleTimeoutAndRecheck:
    @patch("worktrees_hives.babysit.fetch_review_threads", return_value=[])
    @patch("worktrees_hives.babysit.fetch_pr_checks")
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_ci_timeout_is_residual_blocker(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
    ) -> None:
        mock_status.return_value = _make_pr_data()
        mock_checks.side_effect = ValueError("Timed out fetching PR checks for acme/repo#1")
        cycle = BabysitCycle(owner="acme", repo="repo", pr_number=1)
        result = cycle.run()
        assert result.state == PRState.CI_FAILED
        assert any("CI check fetch failed" in b for b in result.residual_blockers)

    @patch("worktrees_hives.babysit.resolve_thread")
    @patch("worktrees_hives.babysit.reply_to_thread")
    @patch("worktrees_hives.babysit.fetch_review_threads")
    @patch("worktrees_hives.babysit.fetch_pr_checks")
    @patch("worktrees_hives.babysit.fetch_pr_status")
    def test_rechecks_ci_after_pushed_fix(
        self,
        mock_status: MagicMock,
        mock_checks: MagicMock,
        mock_threads: MagicMock,
        mock_reply: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        # Status: initial → head verify after push → re-check after fix.
        pending_status = _make_pr_data(
            checks=[{"name": "ci", "state": "PENDING", "status": "IN_PROGRESS"}],
            merge_state="UNSTABLE",
            head_ref_oid="deadbeef0123456789",
        )
        mock_status.side_effect = [
            _make_pr_data(head_ref_oid="deadbeef0123456789"),
            _make_pr_data(head_ref_oid="deadbeef0123456789"),  # head verify
            pending_status,  # post-fix recheck
        ]
        mock_checks.side_effect = [
            [],  # initial
            [
                CheckRun(name="ci", state="PENDING", conclusion=None),
            ],  # post-fix
        ]
        mock_threads.return_value = [
            ReviewThread(
                thread_id="T1",
                comments=[
                    {
                        "author": {"login": "bot"},
                        "path": "main.rs",
                        "line": 10,
                        "body": "Please fix this bug",
                        "databaseId": 100,
                        "url": "https://example.com",
                    }
                ],
            ),
        ]
        cycle = BabysitCycle(
            owner="acme",
            repo="repo",
            pr_number=1,
            fix_handler=lambda _t: "deadbeef",
        )
        result = cycle.run()
        assert result.threads_resolved == 1
        assert result.fix_commits_used >= 1
        assert mock_checks.call_count == 2
        assert mock_status.call_count == 3
        assert result.state == PRState.PENDING_CI
        assert result.checks_pending == 1
