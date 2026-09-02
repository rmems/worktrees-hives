"""Tests for the worktrees-hives CLI surface.

Covers the orchestration commands wired on top of the policy modules. Network
and subprocess work is stubbed at the module boundary (``discover_all`` and
``fetch_pr_infos``) so these tests exercise argument parsing, output rendering,
the v1 JSON envelope, and the exit-code contract.
"""

from __future__ import annotations

import json

import pytest

from worktrees_hives.cli import main
from worktrees_hives.discover import DiscoveryResult, Issue
from worktrees_hives.errors import PolicyError
from worktrees_hives.stacks import PRInfo
from worktrees_hives.stacks import PRState as StackPRState

OWNER = "acme"
REPO = "widget"


def _issue(number: int, *, is_pr: bool = False, repo: str = REPO, labels=None) -> Issue:
    return Issue(
        number=number,
        title=f"Item {number}",
        state="OPEN",
        labels=labels or [],
        milestone=None,
        url=f"https://github.com/{OWNER}/{repo}/issues/{number}",
        owner=OWNER,
        repo=repo,
        is_pr=is_pr,
        assignees=[],
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
    )


def _pr(number: int, head: str, base: str) -> PRInfo:
    return PRInfo(
        number=number,
        head_ref=head,
        base_ref=base,
        repo=REPO,
        owner=OWNER,
        state=StackPRState.OPEN,
    )


