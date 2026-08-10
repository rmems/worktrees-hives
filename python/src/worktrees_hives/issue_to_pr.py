"""Issue-to-PR workflow orchestrator.

Drives the lifecycle from a GitHub issue to a ready-to-review pull request:

1. Create an isolated worktree and feature branch via ``wh``.
2. Coordinate with a worker agent that implements the changes in that worktree.
3. Push the branch and open a PR via ``gh``.
4. Link the PR back to the source issue (``Closes #N``).

Safety invariant: **this module never auto-merges any PR.**
The merge decision remains exclusively with a human operator.

Push and ``gh`` mutations should eventually go through Rust ``wh git`` /
``wh gh`` safe wrappers. Until those surfaces cover this workflow on the
branch, this module validates arguments carefully and never invokes merge.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from worktrees_hives.bridge import WhClient
from worktrees_hives.contract import SuccessResponse
from worktrees_hives.errors import WhError
from worktrees_hives.paths import default_worktree_base as _default_worktree_base

# Remote names must be plain git remote identifiers — never option-looking values
# that would be interpreted as flags by `git push` (e.g. `--force`, `-f`).
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

# Branch names (base / head) — plain refs, no leading dash / option injection.
_BRANCH_NAME_RE = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/\-]*$")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssueToPrConfig:
    """Parameters for a single issue-to-PR run.

    Attributes
    ----------
    owner:
        GitHub repository owner (e.g. ``"acme"``).
    repo:
        GitHub repository name (e.g. ``"example-repo"``).
    issue_number:
        The GitHub issue number to convert into a PR.
    base_branch:
        Branch to create the feature branch from (default ``"main"``).
        Validated like remotes — must not be option-looking.
    remote:
        Git remote name to push to (default ``"origin"``).
    repo_path:
        Local git repository root passed to ``wh worktree create --repo``.
        Defaults to the process current working directory.
    pr_labels:
        Labels to apply to the created PR.
    pr_milestone:
        Optional milestone title for the PR.
    auto_link:
        Whether to add ``Closes #N`` to the PR body (default ``True``).
    gh_path:
        Explicit path to the ``gh`` binary.  If ``None``, resolved via PATH.
    """

    owner: str
    repo: str
    issue_number: int
    base_branch: str = "main"
    remote: str = "origin"
    repo_path: str = field(default_factory=os.getcwd)
    pr_labels: list[str] = field(default_factory=list)
    pr_milestone: str | None = None
    auto_link: bool = True
    gh_path: str | None = None


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class Step(StrEnum):
    """Lifecycle steps for the issue-to-PR workflow."""

    INIT = "init"
    WORKTREE_CREATED = "worktree_created"
    BRANCH_PUSHED = "branch_pushed"
    PR_OPENED = "pr_opened"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IssueToPrError(WhError):
    """Raised when the issue-to-PR workflow encounters an unrecoverable error."""

    def __init__(self, step: Step, detail: str) -> None:
        self.step = step
        super().__init__(f"IssueToPr failed at step '{step.value}': {detail}")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssueToPrResult:
    """Outcome of a successful issue-to-PR run."""

    branch_name: str
    worktree_path: str
    pr_number: int
    pr_url: str


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

# Never-merge safety constant — used in PR body to make intent explicit.
_NEVER_MERGE_MARKER = "<!-- worktrees-hives: never-auto-merge -->"


def _is_forbidden_merge_cmd(cmd: list[str]) -> bool:
    """True for merge *subcommands/endpoints* by structure, not metadata text.

    Scans ``gh`` argv positionally so a repo named ``owner/merge``, a label
    ``merge``, or a title containing ``/merges`` cannot false-positive.
    Matches ``gh pr merge``, REST ``.../merges`` (or ``.../merge``) after
    ``gh api``, and GraphQL ``mergePullRequest`` tokens.
    """
    if len(cmd) < 2:
        return False
    # Drop binary (may be a path to gh).
    args = list(cmd[1:])
    for i in range(len(args) - 1):
        if args[i] == "pr" and args[i + 1] == "merge":
            return True
    for i, a in enumerate(args):
        if a != "api":
            continue
        j = i + 1
        # Skip flags (and one following value for common value-taking flags).
        value_flags = {
            "-X",
            "--method",
            "-H",
            "-f",
            "-F",
            "--jq",
            "--input",
            "--field",
            "--raw-field",
            "--header",
        }
        while j < len(args) and args[j].startswith("-"):
            if args[j] in value_flags:
                j += 2
            else:
                j += 1
        if j < len(args):
            endpoint = args[j].rstrip("/")
            if endpoint.endswith("/merges") or endpoint.endswith("/merge"):
                return True
            if "mergePullRequest" in endpoint:
                return True
        break
    return "mergePullRequest" in args or "merge-pull-request" in args


class IssueToPr:
    """Drive an issue from intake to PR creation.

    This orchestrator:
    - delegates worktree and branch creation to ``wh`` (Rust core),
    - shells out to ``git`` / ``gh`` only with validated arguments,
    - **never** calls any merge API or merge command.

    Prefer ``wh git`` / ``wh gh`` safe wrappers when available on the branch;
    until then subprocess calls are argument-validated and never merge.

    Parameters
    ----------
    config:
        Run-specific configuration (owner, repo, issue, labels, etc.).
    wh_client:
        Pre-configured ``WhClient`` for invoking the ``wh`` CLI.
    """

    def __init__(
        self,
        config: IssueToPrConfig,
        wh_client: WhClient | None = None,
    ) -> None:
        _validate_remote_name(config.remote)
        _validate_branch_name("base_branch", config.base_branch)
        _validate_path_segment("owner", config.owner)
        _validate_path_segment("repo", config.repo)
        if config.issue_number <= 0:
            raise IssueToPrError(
                Step.INIT,
                f"issue_number must be a positive integer, got {config.issue_number}",
            )
        self._cfg = config
        self._wh = wh_client or WhClient()
        self._step = Step.INIT

    # -- public API ---------------------------------------------------------

    def run(self) -> IssueToPrResult:
        """Execute the full issue-to-PR workflow.

        Steps: create worktree → push branch → open PR → link issue.

        Raises
        ------
        IssueToPrError
            If any step fails irrecoverably.
        """
        branch_name = self._branch_name()
        worktree_path = self._worktree_path()

        self._create_worktree(branch_name, worktree_path)
        self._push_branch(branch_name, worktree_path)
        pr_number, pr_url = self._open_pr(branch_name)

        return IssueToPrResult(
            branch_name=branch_name,
            worktree_path=worktree_path,
            pr_number=pr_number,
            pr_url=pr_url,
        )

    # -- step implementations -----------------------------------------------

    def _create_worktree(self, branch_name: str, worktree_path: str) -> None:
        """Ask ``wh`` to create an isolated worktree and branch.

        Aligned with foundation clap shape::

            wh worktree create --repo <path> <owner> <repo_name> <job_id> <branch>

        ``wh`` creates missing branches from HEAD only (no ``--base`` yet).
        Always pre-create (or force-reset) ``branch_name`` from the resolved
        ``base_branch`` so a dirty checkout HEAD cannot leak unrelated commits
        into the PR, and stale ``feature/issue-N`` tips are rewritten.
        """
        _validate_branch_name("branch", branch_name)
        self._ensure_branch_from_base(branch_name)
        job_id = f"issue-{self._cfg.issue_number}"
        try:
            resp = self._wh.run(
                "worktree",
                "create",
                "--repo",
                self._cfg.repo_path,
                self._cfg.owner,
                self._cfg.repo,
                job_id,
                branch_name,
            )
        except WhError as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.INIT, str(exc)) from exc

        if isinstance(resp, SuccessResponse):
            self._step = Step.WORKTREE_CREATED
            # Prefer path from response when present; keep derived for callers.
            _ = worktree_path
        else:
            self._step = Step.FAILED
            raise IssueToPrError(
                Step.INIT,
                f"wh returned error: {resp.error.code}: {resp.error.message}",
            )

    def _git_ok(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``git -C <repo_path> …`` and return the completed process."""
        repo = self._cfg.repo_path
        try:
            return subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.INIT, f"git timed out: {exc}") from exc
        except FileNotFoundError as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.INIT, f"git not found: {exc}") from exc
        except PermissionError as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.INIT, f"git not executable: {exc}") from exc

    def _resolve_start_point(self, base: str) -> str:
        """Resolve ``base`` to a local ref or ``{remote}/{base}`` start-point.

        Prefer an existing local branch; fall back to the remote-tracking ref
        so release bases that only exist as ``origin/release/…`` still work.
        """
        _validate_branch_name("base_branch", base)
        local = self._git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{base}")
        if local.returncode == 0:
            return base
        remote = self._cfg.remote
        tracking = f"refs/remotes/{remote}/{base}"
        remote_ok = self._git_ok("rev-parse", "--verify", "--quiet", tracking)
        if remote_ok.returncode == 0:
            return f"{remote}/{base}"
        self._step = Step.FAILED
        raise IssueToPrError(
            Step.INIT,
            f"base branch {base!r} not found as local ref or {remote}/{base}; "
            f"fetch the remote or create the branch first",
        )

    def _ensure_branch_from_base(self, branch_name: str) -> None:
        """Create or force-reset ``branch_name`` at the resolved base start-point.

        Always runs (including default ``main``) so the feature tip matches
        ``base_branch`` rather than whatever HEAD the orchestrator process
        currently has. Stale ``feature/issue-N`` branches are rewritten with
        ``git branch -f`` so a previous failed run cannot open a PR against
        the wrong history.
        """
        _validate_branch_name("branch", branch_name)
        start = self._resolve_start_point(self._cfg.base_branch)
        # -f: create if missing, move if present (stale reclaim).
        result = self._git_ok("branch", "-f", "--", branch_name, start)
        if result.returncode != 0:
            self._step = Step.FAILED
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise IssueToPrError(
                Step.INIT,
                f"could not create branch {branch_name!r} from {start!r}: {detail}",
            )

    def _push_branch(self, branch_name: str, worktree_path: str) -> None:
        """Push the feature branch to the remote.

        Uses ``git -C <worktree_path> push -u -- <remote> <branch>``.
        Remote and branch names are validated so option-looking values cannot
        turn into force-push flags.

        TODO: route through ``wh git run push ...`` / GitSafe when the Python
        bridge exposes typed safe push helpers.
        """
        _validate_remote_name(self._cfg.remote)
        _validate_branch_name("branch", branch_name)
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    worktree_path,
                    "push",
                    "-u",
                    "--",
                    self._cfg.remote,
                    branch_name,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.WORKTREE_CREATED, f"git push timed out: {exc}") from exc
        except FileNotFoundError as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.WORKTREE_CREATED, f"git not found: {exc}") from exc
        except PermissionError as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.WORKTREE_CREATED, f"git not executable: {exc}") from exc

        if result.returncode != 0:
            self._step = Step.FAILED
            raise IssueToPrError(
                Step.WORKTREE_CREATED,
                f"git push failed (exit {result.returncode}): {result.stderr.strip()}",
            )

        self._step = Step.BRANCH_PUSHED

    def _open_pr(self, branch_name: str) -> tuple[int, str]:
        """Create a PR via ``gh`` and return (pr_number, pr_url).

        The PR body always includes the never-merge safety marker and a
        ``Closes #N`` link when ``auto_link`` is enabled.

        TODO: prefer ``wh gh`` / GhSafe when available; never ``gh pr merge``
        or ``gh api`` merge endpoints.
        """
        body_parts: list[str] = []
        if self._cfg.auto_link:
            body_parts.append(f"Closes #{self._cfg.issue_number}")
        body_parts.append(_NEVER_MERGE_MARKER)

        title = f"Issue #{self._cfg.issue_number}: issue-to-PR workflow"
        body = "\n\n".join(body_parts)

        cmd = self._gh_pr_create_cmd(branch_name, title, body)
        # Never-merge: refuse merge subcommands (not labels/milestones named "merge").
        if _is_forbidden_merge_cmd(cmd):
            self._step = Step.FAILED
            raise IssueToPrError(Step.BRANCH_PUSHED, "merge commands are forbidden")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.BRANCH_PUSHED, f"gh pr create timed out: {exc}") from exc
        except FileNotFoundError as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.BRANCH_PUSHED, f"gh not found: {exc}") from exc
        except PermissionError as exc:
            self._step = Step.FAILED
            raise IssueToPrError(Step.BRANCH_PUSHED, f"gh not executable: {exc}") from exc

        if result.returncode != 0:
            self._step = Step.FAILED
            raise IssueToPrError(
                Step.BRANCH_PUSHED,
                f"gh pr create failed (exit {result.returncode}): {result.stderr.strip()}",
            )

        pr_url = ""
        for line in result.stdout.splitlines():
            line_stripped = line.strip()
            if "/pull/" in line_stripped:
                pr_url = line_stripped
                break
        if not pr_url:
            pr_url = result.stdout.strip()
        if not pr_url:
            self._step = Step.FAILED
            raise IssueToPrError(
                Step.BRANCH_PUSHED,
                "gh pr create returned empty output",
            )
        try:
            pr_number = self._extract_pr_number(pr_url)
        except IssueToPrError:
            self._step = Step.FAILED
            raise
        self._step = Step.PR_OPENED
        return pr_number, pr_url

    # -- helpers ------------------------------------------------------------

    def _branch_name(self) -> str:
        return f"feature/issue-{self._cfg.issue_number}"

    def _worktree_path(self) -> str:
        """Derive a sandboxed worktree path under WH_WORKTREE_BASE.

        Owner and repo segments are validated to reject ``..``, absolute
        components, and other escapes that would leave the worktree base.
        """
        _validate_path_segment("owner", self._cfg.owner)
        _validate_path_segment("repo", self._cfg.repo)
        base = _default_worktree_base()
        job_id = f"issue-{self._cfg.issue_number}"
        path = str(Path(base) / self._cfg.owner / self._cfg.repo / job_id)
        # Defense in depth: resolved path must remain under the base.
        try:
            base_resolved = Path(base).resolve(strict=False)
            path_resolved = Path(path).resolve(strict=False)
            path_resolved.relative_to(base_resolved)
        except ValueError as exc:
            raise IssueToPrError(
                Step.INIT,
                f"worktree path escapes base {base!r}: {path!r}",
            ) from exc
        return path

    def _gh_pr_create_cmd(self, branch_name: str, title: str, body: str) -> list[str]:
        _validate_branch_name("branch", branch_name)
        _validate_branch_name("base_branch", self._cfg.base_branch)
        gh = self._cfg.gh_path or "gh"
        cmd = [
            gh,
            "pr",
            "create",
            "--repo",
            f"{self._cfg.owner}/{self._cfg.repo}",
            "--head",
            branch_name,
            "--base",
            self._cfg.base_branch,
            "--title",
            title,
            "--body",
            body,
        ]
        for label in self._cfg.pr_labels:
            cmd.extend(["--label", label])
        if self._cfg.pr_milestone:
            cmd.extend(["--milestone", self._cfg.pr_milestone])
        return cmd

    @staticmethod
    def _extract_pr_number(pr_url: str) -> int:
        """Extract the PR number from a GitHub PR URL like ``https://github.com/owner/repo/pull/42``."""
        parts = pr_url.rstrip("/").split("/")
        try:
            return int(parts[-1])
        except (ValueError, IndexError) as exc:
            raise IssueToPrError(
                Step.BRANCH_PUSHED,
                f"Could not parse PR number from URL: {pr_url!r}",
            ) from exc

    @property
    def step(self) -> Step:
        """Return the current lifecycle step."""
        return self._step


