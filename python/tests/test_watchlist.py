"""Tests for watchlist module."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from worktrees_hives.errors import PolicyError
from worktrees_hives.watchlist import (
    ALLOWED_OWNERS,
    CorruptStateError,
    JobState,
    JobStatus,
    Watchlist,
    _atomic_write_json,
    _default_state_path,
    load_allowed_owners_from_env,
)

# Explicit test allowlist (module deny-by-default when empty).
_TEST_OWNERS = frozenset({"acme", "example-org", "other-owner"})


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a temporary state file path (watchlist.json)."""
    return tmp_path / "watchlist.json"


@pytest.fixture
def watchlist(state_path: Path) -> Watchlist:
    """Return a fresh Watchlist instance with a test owner allowlist."""
    return Watchlist(state_path, allowed_owners=_TEST_OWNERS)


class TestAtomicWrite:
    """Tests for atomic JSON writing."""

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "state.json"
        _atomic_write_json(path, {"test": True})
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == {"test": True}

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _atomic_write_json(path, {"v": 1})
        _atomic_write_json(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}

    def test_no_temp_files_left(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _atomic_write_json(path, {"test": True})
        tmp_files = list(tmp_path.glob(".watched-*"))
        assert len(tmp_files) == 0


class TestDefaultStatePath:
    def test_honors_wh_watchlist_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom = tmp_path / "custom-watchlist.json"
        monkeypatch.setenv("WH_WATCHLIST_PATH", str(custom))
        monkeypatch.setenv("WH_STATE_PATH", str(tmp_path / "rust-watched.json"))
        assert _default_state_path() == custom

    def test_ignores_wh_state_path_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """WH_STATE_PATH is Rust watched.json — must not redirect the Python store."""
        monkeypatch.delenv("WH_WATCHLIST_PATH", raising=False)
        monkeypatch.setenv("WH_STATE_PATH", str(tmp_path / "rust-watched.json"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        path = _default_state_path()
        assert path.name == "watchlist.json"
        assert path != tmp_path / "rust-watched.json"

    def test_empty_xdg_data_home_falls_back_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty XDG_DATA_HOME must not resolve to CWD-relative worktrees-hives/."""
        monkeypatch.delenv("WH_WATCHLIST_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", "")
        path = _default_state_path()
        assert path.is_absolute()
        assert path.name == "watchlist.json"
        # Must not be Path('') / 'worktrees-hives' / 'watchlist.json' (cwd-relative).
        assert path != Path("worktrees-hives") / "watchlist.json"
        assert path.parent.name == "worktrees-hives"
        if sys.platform not in {"win32", "darwin"}:
            assert path.parent == Path.home() / ".local" / "share" / "worktrees-hives"

    def test_default_filename_is_watchlist_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("WH_WATCHLIST_PATH", raising=False)
        monkeypatch.delenv("WH_STATE_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        path = _default_state_path()
        assert path.name == "watchlist.json"
        assert "worktrees-hives" in path.parts


class TestWatchlistAdd:
    """Tests for Watchlist.add."""

    def test_add_job(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "repo", "feature/x")
        assert job.job_id == "j1"
        assert job.owner == "acme"
        assert job.repo == "repo"
        assert job.branch == "feature/x"
        assert job.status == JobStatus.PENDING
        assert job.fix_count == 0
        assert job.max_fixes is None

    def test_add_with_options(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "repo", "br", stack_id="s1", max_fixes=3)
        assert job.stack_id == "s1"
        assert job.max_fixes == 3

    def test_add_negative_max_fixes_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(ValueError, match="max_fixes"):
            watchlist.add("j1", "acme", "repo", "br", max_fixes=-1)

    def test_add_allows_large_max_fixes(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "repo", "br", max_fixes=999)
        assert job.max_fixes == 999

    def test_add_duplicate_raises(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        with pytest.raises(ValueError, match="already exists"):
            watchlist.add("j1", "acme", "repo", "br")

    def test_add_persists(self, state_path: Path) -> None:
        w1 = Watchlist(state_path, allowed_owners=_TEST_OWNERS)
        w1.add("j1", "acme", "repo", "br")
        w2 = Watchlist(state_path, allowed_owners=_TEST_OWNERS)
        job = w2.get("j1")
        assert job is not None
        assert job.owner == "acme"

    def test_corrupt_json_raises_and_quarantines(self, state_path: Path) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(CorruptStateError, match="quarantined"):
            Watchlist(state_path)
        # Original moved aside; no silent empty state
        assert not state_path.exists()
        quarantined = list(state_path.parent.glob("watchlist.json.corrupt.*"))
        assert len(quarantined) == 1


class TestMaxFixesOnLoad:
    def test_load_preserves_recorded_max_fixes(self, state_path: Path) -> None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "high": {
                            "job_id": "high",
                            "owner": "acme",
                            "repo": "r",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 99,
                            "fix_count": 0,
                            "residual_blockers": [],
                        },
                        "good": {
                            "job_id": "good",
                            "owner": "acme",
                            "repo": "r",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 2,
                            "fix_count": 0,
                            "residual_blockers": [],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        w = Watchlist(state_path, allowed_owners=frozenset({"acme"}))
        high = w.get("high")
        assert high is not None
        assert high.max_fixes == 99
        good = w.get("good")
        assert good is not None
        assert good.max_fixes == 2


class TestWatchlistRemove:
    """Tests for Watchlist.remove."""

    def test_remove_job(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        watchlist.remove("j1")
        assert watchlist.get("j1") is None

    def test_remove_nonexistent_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(KeyError, match="not found"):
            watchlist.remove("nope")


class TestWatchlistGet:
    """Tests for Watchlist.get."""

    def test_get_existing(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.get("j1")
        assert job is not None
        assert job.job_id == "j1"

    def test_get_missing(self, watchlist: Watchlist) -> None:
        assert watchlist.get("nope") is None


class TestWatchlistList:
    """Tests for Watchlist.list_jobs."""

    def test_list_all(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        watchlist.add("j3", "example-org", "r3", "br")
        assert len(watchlist.list_jobs()) == 3

    def test_filter_by_owner(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "example-org", "r2", "br")
        jobs = watchlist.list_jobs(owner="acme")
        assert len(jobs) == 1
        assert jobs[0].owner == "acme"

    def test_filter_by_repo(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        jobs = watchlist.list_jobs(repo="r1")
        assert len(jobs) == 1

    def test_filter_by_status(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        jobs = watchlist.list_jobs(status=JobStatus.PENDING)
        assert len(jobs) == 1
        assert jobs[0].job_id == "j2"


class TestWatchlistUpdate:
    """Tests for Watchlist.update_status."""

    def test_update_status(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        assert job.status == JobStatus.IN_PROGRESS

    def test_update_nonexistent_raises(self, watchlist: Watchlist) -> None:
        with pytest.raises(KeyError, match="not found"):
            watchlist.update_status("nope", JobStatus.COMPLETED)


class TestWatchlistFixCount:
    """Tests for Watchlist.increment_fix_count."""

    def test_increment(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.increment_fix_count("j1")
        assert job.fix_count == 1
        assert job.fix_budget_remaining is None

    def test_per_pr_budget_shared_across_jobs(self, watchlist: Watchlist) -> None:
        """Two job_ids on the same PR can share an explicit PR budget for a cycle."""
        watchlist.add("a", "acme", "repo", "br", max_fixes=3)
        watchlist.add("b", "acme", "repo", "br", max_fixes=3)
        watchlist.set_pr("a", 9, "https://example.com/pr/9")
        watchlist.set_pr("b", 9, "https://example.com/pr/9")
        watchlist.begin_babysit_cycle("c1")
        watchlist.increment_fix_count("a", cycle_id="c1")
        watchlist.increment_fix_count("b", cycle_id="c1")
        watchlist.increment_fix_count("a", cycle_id="c1")
        with pytest.raises(PolicyError, match=r"exhausted|PR"):
            watchlist.increment_fix_count("b", cycle_id="c1")

    def test_exhaust_budget_raises(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br", max_fixes=1)
        watchlist.increment_fix_count("j1")
        with pytest.raises(PolicyError, match="exhausted"):
            watchlist.increment_fix_count("j1")

    def test_large_in_memory_max_fixes_still_honors_in_memory_value(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mutated in-memory max_fixes controls budget for subsequent increments."""
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        w = Watchlist(state_path, allowed_owners=_TEST_OWNERS)
        w.add("j1", "acme", "repo", "br", max_fixes=4)
        job = w.get("j1")
        assert job is not None
        # Mutating max_fixes in-memory should be honored by subsequent increments.
        job.max_fixes = 1
        w.increment_fix_count("j1")
        with pytest.raises(PolicyError, match=r"exhausted"):
            w.increment_fix_count("j1")


class TestOwnerAllowlist:
    """Tests for WH_ALLOWED_OWNERS / allowed_owners on add."""

    def test_default_allowed_owners_empty(self) -> None:
        assert frozenset() == ALLOWED_OWNERS

    def test_empty_allowlist_denies_by_default(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        w = Watchlist(state_path)
        with pytest.raises(PolicyError, match="not in allowlist"):
            w.add("j1", "other-owner", "repo", "br")

    def test_env_allowlist_rejects_other_owner(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        w = Watchlist(state_path)
        with pytest.raises(PolicyError, match="not in allowlist"):
            w.add("j1", "other-owner", "repo", "br")
        w.add("j2", "acme", "repo", "br")
        assert w.get("j2") is not None

    def test_constructor_allowlist(self, state_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        w = Watchlist(state_path, allowed_owners=frozenset({"acme"}))
        with pytest.raises(PolicyError, match="not in allowlist"):
            w.add("j1", "evil", "repo", "br")

    def test_add_rejects_deferred_job_id_collision(self, state_path: Path) -> None:
        """Disallowed-owner durable id must not be clobbered by a later add."""
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "j1": {
                            "job_id": "j1",
                            "owner": "evil",
                            "repo": "repo",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 3,
                            "fix_count": 0,
                            "residual_blockers": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        w = Watchlist(state_path, allowed_owners=frozenset({"acme"}))
        with pytest.raises(ValueError, match="already exists"):
            w.add("j1", "acme", "other", "br")

    def test_env_allowlist_filters_loaded_jobs(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "good": {
                            "job_id": "good",
                            "owner": "acme",
                            "repo": "repo",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 3,
                            "fix_count": 0,
                            "residual_blockers": [],
                        },
                        "bad": {
                            "job_id": "bad",
                            "owner": "evil",
                            "repo": "repo",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 3,
                            "fix_count": 0,
                            "residual_blockers": [],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")

        w = Watchlist(state_path)

        assert w.get("good") is not None
        assert w.get("bad") is None
        assert [job.job_id for job in w.list_jobs()] == ["good"]
        result = w.check(record=False)
        assert [job.job_id for job in result["needs_pr"]] == ["good"]
        # Disallowed owner still on disk after a save of allowed jobs
        w.update_status("good", JobStatus.IN_PROGRESS)
        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert "bad" in reloaded["jobs"]
        assert reloaded["jobs"]["bad"]["owner"] == "evil"

    def test_constructor_allowlist_filters_loaded_jobs(
        self, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WH_ALLOWED_OWNERS", raising=False)
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "bad": {
                            "job_id": "bad",
                            "owner": "evil",
                            "repo": "repo",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 3,
                            "fix_count": 0,
                            "residual_blockers": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        w = Watchlist(state_path, allowed_owners=frozenset({"acme"}))

        assert w.list_jobs() == []
        assert all(not jobs for jobs in w.check().values())

    def test_load_allowed_owners_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme, example-org")
        assert load_allowed_owners_from_env() == frozenset({"acme", "example-org"})


class TestAdditiveV1Fields:
    """v1 schema: unknown job keys must not drop the job."""

    def test_additive_fields_preserved_on_roundtrip(self, state_path: Path) -> None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "j1": {
                            "job_id": "j1",
                            "owner": "acme",
                            "repo": "r",
                            "branch": "br",
                            "status": "pending",
                            "max_fixes": 3,
                            "fix_count": 0,
                            "residual_blockers": [],
                            "kind": "issue",
                            "worktree_path": "/tmp/wt",
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        w = Watchlist(state_path, allowed_owners=frozenset({"acme"}))
        job = w.get("j1")
        assert job is not None
        assert job.owner == "acme"
        # Touch state so extras are written back
        w.update_status("j1", JobStatus.IN_PROGRESS)
        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        entry = reloaded["jobs"]["j1"]
        assert entry["kind"] == "issue"
        assert entry["worktree_path"] == "/tmp/wt"
        assert entry["created_at"] == "2026-01-01T00:00:00Z"
        assert entry["status"] == "in_progress"


class TestWatchlistBlockers:
    """Tests for Watchlist.set_blockers."""

    def test_set_blockers(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.set_blockers("j1", ["ci failing", "merge conflict"])
        assert job.residual_blockers == ["ci failing", "merge conflict"]

    def test_clear_blockers(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        watchlist.set_blockers("j1", ["blocker"])
        job = watchlist.set_blockers("j1", [])
        assert job.residual_blockers == []


class TestWatchlistPR:
    """Tests for Watchlist.set_pr."""

    def test_set_pr(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "repo", "br")
        job = watchlist.set_pr("j1", 42, "https://github.com/acme/repo/pull/42")
        assert job.pr_number == 42
        assert job.pr_url == "https://github.com/acme/repo/pull/42"


class TestWatchlistCheck:
    """Tests for Watchlist.check."""

    def test_empty(self, watchlist: Watchlist) -> None:
        result = watchlist.check()
        assert all(len(v) == 0 for v in result.values())

    def test_categorize(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "acme", "r2", "br")
        watchlist.add("j3", "acme", "r3", "br")
        watchlist.add("j4", "acme", "r4", "br")
        watchlist.set_pr("j2", 1, "https://example.com/pr/1")
        # j2 is green with PR and budget — ready, NOT needs_fix
        watchlist.set_blockers("j3", ["failing test"])
        watchlist.update_status("j4", JobStatus.COMPLETED)

        result = watchlist.check()
        assert len(result["needs_pr"]) == 1
        assert result["needs_pr"][0].job_id == "j1"
        assert len(result["ready"]) == 1
        assert result["ready"][0].job_id == "j2"
        assert len(result["needs_fix"]) == 1
        assert result["needs_fix"][0].job_id == "j3"
        assert len(result["done"]) == 1
        assert result["done"][0].job_id == "j4"

    def test_exhausted_budget_with_blockers_is_blocked(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br", max_fixes=1)
        watchlist.set_pr("j1", 1, "https://example.com/pr/1")
        watchlist.increment_fix_count("j1")
        watchlist.set_blockers("j1", ["still failing"])
        result = watchlist.check()
        assert len(result["blocked"]) == 1
        assert result["blocked"][0].job_id == "j1"
        assert result["ready"] == []
        assert result["needs_fix"] == []

    def test_green_pr_with_budget_is_ready(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.set_pr("j1", 9, "https://example.com/pr/9")
        result = watchlist.check()
        assert result["needs_fix"] == []
        assert len(result["ready"]) == 1


class TestJobState:
    """Tests for JobState properties."""

    def test_full_repo(self) -> None:
        job = JobState("j1", "acme", "repo", "br")
        assert job.full_repo == "acme/repo"

    def test_is_actionable(self) -> None:
        job = JobState("j1", "acme", "repo", "br")
        assert job.is_actionable is True
        job.status = JobStatus.COMPLETED
        assert job.is_actionable is False

    def test_fix_budget(self) -> None:
        job = JobState("j1", "acme", "repo", "br", fix_count=2, max_fixes=3)
        assert job.fix_budget_remaining == 1
        job.fix_count = 3
        assert job.fix_budget_remaining == 0


class TestMultiOwner:
    """Tests for multi-owner repo support (generic owners)."""

    def test_acme_owner(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "widgets", "feature/x")
        assert job.full_repo == "acme/widgets"

    def test_example_org_owner(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j2", "example-org", "project", "main")
        assert job.full_repo == "example-org/project"

    def test_filter_across_owners(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.add("j2", "example-org", "r2", "br")
        watchlist.add("j3", "acme", "r3", "br")
        acme_jobs = watchlist.list_jobs(owner="acme")
        example_jobs = watchlist.list_jobs(owner="example-org")
        assert len(acme_jobs) == 2
        assert len(example_jobs) == 1


class TestCliPolicyExit:
    """CLI maps PolicyError to exit code 2."""

    def test_add_negative_max_fixes_returns_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        code = main(
            [
                "--state",
                str(tmp_path / "watchlist.json"),
                "watchlist",
                "add",
                "j1",
                "acme",
                "repo",
                "br",
                "--max-fixes",
                "-1",
            ]
        )
        assert code == 1

    def test_add_duplicate_returns_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        state = str(tmp_path / "watchlist.json")
        assert main(["--state", state, "watchlist", "add", "j1", "acme", "repo", "br"]) == 0
        assert main(["--state", state, "watchlist", "add", "j1", "acme", "repo", "br"]) == 1

    def test_remove_missing_returns_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        state = str(tmp_path / "watchlist.json")
        assert main(["--state", state, "watchlist", "remove", "missing"]) == 1

    def test_list_and_check_via_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        state = str(tmp_path / "watchlist.json")
        assert main(["--state", state, "watchlist", "add", "j1", "acme", "repo", "br"]) == 0
        assert main(["--state", state, "watchlist", "list", "--owner", "acme"]) == 0
        out = capsys.readouterr().out
        assert "j1" in out
        assert main(["--state", state, "watchlist", "check", "--owner", "acme"]) == 0
        out2 = capsys.readouterr().out
        assert "NEEDS_PR" in out2 or "j1" in out2

    def test_prog_is_worktrees_hives(self, capsys: pytest.CaptureFixture[str]) -> None:
        from worktrees_hives.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "worktrees-hives" in out


class TestCheckEdgeCases:
    def test_in_progress_not_needs_pr(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        result = watchlist.check(record=False)
        assert result["needs_pr"] == []
        assert len(result["in_progress"]) == 1

    def test_in_progress_with_blockers_defers_fixes(self, watchlist: Watchlist) -> None:
        """Active fix worker must not re-enter needs_fix even with residual blockers."""
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.set_pr("j1", 42, "https://example.com/pr/42")
        watchlist.set_blockers("j1", ["ci failing"])
        watchlist.update_status("j1", JobStatus.IN_PROGRESS)
        result = watchlist.check(record=False)
        assert result["needs_fix"] == []
        assert result["blocked"] == []
        assert len(result["in_progress"]) == 1
        assert result["in_progress"][0].job_id == "j1"

    def test_check_preserves_error(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.record_check("j1", error="boom")
        result = watchlist.check(record=True)
        job = watchlist.get("j1")
        assert job is not None
        assert job.error == "boom"
        assert job.last_check is not None
        assert any(result.values())

    def test_babysit_cycle_resets_fix_count(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br", max_fixes=3)
        watchlist.increment_fix_count("j1", cycle_id="c1")
        watchlist.increment_fix_count("j1", cycle_id="c1")
        assert watchlist.get("j1").fix_count == 2
        watchlist.begin_babysit_cycle("c2", "j1")
        assert watchlist.get("j1").fix_count == 0
        assert watchlist.get("j1").babysit_cycle == "c2"

    def test_schema_version_too_new_raises(self, state_path: Path) -> None:
        state_path.write_text(
            json.dumps({"schema_version": 99, "jobs": {}}),
            encoding="utf-8",
        )
        with pytest.raises(CorruptStateError, match="Unsupported"):
            Watchlist(state_path, allowed_owners=_TEST_OWNERS)

    def test_malformed_jobs_array_raises(self, state_path: Path) -> None:
        """jobs: [] must not be normalized to {} (would wipe durable data on save)."""
        state_path.write_text(
            json.dumps({"schema_version": 1, "jobs": []}),
            encoding="utf-8",
        )
        with pytest.raises(CorruptStateError, match="Malformed jobs"):
            Watchlist(state_path, allowed_owners=_TEST_OWNERS)

    def test_concurrent_adds_preserve_both_jobs(self, state_path: Path) -> None:
        """Two processes adding different jobs must not drop the earlier write."""
        w1 = Watchlist(state_path, allowed_owners=_TEST_OWNERS)
        w2 = Watchlist(state_path, allowed_owners=_TEST_OWNERS)
        w1.add("j1", "acme", "r1", "br")
        w2.add("j2", "acme", "r2", "br")
        reloaded = Watchlist(state_path, allowed_owners=_TEST_OWNERS)
        ids = {j.job_id for j in reloaded.list_jobs()}
        assert ids == {"j1", "j2"}

    def test_io_error_on_directory_state(self, tmp_path: Path) -> None:
        d = tmp_path / "not-a-file"
        d.mkdir()
        with pytest.raises(CorruptStateError, match=r"Cannot read|Cannot write|Cannot create"):
            Watchlist(d, allowed_owners=_TEST_OWNERS)


class TestCliJsonEnvelopes:
    """--json emits v1 envelopes for mutations and check categories."""

    def test_json_add_and_remove(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        state = str(tmp_path / "watchlist.json")
        assert (
            main(
                [
                    "--json",
                    "--state",
                    state,
                    "watchlist",
                    "add",
                    "j1",
                    "acme",
                    "repo",
                    "br",
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["schema_version"] == 1
        assert payload["command"] == "watchlist.add"
        assert payload["data"]["job"]["job_id"] == "j1"
        assert "Added job" not in out

        assert main(["--json", "--state", state, "watchlist", "remove", "j1"]) == 0
        out2 = capsys.readouterr().out
        rem = json.loads(out2)
        assert rem["ok"] is True
        assert rem["command"] == "watchlist.remove"
        assert rem["data"]["removed"] is True
        assert "Removed job" not in out2

    def test_json_check_includes_blockers_and_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        state = tmp_path / "watchlist.json"
        w = Watchlist(state, allowed_owners=_TEST_OWNERS)
        w.add("j1", "acme", "repo", "br")
        w.set_pr("j1", 7, "https://example.com/pr/7")
        w.set_blockers("j1", ["ruff", "review"])
        assert main(["--json", "--state", str(state), "watchlist", "check"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["command"] == "watchlist.check"
        needs = payload["data"]["categories"]["needs_fix"]
        assert len(needs) == 1
        item = needs[0]
        assert item["residual_blockers"] == ["ruff", "review"]
        assert item["fix_count"] == 0
        assert item["max_fixes"] is None
        assert item["fix_budget_remaining"] is None

    def test_json_add_includes_stack_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        state = str(tmp_path / "watchlist.json")
        assert (
            main(
                [
                    "--json",
                    "--state",
                    state,
                    "watchlist",
                    "add",
                    "j1",
                    "acme",
                    "repo",
                    "br",
                    "--stack-id",
                    "s1",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["job"]["stack_id"] == "s1"

    def test_json_check_corrupt_uses_categories_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from worktrees_hives.cli import main

        monkeypatch.setenv("WH_ALLOWED_OWNERS", "acme")
        bad = tmp_path / "broken.json"
        bad.write_text("{not-json", encoding="utf-8")
        code = main(["--json", "--state", str(bad), "watchlist", "check"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["command"] == "watchlist.check"
        assert "categories" in payload["data"]
        assert "jobs" not in payload["data"]
