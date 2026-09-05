"""Exact-base contract gates: start-point identity and verified path equality."""

from __future__ import annotations

from pathlib import Path

import pytest

from worktrees_hives.contract import (
    parse_verified_worktree_creation,
    validate_start_point_request,
)
from worktrees_hives.errors import WhSchemaError


class TestValidateStartPointRequest:
    """EXACT-BASE-ABBREV-COMMITISH-SUFFIX: decorated abbrevs cannot bypass."""

    def test_rejects_pure_abbreviated_hex(self) -> None:
        with pytest.raises(WhSchemaError, match="full 40- or 64-character"):
            validate_start_point_request("abc1234")

    @pytest.mark.parametrize(
        "start_point",
        ["abc1234~0", "abc1234^0", "abc1234^{commit}", "abc1234@{0}"],
    )
    def test_rejects_decorated_abbreviated_hex(self, start_point: str) -> None:
        with pytest.raises(WhSchemaError, match="full 40- or 64-character"):
            validate_start_point_request(start_point)

    def test_rejects_sha256_width_prefix_with_decoration(self) -> None:
        with pytest.raises(WhSchemaError, match="full 40- or 64-character"):
            validate_start_point_request(("a" * 12) + "~0")

    @pytest.mark.parametrize("commit", ["a" * 40, "b" * 64, "A" * 40])
    def test_accepts_full_object_id(self, commit: str) -> None:
        assert validate_start_point_request(commit) == commit.lower()

    @pytest.mark.parametrize(
        "start_point",
        ["origin/main", "refs/heads/main", "refs/tags/v1.0", "develop", "feature/ok"],
    )
    def test_accepts_symbolic_refs(self, start_point: str) -> None:
        assert validate_start_point_request(start_point) == start_point

    def test_accepts_non_hex_branch_peel(self) -> None:
        assert validate_start_point_request("develop~1") == "develop~1"


class TestParseVerifiedWorktreeCreationPath:
    """PATH-ORACLE-PYTHON-RESOLVE-WEAK: crafted `..` paths must not pass."""

    def _payload(self, path: str) -> dict[str, object]:
        return {
            "path": path,
            "branch": "feature/job",
            "branch_ref": "refs/heads/feature/job",
            "start_commit": "a" * 40,
            "head_commit": "a" * 40,
            "worktree_registered": True,
        }

    def test_accepts_exact_verified_path(self) -> None:
        path = "/tmp/worktrees/acme/repo/job"
        creation = parse_verified_worktree_creation(
            self._payload(path),
            requested_start_point="a" * 40,
            expected_path=path,
            expected_branch="feature/job",
        )
        assert creation.path == path

    def test_rejects_oracle_dotdot_spelling(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        job = repo / "j"
        job.mkdir(parents=True)
        expected = str(job)
        crafted = str(repo / ".." / "r" / "j")
        assert Path(crafted).resolve() == Path(expected).resolve()

        with pytest.raises(WhSchemaError, match="expected worktree path"):
            parse_verified_worktree_creation(
                self._payload(crafted),
                requested_start_point="a" * 40,
                expected_path=expected,
                expected_branch="feature/job",
            )
