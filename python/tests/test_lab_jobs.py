"""Tests for lab job model (GH #85) — not babysit watchlist jobs."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from worktrees_hives.contract import ErrorData, ErrorResponse, SuccessResponse
from worktrees_hives.findings import AgentRole
from worktrees_hives.lab_jobs import (
    LabJob,
    LabJobError,
    LabJobExistsError,
    LabJobManager,
    LabJobNotFoundError,
    LabJobStatus,
    LabJobStore,
    default_lab_jobs_path,
)
from worktrees_hives.watchlist import JobState, JobStatus

if TYPE_CHECKING:
    from pathlib import Path

TEST_OWNER = "acme"
TEST_REPO = "example-repo"
TEST_COMMIT = "a" * 40


def _ok_create(
    path: str = "/tmp/wt/acme/example-repo/lab-H1",
    branch: str = "lab/H-001",
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


def _ok_remove() -> SuccessResponse:
    return SuccessResponse(command="worktree.remove", data={}, schema_version=1)


def _manager(
    tmp_path: Path,
    mock_wh: MagicMock | None = None,
    *,
    worktree_base: str | None = None,
    allowed_owners: frozenset[str] | None = frozenset({TEST_OWNER}),
) -> tuple[LabJobManager, MagicMock, LabJobStore]:
    wh = mock_wh if mock_wh is not None else MagicMock()
    store = LabJobStore(tmp_path / "lab_jobs.json")
    base = worktree_base if worktree_base is not None else str(tmp_path / "wt")
    mgr = LabJobManager(
        wh_client=wh,
        worktree_base=base,
        repo_root=str(tmp_path / "repo"),
        store=store,
        allowed_owners=allowed_owners,
    )
    return mgr, wh, store


class TestSeparationFromBabysit:
    def test_types_are_not_watchlist_jobs(self) -> None:
        assert LabJob is not JobState
        assert LabJobStatus is not JobStatus
        assert set(LabJobStatus) != set(JobStatus)


class TestLabJobStore:
    @pytest.mark.parametrize("start_point", ["origin/main", TEST_COMMIT])
    def test_round_trip(self, tmp_path: Path, start_point: str) -> None:
        store = LabJobStore(tmp_path / "lab_jobs.json")
        now = "2026-08-12T00:00:00Z"
        job = LabJob(
            job_id="lab-H-001",
            hypothesis_id="H-001",
            agent_id="grok",
            role=AgentRole.AGENT,
            owner=TEST_OWNER,
            repo=TEST_REPO,
            branch="lab/H-001",
            worktree_path="/tmp/wt/acme/example-repo/lab-H-001",
            status=LabJobStatus.ALLOCATED,
            created_at=now,
            updated_at=now,
            start_point=start_point,
        )
        store.put(job)
        store2 = LabJobStore(tmp_path / "lab_jobs.json")
        loaded = store2.get("lab-H-001")
        assert loaded is not None
        assert loaded.hypothesis_id == "H-001"
        assert loaded.role is AgentRole.AGENT
        assert loaded.status is LabJobStatus.ALLOCATED
        assert loaded.start_point == start_point
        assert loaded.start_commit is None

    def test_legacy_record_without_start_identity_remains_readable(self) -> None:
        now = "2026-08-12T00:00:00Z"
        raw = LabJob(
            job_id="legacy",
            hypothesis_id="H-old",
            agent_id="agent",
            role=AgentRole.AGENT,
            owner=TEST_OWNER,
            repo=TEST_REPO,
            branch="lab/H-old",
            worktree_path="/tmp/legacy",
            status=LabJobStatus.ALLOCATED,
            created_at=now,
            updated_at=now,
        ).to_dict()
        raw.pop("start_point")
        raw.pop("start_commit")
        loaded = LabJob.from_dict(raw)
        assert loaded.start_point is None
        assert loaded.start_commit is None

    def test_default_path_uses_user_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WH_LAB_JOBS_PATH", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(
            "worktrees_hives.lab_jobs.user_data_dir",
            lambda platform=None: "/data",
        )
        assert (
            str(default_lab_jobs_path())
            .replace("\\", "/")
            .endswith("worktrees-hives/lab_jobs.json")
        )


class TestAllocate:
    def test_happy_path(self, tmp_path: Path) -> None:
        mgr, wh, store = _manager(tmp_path)
        path = os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-H-001")
        wh.run.return_value = _ok_create(path=path, branch="lab/H-001")
        job = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok-lab",
            role=AgentRole.AGENT,
        )
        assert job.status is LabJobStatus.ALLOCATED
        assert job.job_id == "lab-H-001"
        assert job.worktree_path == path
        assert job.agent_id == "grok-lab"
        assert job.start_point == "origin/main"
        assert job.start_commit == TEST_COMMIT
        assert store.get(job.job_id) is not None
        args = wh.run.call_args[0]
        assert args[0:4] == ("worktree", "create", "--schema-version", "2")
        assert args[4:6] == ("--repo", str(tmp_path / "repo"))
        assert args[6:8] == ("--start-point", "origin/main")
        assert args[8:12] == (TEST_OWNER, TEST_REPO, "lab-H-001", "lab/H-001")

    def test_pending_reservation_persists_normalized_start_point(self, tmp_path: Path) -> None:
        mgr, wh, store = _manager(tmp_path)

        def inspect_pending(*_args: str, **_kwargs: object) -> SuccessResponse:
            pending = store.get("lab-H-upper")
            assert pending is not None
            assert pending.status is LabJobStatus.PENDING
            assert pending.start_point == TEST_COMMIT
            assert pending.worktree_path == os.path.join(
                str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-H-upper"
            )
            return _ok_create(
                path=os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-H-upper"),
                branch="lab/H-upper",
            )

        wh.run.side_effect = inspect_pending
        job = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point=TEST_COMMIT.upper(),
            hypothesis_id="H-upper",
            agent_id="agent",
            role=AgentRole.AGENT,
        )
        assert wh.run.call_args.args[7] == TEST_COMMIT
        assert job.start_point == TEST_COMMIT

    @pytest.mark.parametrize(
        ("start_commit", "head_commit", "message"),
        [
            (None, TEST_COMMIT, "start_commit"),
            ("malformed", TEST_COMMIT, "start_commit"),
            (TEST_COMMIT, "b" * 40, "does not equal"),
        ],
    )
    def test_rejects_invalid_commit_identity_response(
        self,
        tmp_path: Path,
        start_commit: object,
        head_commit: object,
        message: str,
    ) -> None:
        mgr, wh, store = _manager(tmp_path)
        wh.run.return_value = _ok_create(start_commit=start_commit, head_commit=head_commit)
        with pytest.raises(LabJobError, match=message):
            mgr.allocate(
                owner=TEST_OWNER,
                repo=TEST_REPO,
                start_point="origin/main",
                hypothesis_id="H-bad",
                agent_id="agent",
                role=AgentRole.AGENT,
            )
        assert store.list_jobs() == []

    def test_full_sha_response_must_match_request(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path)
        wh.run.return_value = _ok_create(start_commit="b" * 40, head_commit="b" * 40)
        with pytest.raises(LabJobError, match="requested full object id"):
            mgr.allocate(
                owner=TEST_OWNER,
                repo=TEST_REPO,
                start_point=TEST_COMMIT,
                hypothesis_id="H-sha",
                agent_id="agent",
                role=AgentRole.AGENT,
            )

    def test_full_sha_response_exact_match_is_accepted(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path)
        wh.run.return_value = _ok_create(
            path=os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-H-exact"),
            branch="lab/H-exact",
        )
        job = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point=TEST_COMMIT,
            hypothesis_id="H-exact",
            agent_id="agent",
            role=AgentRole.AGENT,
        )
        assert job.start_commit == TEST_COMMIT

    def test_rejects_abbreviated_sha_request(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path)
        with pytest.raises(LabJobError, match="full 40- or 64-character"):
            mgr.allocate(
                owner=TEST_OWNER,
                repo=TEST_REPO,
                start_point="abc1234",
                hypothesis_id="H-short",
                agent_id="agent",
                role=AgentRole.AGENT,
            )
        wh.run.assert_not_called()

    def test_duplicate_job_id(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path)
        path = os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-fixed")
        wh.run.return_value = _ok_create(path=path, branch="lab/H-001")
        mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="a",
            role="agent",
            job_id="lab-fixed",
            branch="lab/H-001",
        )
        with pytest.raises(LabJobExistsError):
            mgr.allocate(
                owner=TEST_OWNER,
                repo=TEST_REPO,
                start_point="origin/main",
                hypothesis_id="H-001",
                agent_id="b",
                role="subagent",
                job_id="lab-fixed",
                branch="lab/H-001-b",
            )
        assert wh.run.call_count == 1

    def test_same_hypothesis_auto_suffixes_job_id(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path)

        def _side_effect(*args: str, **_kwargs: object) -> SuccessResponse:
            jid = args[10]
            branch = args[11]
            path = os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, jid)
            return _ok_create(path=path, branch=branch)

        wh.run.side_effect = _side_effect
        j1 = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="a",
            role="agent",
        )
        j2 = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="b",
            role="subagent",
        )
        assert j1.job_id == "lab-H-001"
        assert j2.job_id.startswith("lab-H-001-")
        assert j1.job_id != j2.job_id

    def test_allowlist_blocks_before_wh(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path, allowed_owners=frozenset({TEST_OWNER}))
        with pytest.raises(LabJobError, match="allowlist"):
            mgr.allocate(
                owner="evil",
                repo=TEST_REPO,
                start_point="origin/main",
                hypothesis_id="H-1",
                agent_id="a",
                role=AgentRole.AGENT,
            )
        wh.run.assert_not_called()

    def test_empty_allowlist_denies(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path, allowed_owners=frozenset())
        with pytest.raises(LabJobError, match=r"deny-by-default|empty"):
            mgr.allocate(
                owner=TEST_OWNER,
                repo=TEST_REPO,
                start_point="origin/main",
                hypothesis_id="H-1",
                agent_id="a",
                role=AgentRole.AGENT,
            )
        wh.run.assert_not_called()

    def test_two_jobs_distinct_paths(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path)
        p1 = os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-A")
        p2 = os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-B")

        def _side_effect(*args: str, **_kwargs: object) -> SuccessResponse:
            jid = args[10]
            path = p1 if jid == "lab-A" else p2
            return _ok_create(path=path, branch=f"lab/{jid}")

        wh.run.side_effect = _side_effect
        j1 = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point="origin/main",
            hypothesis_id="A",
            agent_id="a1",
            role=AgentRole.AGENT,
            job_id="lab-A",
            branch="lab/lab-A",
        )
        j2 = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point="origin/main",
            hypothesis_id="B",
            agent_id="a2",
            role=AgentRole.SUBAGENT,
            job_id="lab-B",
            branch="lab/lab-B",
        )
        assert j1.worktree_path != j2.worktree_path
        assert j1.job_id != j2.job_id
        listed = mgr.list_jobs()
        assert {j.job_id for j in listed} == {"lab-A", "lab-B"}

    def test_allocate_aborts_if_teardown_during_create(self, tmp_path: Path) -> None:
        """Concurrent teardown must not be overwritten by a late allocate put."""
        mgr, wh, store = _manager(tmp_path)
        jid = "lab-race"
        path = os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, jid)

        calls: list[tuple[str, ...]] = []

        def _run(*args: str, **_kwargs: object) -> SuccessResponse:
            calls.append(args)
            if args[0:2] == ("worktree", "create"):
                existing = store.get(jid)
                assert existing is not None
                assert existing.status is LabJobStatus.PENDING
                store.put(
                    replace(
                        existing,
                        status=LabJobStatus.TORN_DOWN,
                        updated_at="2026-08-12T01:00:00Z",
                    )
                )
                return _ok_create(path=path, branch="lab/H-race")
            return _ok_remove()

        wh.run.side_effect = _run
        with pytest.raises(LabJobError, match="allocation aborted"):
            mgr.allocate(
                owner=TEST_OWNER,
                repo=TEST_REPO,
                start_point="origin/main",
                hypothesis_id="H-race",
                agent_id="a",
                role=AgentRole.AGENT,
                job_id=jid,
                branch="lab/H-race",
            )
        final = store.get(jid)
        assert final is not None
        assert final.status is LabJobStatus.TORN_DOWN
        # Compensating remove after aborted commit.
        assert len(calls) == 2
        assert calls[1][0:2] == ("worktree", "remove")


class TestTeardown:
    def test_teardown_tombstone(self, tmp_path: Path) -> None:
        mgr, wh, store = _manager(tmp_path)
        path = os.path.join(str(tmp_path / "wt"), TEST_OWNER, TEST_REPO, "lab-H-001")
        wh.run.side_effect = [
            _ok_create(path=path, branch="lab/H-001"),
            _ok_remove(),
        ]
        job = mgr.allocate(
            owner=TEST_OWNER,
            repo=TEST_REPO,
            start_point="origin/main",
            hypothesis_id="H-001",
            agent_id="grok",
            role=AgentRole.AGENT,
        )
        done = mgr.teardown(job.job_id)
        assert done.status is LabJobStatus.TORN_DOWN
        assert store.get(job.job_id) is not None
        assert store.get(job.job_id).status is LabJobStatus.TORN_DOWN  # type: ignore[union-attr]
        remove_args = wh.run.call_args_list[1][0]
        assert remove_args[0:2] == ("worktree", "remove")
        assert remove_args[2] == path

    def test_teardown_missing(self, tmp_path: Path) -> None:
        mgr, _, _ = _manager(tmp_path)
        with pytest.raises(LabJobNotFoundError):
            mgr.teardown("nope")

    def test_wh_error_surfaces(self, tmp_path: Path) -> None:
        mgr, wh, _ = _manager(tmp_path)
        wh.run.return_value = ErrorResponse(
            command="worktree.create",
            error=ErrorData(code="WORKTREE_CREATE_FAILED", message="nope"),
            schema_version=2,
            data={"path": "/tmp/residual"},
        )
        with pytest.raises(LabJobError, match="create failed") as exc_info:
            mgr.allocate(
                owner=TEST_OWNER,
                repo=TEST_REPO,
                start_point="origin/main",
                hypothesis_id="H-9",
                agent_id="a",
                role=AgentRole.AGENT,
            )
        assert exc_info.value.code == "WORKTREE_CREATE_FAILED"
        assert exc_info.value.data == {"path": "/tmp/residual"}
