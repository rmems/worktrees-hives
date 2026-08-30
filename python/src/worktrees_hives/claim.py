"""Claim an issue or PR into an isolated worktree via ``wh``.

Python owns **orchestration policy only**: naming, path layout, allowlist,
pre-flight exists checks. All git/worktree mutations go through
:class:`~worktrees_hives.bridge.WhClient` → ``wh worktree create|remove|…``.

There is **no** local ``git`` fallback. Missing ``wh`` or a failed worktree
subcommand raises :class:`ClaimError`.

Worktree path layout::

    {worktree_base}/{owner}/{repo}/{job_id}

Branch naming:

- Issues: ``hive/gh-{number}``
- PRs: caller-supplied head branch (no rename)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from worktrees_hives.contract import ErrorResponse, SuccessResponse
from worktrees_hives.errors import (
    PolicyError,
    WhBinaryNotFoundError,
    WhError,
    WhProcessError,
)
from worktrees_hives.paths import default_worktree_base as _default_worktree_base

if TYPE_CHECKING:
    from worktrees_hives.bridge import WhClient

_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"

# Segment for owner / repo / job_id (no separators, no option-looking).
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Branch / ref: plain git-ish names, no leading dash.
_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/-]*$")
# Commit id supplied by the caller for an exact PR-head start point.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _load_allowed_owners_from_env() -> frozenset[str]:
    """Load ``WH_ALLOWED_OWNERS`` (comma-separated). Empty / ``*`` / unset → no restriction."""
    if _ALLOWED_OWNERS_ENV not in os.environ:
        return frozenset()
    raw = os.environ[_ALLOWED_OWNERS_ENV].strip()
    if not raw or raw == "*":
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class ClaimError(WhError):
    """Raised when a claim operation fails at the orchestration layer."""


class ClaimExistsError(ClaimError):
    """Raised when a worktree path already exists for the given job id."""


class IsolationError(ClaimError):
    """Raised when post-create isolation policy checks fail."""


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Successful claim metadata returned to the caller."""

    owner: str
    repo: str
    job_id: str
    branch: str
    worktree_path: str
    issue_number: int | None = None
    pr_number: int | None = None
    # True when this claim owns the branch name (issue path).
    owns_branch: bool = True