def _validate_remote_name(remote: str) -> None:
    """Reject empty or option-looking remote names (e.g. ``--force``, ``-f``)."""
    if not remote or not _REMOTE_NAME_RE.fullmatch(remote) or remote.startswith("-"):
        raise IssueToPrError(
            Step.INIT,
            f"Invalid remote name {remote!r}: must be a plain remote identifier, "
            "not an option-looking value",
        )


def _validate_branch_name(field_name: str, value: str) -> None:
    """Reject empty or option-looking branch/ref names (e.g. ``--force``)."""
    if not value or not _BRANCH_NAME_RE.fullmatch(value) or value.startswith("-"):
        raise IssueToPrError(
            Step.INIT,
            f"Invalid {field_name} {value!r}: must be a plain git ref, "
            "not empty or option-looking (e.g. --force)",
        )


def _validate_path_segment(field_name: str, value: str) -> None:
    """Reject path-traversal and absolute components in owner/repo segments."""
    if not value or value in (".", ".."):
        raise IssueToPrError(Step.INIT, f"Invalid {field_name} segment: {value!r}")
    # Reject separators and drive-style absolute components.
    if "/" in value or "\\" in value or ":" in value:
        raise IssueToPrError(
            Step.INIT,
            f"Invalid {field_name} segment (contains separator): {value!r}",
        )
    if value.startswith("-"):
        raise IssueToPrError(
            Step.INIT,
            f"Invalid {field_name} segment (option-looking): {value!r}",
        )
    # Reject absolute or multi-component pure paths on either OS convention.
    for pure in (PurePosixPath(value), PureWindowsPath(value)):
        parts = pure.parts
        if len(parts) != 1 or parts[0] in (".", "..") or pure.is_absolute():
            raise IssueToPrError(Step.INIT, f"Invalid {field_name} segment: {value!r}")