def _envelope(capsys) -> dict:
    """Parse the single JSON envelope written to stdout."""
    out = capsys.readouterr().out.strip()
    return json.loads(out)


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_human_output_groups_by_repo(self, monkeypatch, capsys):
        result = DiscoveryResult(
            issues=[_issue(2), _issue(1, labels=["bug"]), _issue(7, repo="other")],
            errors=[],
            owners_scanned=[OWNER],
        )
        monkeypatch.setattr("worktrees_hives.discover.discover_all", lambda **kw: result)
        assert main(["discover", "--owner", OWNER]) == 0
        out = capsys.readouterr().out
        assert f"{OWNER}/{REPO}:" in out
        assert f"{OWNER}/other:" in out
        # Sorted numerically within a repo, not by insertion order.
        assert out.index("#1") < out.index("#2")
        assert "[bug]" in out
        assert "3 item(s) across 1 owner(s)" in out

    def test_json_envelope_uses_shared_serializer(self, monkeypatch, capsys):
        result = DiscoveryResult(
            issues=[_issue(4, is_pr=True)],
            errors=[],
            owners_scanned=[OWNER],
        )
        monkeypatch.setattr("worktrees_hives.discover.discover_all", lambda **kw: result)
        assert main(["--json", "discover"]) == 0
        env = _envelope(capsys)
        assert env["ok"] is True
        assert env["schema_version"] == 1
        assert env["command"] == "discover"
        assert env["data"]["total_issues"] == 1
        assert env["data"]["issues"][0]["number"] == 4
        assert env["data"]["issues"][0]["is_pr"] is True

    def test_flags_are_forwarded(self, monkeypatch):
        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return DiscoveryResult(issues=[], errors=[], owners_scanned=[])

        monkeypatch.setattr("worktrees_hives.discover.discover_all", fake)
        main(
            [
                "discover",
                "--owner",
                "a",
                "--owner",
                "b",
                "--kind",
                "prs",
                "--allow-non-default-owners",
                "--no-check-auth",
            ]
        )
        assert seen["owners"] == ["a", "b"]
        assert seen["kind"] == "prs"
        assert seen["allow_non_default_owners"] is True
        # --no-check-auth inverts into check_auth=False.
        assert seen["check_auth"] is False

    def test_owner_defaults_to_none_for_env_fallback(self, monkeypatch):
        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return DiscoveryResult(issues=[], errors=[], owners_scanned=[])

        monkeypatch.setattr("worktrees_hives.discover.discover_all", fake)
        main(["discover"])
        assert seen["owners"] is None

    def test_truncation_and_errors_go_to_stderr(self, monkeypatch, capsys):
        result = DiscoveryResult(
            issues=[],
            errors=["acme/widget: rate limited"],
            owners_scanned=[OWNER],
            truncated=True,
        )
        monkeypatch.setattr("worktrees_hives.discover.discover_all", lambda **kw: result)
        assert main(["discover"]) == 0
        err = capsys.readouterr().err
        assert "truncated" in err
        assert "rate limited" in err

    def test_policy_error_exits_2(self, monkeypatch, capsys):
        def boom(**kwargs):
            raise PolicyError("OWNER_NOT_ALLOWED", "owner not allowed")

        monkeypatch.setattr("worktrees_hives.discover.discover_all", boom)
        assert main(["--json", "discover"]) == 2
        env = _envelope(capsys)
        assert env["ok"] is False
        assert env["error"]["code"] == "OWNER_NOT_ALLOWED"

    def test_value_error_exits_1(self, monkeypatch):
        def boom(**kwargs):
            raise ValueError("bad input")

        monkeypatch.setattr("worktrees_hives.discover.discover_all", boom)
        assert main(["discover"]) == 1


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class TestPlan:
    @staticmethod
    def _stub_repo(monkeypatch, prs: list[PRInfo]) -> None:
        monkeypatch.setattr(
            "worktrees_hives.stacks.StackDetector.resolve_default_branch",
            lambda self, repo_path=None: self.default_branch,
        )
        monkeypatch.setattr(
            "worktrees_hives.stacks.StackDetector.fetch_pr_infos",
            lambda self, repo_path=None: prs,
        )

    def test_requires_a_target(self, capsys):
        assert main(["plan"]) == 1
        assert "no targets" in capsys.readouterr().err

    @pytest.mark.parametrize("slug", ["notaslug", "a/b/c", "/repo", "owner/"])
    def test_rejects_malformed_slug(self, slug):
        assert main(["plan", "--repo", slug]) == 1

    def test_orders_stack_bottom_up(self, monkeypatch, capsys):
        # child (#2) sits on top of base (#1); base must come first.
        self._stub_repo(
            monkeypatch, [_pr(2, "feat/child", "feat/base"), _pr(1, "feat/base", "main")]
        )
        assert main(["plan", "--repo", f"{OWNER}/{REPO}", "--allow-unlisted"]) == 0
        out = capsys.readouterr().out
        assert out.index("#1") < out.index("#2")

    def test_json_annotates_stack_position(self, monkeypatch, capsys):
        self._stub_repo(
            monkeypatch, [_pr(1, "feat/base", "main"), _pr(2, "feat/child", "feat/base")]
        )
        assert main(["--json", "plan", "--repo", f"{OWNER}/{REPO}", "--allow-unlisted"]) == 0
        env = _envelope(capsys)
        assert env["command"] == "plan"
        ordered = env["data"]["ordered"]
        assert [e["number"] for e in ordered] == [1, 2]
        assert ordered[0]["stack_position"] == 0
        assert ordered[1]["stack_position"] == 1
        assert ordered[0]["stack_id"] == ordered[1]["stack_id"]

    def test_standalone_pr_has_no_stack(self, monkeypatch, capsys):
        self._stub_repo(monkeypatch, [_pr(9, "fix/typo", "main")])
        main(["--json", "plan", "--repo", f"{OWNER}/{REPO}", "--allow-unlisted"])
        entry = _envelope(capsys)["data"]["ordered"][0]
        assert entry["stack_id"] is None
        assert entry["number"] == 9

    def test_repo_flag_rejects_when_allowlist_is_empty(self, monkeypatch, capsys):
        """Explicit --repo is still owner-scoped: empty allowlist fails closed."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        self._stub_repo(monkeypatch, [_pr(1, "feat/base", "main")])
        assert main(["plan", "--repo", f"{OWNER}/{REPO}"]) == 2
        assert "not in allowlist" in capsys.readouterr().err

    def test_repo_flag_rejects_unlisted_owner(self, monkeypatch, capsys):
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "someone-else")
        self._stub_repo(monkeypatch, [_pr(1, "feat/base", "main")])
        assert main(["plan", "--repo", f"{OWNER}/{REPO}"]) == 2
        assert "not in allowlist" in capsys.readouterr().err

    def test_repo_targets_are_deduped(self, monkeypatch, capsys):
        self._stub_repo(monkeypatch, [_pr(1, "fix/a", "main")])
        main(
            [
                "--json",
                "plan",
                "--repo",
                f"{OWNER}/{REPO}",
                "--repo",
                f"{OWNER.upper()}/{REPO}",
                "--allow-unlisted",
            ]
        )
        assert len(_envelope(capsys)["data"]["ordered"]) == 1

    def test_owner_flag_rejects_unlisted_owner(self, monkeypatch):
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "someone-else")
        assert main(["plan", "--owner", OWNER]) == 2

    def test_owner_flag_rejects_when_allowlist_is_empty(self, monkeypatch, capsys):
        """Empty allowlist is deny-by-default for multi-owner discovery (AGENTS.md),
        not "explicit --owner is itself operator intent" — --owner needs
        --allow-unlisted regardless of whether the allowlist is unset or non-empty.
        """
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        called = False

        def fake_list_repos(owner):
            nonlocal called
            called = True
            return ([REPO], None, False)

        monkeypatch.setattr("worktrees_hives.discover.list_repos_for_owner", fake_list_repos)
        assert main(["plan", "--owner", OWNER]) == 2
        assert not called, "must reject before any repo discovery happens"

    def test_owner_flag_allows_unlisted_with_override(self, monkeypatch, capsys):
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "someone-else")
        monkeypatch.setattr(
            "worktrees_hives.discover.list_repos_for_owner",
            lambda owner: ([REPO], None, False),
        )
        self._stub_repo(monkeypatch, [_pr(1, "fix/a", "main")])
        assert main(["plan", "--owner", OWNER, "--allow-unlisted"]) == 0

    def test_owner_listing_error_is_a_recoverable_failure(self, monkeypatch, capsys):
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        monkeypatch.setattr(
            "worktrees_hives.discover.list_repos_for_owner",
            lambda owner: ([], "rate limited", False),
        )
        assert main(["plan", "--owner", OWNER, "--allow-unlisted"]) == 1
        assert "rate limited" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_help_omits_removed_babysit_command(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "discover" in out
        assert "plan" in out
        assert "babysit" not in out

    def test_watchlist_still_routes(self, tmp_path, capsys):
        state = tmp_path / "wl.json"
        assert main(["--state", str(state), "watchlist", "list"]) == 0
        assert "No jobs in watchlist" in capsys.readouterr().out

    def test_unknown_command_is_rejected(self):
        with pytest.raises(SystemExit):
            main(["nope"])

    def test_missing_command_is_rejected(self):
        with pytest.raises(SystemExit):
            main([])