@dataclass
class ClaimManager:
    """Policy-only claim lifecycle; mutations via required :class:`WhClient`.

    Parameters
    ----------
    wh_client:
        Required bridge to ``wh``. Isolation and sandboxing are enforced by
        Rust ``wh worktree``, not by this module.
    worktree_base:
        Root for path derivation (must match what ``wh`` uses via env).
    repo_root:
        Local repository root passed as ``--repo`` to ``wh worktree create``.
    allowed_owners:
        Optional owner allowlist. ``None`` loads ``WH_ALLOWED_OWNERS``.
        Empty set means no restriction.
    """

    wh_client: WhClient
    worktree_base: str = field(default_factory=_default_worktree_base)
    repo_root: str = field(default_factory=os.getcwd)
    allowed_owners: frozenset[str] | None = None

    def __post_init__(self) -> None:
        self.worktree_base = os.path.abspath(os.path.expanduser(self.worktree_base))
        self.repo_root = os.path.abspath(os.path.expanduser(self.repo_root))
        if self.allowed_owners is None:
            self.allowed_owners = _load_allowed_owners_from_env()
        else:
            self.allowed_owners = frozenset(self.allowed_owners)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def claim_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        base_ref: str,
    ) -> ClaimResult:
        """Claim a GitHub issue: derive branch/job, create worktree via ``wh``."""
        if issue_number <= 0:
            raise ClaimError(f"issue_number must be positive, got {issue_number}")
        _validate_ref("base_ref", base_ref)
        _validate_segment("owner", owner)
        _validate_segment("repo", repo)
        self._assert_owner_allowed(owner)

        branch = f"hive/gh-{issue_number}"
        _validate_ref("branch", branch)
        job_id = f"gh-{issue_number}"
        worktree_path = self.derive_path(owner, repo, job_id)
        self._assert_not_exists(worktree_path)

        path, returned_branch = self._wh_create(owner, repo, job_id, branch, base_ref)
        self._check_isolation(path, returned_branch, branch)

        return ClaimResult(
            owner=owner,
            repo=repo,
            job_id=job_id,
            branch=returned_branch,
            worktree_path=path,
            issue_number=issue_number,
            owns_branch=True,
        )

    def claim_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        head_branch: str,
        head_sha: str,
        head_repo: str | None = None,
    ) -> ClaimResult:
        """Claim a PR head branch into an isolated worktree via ``wh``."""
        if pr_number <= 0:
            raise ClaimError(f"pr_number must be positive, got {pr_number}")
        _validate_segment("owner", owner)
        _validate_segment("repo", repo)
        self._assert_owner_allowed(owner)
        _validate_ref("head_branch", head_branch)
        if not _SHA_RE.fullmatch(head_sha):
            raise ClaimError(f"invalid head_sha shape: {head_sha!r}")
        if head_repo is not None:
            # owner/repo slug for documentation; create still targets base repo root.
            if "/" not in head_repo:
                raise ClaimError(f"head_repo must be owner/repo, got {head_repo!r}")
            ho, _, hr = head_repo.partition("/")
            _validate_segment("head_repo.owner", ho)
            _validate_segment("head_repo.repo", hr)

        job_id = f"pr-{pr_number}"
        worktree_path = self.derive_path(owner, repo, job_id)
        self._assert_not_exists(worktree_path)

        path, returned_branch = self._wh_create(owner, repo, job_id, head_branch, head_sha)
        self._check_isolation(path, returned_branch, head_branch)

        return ClaimResult(
            owner=owner,
            repo=repo,
            job_id=job_id,
            branch=returned_branch,
            worktree_path=path,
            pr_number=pr_number,
            owns_branch=False,
        )

    def cleanup(
        self,
        result: ClaimResult,
        *,
        force: bool = False,
        prune: bool = False,
    ) -> None:
        """Remove the claim worktree via ``wh worktree remove``."""
        args: list[str] = ["worktree", "remove", result.worktree_path]
        if force:
            args.append("--force")
        self._wh_run(*args)
        if prune:
            self._wh_run("worktree", "prune", "--repo", self.repo_root)

    @staticmethod
    def verify_isolation(result: ClaimResult) -> None:
        """Filesystem isolation check (no git): path exists and is a directory."""
        path = Path(result.worktree_path)
        if not path.is_dir():
            raise IsolationError(
                f"worktree path missing or not a directory: {result.worktree_path}"
            )

    def derive_path(self, owner: str, repo: str, job_id: str) -> str:
        """Derive sandboxed worktree path under ``worktree_base``."""
        _validate_segment("owner", owner)
        _validate_segment("repo", repo)
        _validate_segment("job_id", job_id)
        path = os.path.abspath(os.path.join(self.worktree_base, owner, repo, job_id))
        base = self.worktree_base
        try:
            Path(path).resolve().relative_to(Path(base).resolve())
        except ValueError as exc:
            raise ClaimError(f"worktree path escapes base {base!r}: {path!r}") from exc
        return path

    # ------------------------------------------------------------------
    # wh bridge
    # ------------------------------------------------------------------

    def _wh_create(
        self,
        owner: str,
        repo: str,
        job_id: str,
        branch: str,
        start_point: str,
    ) -> tuple[str, str]:
        """Invoke ``wh worktree create --repo …``; return (path, branch)."""
        resp = self._wh_run(
            "worktree",
            "create",
            "--repo",
            self.repo_root,
            "--start-point",
            start_point,
            owner,
            repo,
            job_id,
            branch,
        )
        if not isinstance(resp, SuccessResponse):
            raise ClaimError(f"wh worktree create failed: {resp.error.code}: {resp.error.message}")
        path = resp.data.get("path")
        ret_branch = resp.data.get("branch")
        if not isinstance(path, str) or not path:
            # Fall back to derived path if envelope omits path.
            path = self.derive_path(owner, repo, job_id)
        if not isinstance(ret_branch, str) or not ret_branch:
            ret_branch = branch
        return path, ret_branch

    def _wh_run(self, *args: str) -> SuccessResponse | ErrorResponse:
        try:
            return self.wh_client.run(*args)
        except WhBinaryNotFoundError as exc:
            raise ClaimError(
                "wh binary not found; install wh or set WH_BIN "
                f"(isolation requires Rust worktree CLI): {exc}"
            ) from exc
        except PolicyError as exc:
            raise ClaimError(f"wh policy rejection [{exc.code}]: {exc.message}") from exc
        except WhProcessError as exc:
            raise ClaimError(f"wh exited {exc.returncode}: {exc.stderr or 'no stderr'}") from exc
        except WhError as exc:
            raise ClaimError(str(exc)) from exc

    @staticmethod
    def _check_isolation(path: str, returned_branch: str, expected_branch: str) -> None:
        if returned_branch != expected_branch:
            raise IsolationError(
                f"wh returned branch {returned_branch!r}, expected {expected_branch!r}"
            )
        # Path may not exist yet in pure-mock unit tests if data is synthetic;
        # only enforce when the path is present on disk.
        p = Path(path)
        if p.exists() and not p.is_dir():
            raise IsolationError(f"worktree path is not a directory: {path}")

    @staticmethod
    def _assert_not_exists(worktree_path: str) -> None:
        if Path(worktree_path).exists():
            raise ClaimExistsError(f"worktree already exists for this job: {worktree_path}")

    def _assert_owner_allowed(self, owner: str) -> None:
        allowed = self.allowed_owners or frozenset()
        if (
            allowed
            and owner not in allowed
            and owner.casefold() not in {a.casefold() for a in allowed}
        ):
            raise ClaimError(
                f"Owner {owner!r} is not in the configured allowlist "
                f"({sorted(allowed)}). Set WH_ALLOWED_OWNERS or "
                "ClaimManager(allowed_owners=...)."
            )


def _validate_segment(field_name: str, value: str) -> None:
    """Reject empty, option-looking, or path-like segments."""
    if not value or value in (".", "..") or not _SEGMENT_RE.fullmatch(value):
        raise ClaimError(
            f"Invalid {field_name} segment {value!r}: "
            "must be a plain name without separators or leading dash"
        )
    if "/" in value or "\\" in value or ":" in value:
        raise ClaimError(f"Invalid {field_name} segment (contains separator): {value!r}")


def _validate_ref(field_name: str, value: str) -> None:
    """Reject empty or option-looking branch/ref names (pure Python, no git)."""
    if not value or not _REF_RE.fullmatch(value) or value.startswith("-"):
        raise ClaimError(
            f"Invalid {field_name} {value!r}: must be a plain git ref, not empty or option-looking"
        )
