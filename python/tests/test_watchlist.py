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

    def test_add_with_options(self, watchlist: Watchlist) -> None:
        job = watchlist.add("j1", "acme", "repo", "br", stack_id="s1")
        assert job.stack_id == "s1"

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


class TestWatchlistSchemaMigration:
    def test_v1_mutation_rewrites_v2_without_babysit_state(self, state_path: Path) -> None:
        """A v1 record keeps additive fields but drops retired babysit state on save."""
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "legacy": {
                            "job_id": "legacy",
                            "owner": "acme",
                            "repo": "r",
                            "branch": "br",
                            "status": "pending",
                            "fix_count": 2,
                            "max_fixes": 3,
                            "babysit_cycle": "before-removal",
                            "residual_blockers": [],
                            "future_job_key": {"source": "another-writer"},
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        w = Watchlist(state_path, allowed_owners=frozenset({"acme"}))
        assert w.get("legacy") is not None

        w.update_status("legacy", JobStatus.IN_PROGRESS)

        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert reloaded["schema_version"] == 2
        entry = reloaded["jobs"]["legacy"]
        assert entry["status"] == "in_progress"
        assert entry["future_job_key"] == {"source": "another-writer"}
        for field_name in ("fix_count", "max_fixes", "babysit_cycle"):
            assert field_name not in entry

    @pytest.mark.parametrize("schema", [True, False, "1", "2", 2.0, 2.9])
    def test_schema_version_must_be_int_1_or_2(self, state_path: Path, schema: object) -> None:
        """Reject bools, numeric strings, and floats; integers 1 and 2 stay valid."""
        state_path.write_text(
            json.dumps({"schema_version": schema, "jobs": {}}),
            encoding="utf-8",
        )
        with pytest.raises(CorruptStateError, match="Unsupported"):
            Watchlist(state_path, allowed_owners=_TEST_OWNERS)

    @pytest.mark.parametrize("schema", [1, 2])
    def test_schema_version_int_1_or_2_loads(self, state_path: Path, schema: int) -> None:
        """Integers 1 and 2 load, and the next mutation rewrites the file as v2."""
        state_path.write_text(
            json.dumps({"schema_version": schema, "jobs": {}}),
            encoding="utf-8",
        )
        w = Watchlist(state_path, allowed_owners=_TEST_OWNERS)
        assert w.list_jobs() == []

        w.add("j1", "acme", "repo", "br")

        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert reloaded["schema_version"] == 2


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

    def test_explicitly_blocked_job_with_blockers_is_blocked(self, watchlist: Watchlist) -> None:
        watchlist.add("j1", "acme", "r1", "br")
        watchlist.set_pr("j1", 1, "https://example.com/pr/1")
        watchlist.set_blockers("j1", ["still failing"])
        watchlist.update_status("j1", JobStatus.BLOCKED)
        result = watchlist.check()
        assert len(result["blocked"]) == 1
        assert result["blocked"][0].job_id == "j1"
        assert result["ready"] == []
        assert result["needs_fix"] == []

    def test_green_pr_is_ready(self, watchlist: Watchlist) -> None:
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

    def test_add_disallowed_owner_policy_returns_2(
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
                "other-owner",
                "repo",
                "br",
            ]
        )
        assert code == 2

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
    """--json emits v2 envelopes for mutations and check categories."""

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
        assert payload["schema_version"] == 2
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

    def test_json_check_includes_blockers_without_babysit_budget(
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
        for field_name in ("fix_count", "max_fixes", "fix_budget_remaining", "babysit_cycle"):
            assert field_name not in item

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

    def test_json_list_envelope_and_corrupt_shape(
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
        capsys.readouterr()
        assert main(["--json", "--state", state, "watchlist", "list"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["schema_version"] == 2
        assert payload["command"] == "watchlist.list"
        assert [j["job_id"] for j in payload["data"]["jobs"]] == ["j1"]

        broken = tmp_path / "broken.json"
        broken.write_text("{not-json", encoding="utf-8")
        assert main(["--json", "--state", str(broken), "watchlist", "list"]) == 1
        corrupt = json.loads(capsys.readouterr().out)
        assert corrupt["ok"] is False
        assert corrupt["command"] == "watchlist.list"
        assert corrupt["data"] == {"jobs": []}
        assert corrupt["error"]["code"] == "CORRUPT_STATE"
