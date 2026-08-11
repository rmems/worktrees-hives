"""PR babysit cycle: monitor CI, resolve review threads, enforce fix cap.

Generalises the single-repo pr-babysit skill into a callable Python module
that can be driven by the hive orchestrator across multiple repos in parallel.

Safety invariants (from AGENTS.md):
  - Never merge a PR.
  - Max 3 code-fix commits per PR per cycle; thread replies are unlimited.
  - Force-push only with --force-with-lease.
  - Post review replies only after pushing, including SHA + attribution.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FIX_COMMITS_PER_CYCLE = 3
DEFAULT_ATTRIBUTION = "worktrees-hives agent"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
WH_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"

# Empty default — no org hardcoding. Configure via WH_ALLOWED_OWNERS or
# BabysitCycle(allowed_owners=...). Empty allowlist = deny mutations (fail closed).
ALLOWED_OWNERS: frozenset[str] = frozenset()


def load_allowed_owners_from_env() -> frozenset[str]:
    """Parse WH_ALLOWED_OWNERS (comma-separated). Empty/unset → empty set."""
    raw = os.environ.get(WH_ALLOWED_OWNERS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def assert_owner_allowed(
    owner: str,
    allowed_owners: frozenset[str] | None = None,
) -> frozenset[str]:
    """Require a non-empty owner allowlist and that *owner* is a member.

    Returns the effective allowlist. Fails closed when unconfigured so public
    mutation helpers cannot bypass repository-scope guardrails.
    """
    effective = (
        frozenset(allowed_owners) if allowed_owners is not None else load_allowed_owners_from_env()
    )
    if not effective:
        raise ValueError(
            f"Owner allowlist empty: set {WH_ALLOWED_OWNERS_ENV} or pass "
            "allowed_owners= before GitHub mutations."
        )
    if owner not in effective:
        raise ValueError(
            f"Owner '{owner}' not in allowed owners: {sorted(effective)}. "
            f"Set {WH_ALLOWED_OWNERS_ENV} or pass allowed_owners=."
        )
    return effective


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class PRState(Enum):
    """High-level PR health after a check cycle."""

    MERGED = "merged"
    CLOSED = "closed"
    HEALTHY = "healthy"
    PENDING_CI = "pending_ci"
    CI_FAILED = "ci_failed"
    CHANGES_REQUESTED = "changes_requested"
    UNRESOLVED_THREADS = "unresolved_threads"
    CONFLICTING = "conflicting"
    BEHIND = "behind"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    DRAFT = "draft"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


class ThreadAction(Enum):
    """Action taken on a review thread."""

    FIX_AND_REPLY = "fix_and_reply"
    REPLY_ONLY = "reply_only"
    SKIPPED = "skipped"


@dataclass
class CheckRun:
    """A single CI check run."""

    name: str
    state: str
    conclusion: str | None = None
    link: str | None = None

    @property
    def is_failure(self) -> bool:
        conclusion = (self.conclusion or "").upper()
        return conclusion in (
            "FAILURE",
            "ERROR",
            "ACTION_REQUIRED",
            "TIMED_OUT",
            "CANCELLED",
            "STARTUP_FAILURE",
        )

    @property
    def is_pending(self) -> bool:
        return self.state in (
            "IN_PROGRESS",
            "QUEUED",
            "PENDING",
            "WAITING",
            "EXPECTED",
            "REQUESTED",
            "STALE",
        )

    @property
    def is_success(self) -> bool:
        return self.conclusion == "SUCCESS"


@dataclass
class ReviewThread:
    """An unresolved review thread on a PR."""

    thread_id: str
    comments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def first_comment(self) -> dict[str, Any] | None:
        return self.comments[0] if self.comments else None

    @property
    def path(self) -> str | None:
        c = self.first_comment
        return c.get("path") if c else None

    @property
    def line(self) -> int | None:
        c = self.first_comment
        return c.get("line") if c else None

    @property
    def body(self) -> str:
        c = self.first_comment
        return c.get("body", "") if c else ""

    @property
    def combined_body(self) -> str:
        """Concatenate all comment bodies for actionability checks."""
        parts = [str(c.get("body", "")) for c in self.comments]
        return "\n".join(parts)

    @property
    def database_id(self) -> int | None:
        c = self.first_comment
        return c.get("databaseId") if c else None

    @property
    def author_login(self) -> str:
        c = self.first_comment
        if c and c.get("author"):
            author = c["author"]
            if isinstance(author, dict):
                login = author.get("login", "")
                return str(login) if login is not None else ""
        return ""


@dataclass
class BabysitResult:
    """Result of one babysit cycle on a single PR."""

    pr_number: int
    state: PRState
    fix_commits_used: int = 0
    threads_resolved: int = 0
    threads_remaining: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    checks_pending: int = 0
    residual_blockers: list[str] = field(default_factory=list)

    @property
    def is_merge_ready(self) -> bool:
        return self.state == PRState.HEALTHY

    def summary(self) -> str:
        lines = [
            f"PR #{self.pr_number}: {self.state.value}",
            f"  Fixes: {self.fix_commits_used}/{MAX_FIX_COMMITS_PER_CYCLE}",
            f"  Threads resolved: {self.threads_resolved}, remaining: {self.threads_remaining}",
            f"  Checks: {self.checks_passed} passed, {self.checks_failed} failed, "
            f"{self.checks_pending} pending",
        ]
        if self.residual_blockers:
            lines.append("  Residual blockers:")
            for b in self.residual_blockers:
                lines.append(f"    - {b}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _run_gh(
    args: list[str],
    *,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command and return the result."""
    env = {"NO_COLOR": "1"}
    full_env = {**os.environ, **env}
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
        env=full_env,
        timeout=timeout,
    )


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI_RE.sub("", text)


def _graphql_query(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Execute a GraphQL query via gh api graphql.

    Raises ValueError when the payload cannot be parsed or when GitHub returns
    a top-level ``errors`` array (permission, rate-limit, stale thread, etc.).
    """
    args = ["api", "graphql"]
    for k, v in variables.items():
        if isinstance(v, int):
            args.extend(["-F", f"{k}={v}"])
        else:
            args.extend(["-f", f"{k}={v}"])
    args.extend(["-f", f"query={query}"])
    result = _run_gh(args)
    raw = _strip_ansi(result.stdout)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse GraphQL response: {e}") from e
    errors = data.get("errors") if isinstance(data, dict) else None
    if errors:
        if isinstance(errors, list):
            messages = "; ".join(
                str(err.get("message", err)) if isinstance(err, dict) else str(err)
                for err in errors
            )
        else:
            messages = str(errors)
        raise ValueError(f"GraphQL errors: {messages}")
    if not isinstance(data, dict):
        raise ValueError("GraphQL response must be a JSON object")
    return data


# ---------------------------------------------------------------------------
# PR status fetching
# ---------------------------------------------------------------------------


def fetch_pr_status(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Fetch PR metadata via gh pr view."""
    result = _run_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "state,mergeable,mergeStateStatus,statusCheckRollup,"
            "reviewDecision,headRefName,baseRefName,headRefOid,isDraft",
        ]
    )
    try:
        parsed: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse PR status response: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("PR status response must be a JSON object")
    return parsed


def fetch_pr_checks(owner: str, repo: str, number: int) -> list[CheckRun]:
    """Fetch CI check runs for a PR.

    Empty stdout is only treated as "no checks" when gh exits 0 or reports
    that no checks are configured. Nonzero exit with empty/unparseable output
    is treated as a fetch failure so babysit cannot misclassify as HEALTHY.

    ``subprocess.TimeoutExpired`` is converted to ``ValueError`` so callers can
    treat stalled CI queries as residual blockers rather than aborting.
    """
    try:
        result = _run_gh(
            [
                "pr",
                "checks",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                "--required",
                "--json",
                "name,state,bucket,link",
            ],
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError(f"Timed out fetching PR checks for {owner}/{repo}#{number}") from e
    raw = _strip_ansi(result.stdout)
    stderr = _strip_ansi(result.stderr or "")
    combined_lower = f"{raw}\n{stderr}".lower()
    no_ci_markers = (
        "no checks reported",
        "no checks",
        "without checks",
        "has no checks",
    )
    if not raw.strip():
        if result.returncode == 0 or any(m in combined_lower for m in no_ci_markers):
            return []
        detail = stderr.strip() or f"gh pr checks exited {result.returncode}"
        raise ValueError(f"Failed to fetch PR checks for {owner}/{repo}#{number}: {detail}")
    if any(m in combined_lower for m in no_ci_markers) and result.returncode != 0:
        return []
    # Non-JSON "no checks" text can appear with exit 0/1 before JSON exporter.
    if any(m in combined_lower for m in no_ci_markers) and not raw.strip().startswith("["):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        if any(m in combined_lower for m in no_ci_markers):
            return []
        if result.returncode != 0:
            detail = stderr.strip() or str(e)
            raise ValueError(
                f"Failed to fetch PR checks for {owner}/{repo}#{number}: {detail}"
            ) from e
        raise ValueError(f"Failed to parse PR checks response: {e}") from e
    if not isinstance(data, list):
        raise ValueError("PR checks response must be a JSON array")
    terminal_conclusions = {
        "SUCCESS",
        "FAILURE",
        "ERROR",
        "ACTION_REQUIRED",
        "CANCELLED",
        "SKIPPED",
        "TIMED_OUT",
        "STARTUP_FAILURE",
    }
    # gh may report bucket=fail/pass/pending instead of conclusion.
    bucket_map = {
        "pass": "SUCCESS",
        "fail": "FAILURE",
        "pending": None,
        "skipping": "SKIPPED",
        "cancel": "CANCELLED",
    }
    checks: list[CheckRun] = []
    for c in data:
        if not isinstance(c, dict):
            continue
        state = str(c.get("state") or "")
        bucket = str(c.get("bucket") or "").lower()
        conclusion: str | None
        if state.upper() in terminal_conclusions:
            conclusion = state.upper()
        elif bucket in bucket_map:
            conclusion = bucket_map[bucket]
        else:
            conclusion = None
        checks.append(
            CheckRun(
                name=str(c.get("name") or ""),
                state=state or (bucket.upper() if bucket else ""),
                conclusion=conclusion,
                link=c.get("link"),
            )
        )
    return checks


def fetch_review_threads(owner: str, repo: str, number: int) -> list[ReviewThread]:
    """Fetch all unresolved review threads via paginated GraphQL.

    Comments are requested with first:50 (vs first:10) to reduce truncation of
    long review threads within a single page.
    """
    query = """
    query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: 50, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              isResolved
              id
              comments(first: 50) {
                nodes {
                  author { login }
                  path
                  line
                  body
                  databaseId
                  url
                }
              }
            }
          }
        }
      }
    }
    """
    threads: list[ReviewThread] = []
    cursor: str | None = None

    while True:
        variables: dict[str, Any] = {"owner": owner, "name": repo, "number": number}
        if cursor:
            variables["cursor"] = cursor

        data = _graphql_query(query, variables)
        try:
            pr_data = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        except KeyError as e:
            raise ValueError(f"Unexpected GraphQL response structure: missing {e}") from e
        nodes = pr_data["nodes"]

        for node in nodes:
            if not node["isResolved"]:
                comments = [
                    {
                        "author": c.get("author", {}),
                        "path": c.get("path"),
                        "line": c.get("line"),
                        "body": c.get("body", ""),
                        "databaseId": c.get("databaseId"),
                        "url": c.get("url"),
                    }
                    for c in node["comments"]["nodes"]
                ]
                threads.append(ReviewThread(thread_id=node["id"], comments=comments))

        page_info = pr_data["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    return threads


# ---------------------------------------------------------------------------
# Thread reply and resolution
# ---------------------------------------------------------------------------


def reply_to_thread(
    owner: str,
    repo: str,
    number: int,
    thread: ReviewThread,
    body: str,
    *,
    allowed_owners: frozenset[str] | None = None,
) -> None:
    """Post a reply to a review thread comment (owner allowlist enforced)."""
    assert_owner_allowed(owner, allowed_owners)
    db_id = thread.database_id
    if db_id is None:
        return
    _run_gh(
        [
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/comments/{db_id}/replies",
            "-X",
            "POST",
            "-f",
            f"body={body}",
        ]
    )


def resolve_thread(
    thread_id: str,
    *,
    owner: str,
    allowed_owners: frozenset[str] | None = None,
) -> None:
    """Resolve a review thread via GraphQL mutation (owner allowlist enforced)."""
    assert_owner_allowed(owner, allowed_owners)
    mutation = """
    mutation($threadId: ID!) {
      resolveReviewThread(input: {threadId: $threadId}) {
        thread { isResolved }
      }
    }
    """
    _graphql_query(mutation, {"threadId": thread_id})


def post_pr_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    allowed_owners: frozenset[str] | None = None,
) -> None:
    """Post a general comment on a PR (owner allowlist enforced)."""
    assert_owner_allowed(owner, allowed_owners)
    _run_gh(
        [
            "pr",
            "comment",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--body",
            body,
        ]
    )


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------


def classify_pr(pr_data: dict[str, Any]) -> PRState:
    """Determine the high-level state of a PR from gh pr view data.

    BEHIND is distinct from CONFLICTING: a branch that is behind base does not
    imply merge conflicts. BLOCKED is distinct from UNRESOLVED_THREADS (branch
    protection/ruleset vs conversation threads).
    """
    state = pr_data.get("state", "")
    if state == "MERGED":
        return PRState.MERGED
    if state == "CLOSED":
        return PRState.CLOSED

    mergeable = pr_data.get("mergeable", "")
    merge_state = pr_data.get("mergeStateStatus", "")
    review_decision = pr_data.get("reviewDecision")

    if pr_data.get("isDraft"):
        return PRState.DRAFT

    # Conflicts only — BEHIND is handled separately
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return PRState.CONFLICTING

    # GitHub still computing the test merge — not ready / not healthy.
    if mergeable == "UNKNOWN":
        return PRState.PENDING_CI

    if merge_state == "BEHIND":
        return PRState.BEHIND

    checks = pr_data.get("statusCheckRollup", [])
    has_failure = any(
        (c.get("conclusion") or "").upper()
        in (
            "FAILURE",
            "ERROR",
            "ACTION_REQUIRED",
            "CANCELLED",
            "TIMED_OUT",
            "STARTUP_FAILURE",
        )
        or (c.get("state") or "").upper() in ("FAILURE", "ERROR", "CANCELLED", "STARTUP_FAILURE")
        for c in checks
        if isinstance(c, dict)
    )
    # Keep rollup pending states aligned with CheckRun.is_pending so required
    # external statuses (EXPECTED/REQUESTED/STALE) are not treated as HEALTHY.
    _pending_states = (
        "IN_PROGRESS",
        "QUEUED",
        "PENDING",
        "WAITING",
        "EXPECTED",
        "REQUESTED",
        "STALE",
    )
    has_pending = any(
        c.get("state") in _pending_states or c.get("status") in _pending_states
        for c in checks
        if isinstance(c, dict)
    )

    if has_failure:
        return PRState.CI_FAILED
    if review_decision == "CHANGES_REQUESTED":
        return PRState.CHANGES_REQUESTED
    if review_decision == "REVIEW_REQUIRED":
        return PRState.REVIEW_REQUIRED
    if has_pending:
        return PRState.PENDING_CI
    # Branch protection/ruleset can report BLOCKED without failed checks.
    if merge_state == "BLOCKED":
        return PRState.BLOCKED
    # UNSTABLE = mergeable but non-passing commit status (GitHub docs).
    if merge_state == "UNSTABLE":
        return PRState.CI_FAILED

    return PRState.HEALTHY


# ---------------------------------------------------------------------------
# Babysit cycle
# ---------------------------------------------------------------------------


@dataclass
class BabysitCycle:
    """Orchestrates one babysit cycle for a single PR.

    Usage::

        cycle = BabysitCycle(owner="acme", repo="my-repo", pr_number=42)
        result = cycle.run()
        print(result.summary())

    Owner allowlist (fail closed):
      - ``allowed_owners`` constructor arg, or
      - ``WH_ALLOWED_OWNERS`` env (comma-separated)
      - empty allowlist → construction fails (no mutations without scope)
    """

    owner: str
    repo: str
    pr_number: int
    attribution: str = DEFAULT_ATTRIBUTION
    max_fixes: int = MAX_FIX_COMMITS_PER_CYCLE
    # Optional worker that applies a code fix for a thread and returns the
    # pushed HEAD SHA (or multiple SHAs as a sequence). Without a successful
    # return value, FIX_AND_REPLY must not claim "Addressed" or resolve.
    fix_handler: Callable[[ReviewThread], str | list[str] | None] | None = None
    # None → load from WH_ALLOWED_OWNERS (must be non-empty).
    allowed_owners: frozenset[str] | None = None

    # mutable state — count unique pushed SHAs (code-fix commits), not threads.
    _fix_shas: set[str] = field(default_factory=set, init=False, repr=False)
    _effective_allowed_owners: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False
    )

    @property
    def _fixes_used(self) -> int:
        return len(self._fix_shas)

    def __post_init__(self) -> None:
        self._effective_allowed_owners = assert_owner_allowed(self.owner, self.allowed_owners)
        if self.max_fixes > MAX_FIX_COMMITS_PER_CYCLE:
            raise ValueError(
                f"max_fixes ({self.max_fixes}) exceeds safety ceiling "
                f"({MAX_FIX_COMMITS_PER_CYCLE}). Per AGENTS.md, at most "
                f"{MAX_FIX_COMMITS_PER_CYCLE} code-fix commits per PR per cycle."
            )
        if self.max_fixes < 0:
            raise ValueError("max_fixes must be non-negative")

    def run(self) -> BabysitResult:
        """Execute one full check-and-fix cycle."""
        pr_data = fetch_pr_status(self.owner, self.repo, self.pr_number)
        state = classify_pr(pr_data)

        if state in (PRState.MERGED, PRState.CLOSED):
            return BabysitResult(
                pr_number=self.pr_number,
                state=state,
                residual_blockers=[f"PR is {state}"],
            )

        result = BabysitResult(pr_number=self.pr_number, state=state)

        # Step 1: Handle conflicts, draft, review-required, behind, blocked
        if state == PRState.CONFLICTING:
            result.residual_blockers.append("Merge conflicts must be resolved manually")
            return result
        if state == PRState.DRAFT:
            result.residual_blockers.append("PR is a draft; mark as ready for review first")
            return result
        if state == PRState.REVIEW_REQUIRED:
            result.residual_blockers.append("PR requires a review approval before merge")

        if state == PRState.BEHIND:
            # Behind ≠ conflicts; still process CI/threads, keep residual.
            result.residual_blockers.append(
                "Branch is behind base; update/rebase before merge (not a conflict)"
            )

        merge_state = pr_data.get("mergeStateStatus", "")
        if merge_state == "BLOCKED" or state == PRState.BLOCKED:
            # Separate message from unresolved conversation threads.
            result.residual_blockers.append(
                "GitHub merge state is BLOCKED (branch protection/ruleset; "
                "not unresolved review threads)"
            )

        # Step 2: Process CI failures
        try:
            checks = fetch_pr_checks(self.owner, self.repo, self.pr_number)
        except (ValueError, subprocess.TimeoutExpired) as e:
            result.residual_blockers.append(f"CI check fetch failed: {e}")
            result.state = PRState.CI_FAILED
            result.fix_commits_used = self._fixes_used
            return result

        result.checks_passed = sum(1 for c in checks if c.is_success)
        result.checks_failed = sum(1 for c in checks if c.is_failure)
        result.checks_pending = sum(1 for c in checks if c.is_pending)

        failed_checks = [c for c in checks if c.is_failure]
        for check in failed_checks:
            label = "CI cancelled" if check.conclusion == "CANCELLED" else "CI failure"
            result.residual_blockers.append(f"{label}: {check.name}")

        # Step 3: Process unresolved review threads
        try:
            threads = fetch_review_threads(self.owner, self.repo, self.pr_number)
        except ValueError as e:
            result.residual_blockers.append(f"Review thread fetch failed: {e}")
            result.fix_commits_used = self._fixes_used
            if result.checks_failed > 0:
                result.state = PRState.CI_FAILED
            elif result.checks_pending > 0:
                result.state = PRState.PENDING_CI
            else:
                result.state = PRState.UNRESOLVED_THREADS
            return result
        result.threads_remaining = len(threads)

        allow = self._effective_allowed_owners
        hit_cap = False
        for thread in threads:
            action = self._decide_thread_action(thread)
            if action == ThreadAction.FIX_AND_REPLY:
                pushed_shas = self._apply_fix(thread)
                if not pushed_shas:
                    # No worker/push occurred — do not claim addressed or resolve.
                    reply_body = (
                        f"Actionable review noted; no fix worker applied a push for "
                        f"this cycle. Leaving unresolved until a real fix lands. "
                        f"-- {self.attribution}"
                    )
                    try:
                        reply_to_thread(
                            self.owner,
                            self.repo,
                            self.pr_number,
                            thread,
                            reply_body,
                            allowed_owners=allow,
                        )
                    except (
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                        ValueError,
                    ) as e:
                        result.residual_blockers.append(
                            f"Reply failed for thread on {thread.path}:{thread.line}: {e}"
                        )
                    result.residual_blockers.append(
                        f"Thread on {thread.path}:{thread.line} needs a real code fix"
                    )
                    continue
                for sha in pushed_shas:
                    self._fix_shas.add(sha)
                primary = pushed_shas[-1]
                # Prefer live headRefOid when it matches a reported push SHA.
                try:
                    fresh = fetch_pr_status(self.owner, self.repo, self.pr_number)
                    head = str(fresh.get("headRefOid") or "")
                    if head:
                        for s in pushed_shas:
                            if head.startswith(s) or s.startswith(head[: max(7, min(len(s), 12))]):
                                primary = head[:8]
                                self._fix_shas.add(head)
                                break
                except (ValueError, subprocess.TimeoutExpired):
                    pass
                reply_body = f"Addressed in {primary}: {self.attribution}"
                try:
                    reply_to_thread(
                        self.owner,
                        self.repo,
                        self.pr_number,
                        thread,
                        reply_body,
                        allowed_owners=allow,
                    )
                    resolve_thread(
                        thread.thread_id,
                        owner=self.owner,
                        allowed_owners=allow,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
                    result.residual_blockers.append(
                        f"Post-fix GitHub mutation failed on {thread.path}:{thread.line} "
                        f"(pushed {primary}): {e}"
                    )
                    continue
                result.threads_resolved += 1
                result.threads_remaining -= 1
            elif action == ThreadAction.REPLY_ONLY:
                hit_cap = True
                reply_body = self._draft_substantive_reply()
                try:
                    reply_to_thread(
                        self.owner,
                        self.repo,
                        self.pr_number,
                        thread,
                        reply_body,
                        allowed_owners=allow,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
                    result.residual_blockers.append(
                        f"Cap reply failed for thread on {thread.path}:{thread.line}: {e}"
                    )
                # Do not resolve -- feedback is still pending
                result.residual_blockers.append(
                    f"Thread on {thread.path}:{thread.line} replied but not resolved"
                )
            # SKIPPED: leave unresolved

        # After any pushed fix, re-fetch status/checks for the new head before
        # declaring HEALTHY (old green checks are for the pre-fix SHA).
        mergeable = pr_data.get("mergeable", "")
        if self._fixes_used > 0:
            try:
                pr_data = fetch_pr_status(self.owner, self.repo, self.pr_number)
                state = classify_pr(pr_data)
                merge_state = pr_data.get("mergeStateStatus", "")
                mergeable = pr_data.get("mergeable", "")
                checks = fetch_pr_checks(self.owner, self.repo, self.pr_number)
                result.checks_passed = sum(1 for c in checks if c.is_success)
                result.checks_failed = sum(1 for c in checks if c.is_failure)
                result.checks_pending = sum(1 for c in checks if c.is_pending)
                for check in checks:
                    if check.is_failure:
                        label = (
                            "CI cancelled"
                            if (check.conclusion or "").upper() == "CANCELLED"
                            else "CI failure"
                        )
                        msg = f"{label}: {check.name}"
                        if msg not in result.residual_blockers:
                            result.residual_blockers.append(msg)
            except (ValueError, subprocess.TimeoutExpired) as e:
                result.residual_blockers.append(f"CI re-check after fix failed: {e}")
                result.checks_failed = max(result.checks_failed, 1)

        # Cap residual: post aggregate PR comment when budget exhausted.
        if hit_cap or self._fixes_used >= self.max_fixes:
            residual_report = (
                f"## Babysit residual (fix cap {self.max_fixes})\n\n"
                f"- Fix commits this cycle: {self._fixes_used}\n"
                f"- Threads remaining: {result.threads_remaining}\n"
                f"- Checks failed: {result.checks_failed}, pending: {result.checks_pending}\n"
            )
            if result.residual_blockers:
                residual_report += "\n### Blockers\n" + "\n".join(
                    f"- {b}" for b in result.residual_blockers
                )
            residual_report += f"\n\n-- {self.attribution}"
            try:
                post_pr_comment(
                    self.owner,
                    self.repo,
                    self.pr_number,
                    residual_report,
                    allowed_owners=allow,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
                result.residual_blockers.append(f"Failed to post residual cap summary: {e}")

        # Final state assessment -- preserve higher-severity states (incl. rollup)
        result.fix_commits_used = self._fixes_used
        if state == PRState.CHANGES_REQUESTED:
            result.state = PRState.CHANGES_REQUESTED
        elif state == PRState.REVIEW_REQUIRED:
            result.state = PRState.REVIEW_REQUIRED
        elif state == PRState.CONFLICTING or merge_state == "DIRTY" or mergeable == "CONFLICTING":
            result.state = PRState.CONFLICTING
        elif state == PRState.CI_FAILED or result.checks_failed > 0:
            result.state = PRState.CI_FAILED
        elif state == PRState.PENDING_CI or result.checks_pending > 0:
            result.state = PRState.PENDING_CI
        elif merge_state == "UNSTABLE":
            # Mergeable but non-passing status with no discrete check rows.
            result.state = PRState.CI_FAILED
        elif state == PRState.BEHIND or merge_state == "BEHIND":
            result.state = PRState.BEHIND
        elif state == PRState.BLOCKED or merge_state == "BLOCKED":
            result.state = PRState.BLOCKED
        elif result.threads_remaining > 0:
            result.state = PRState.UNRESOLVED_THREADS
        else:
            result.state = PRState.HEALTHY

        return result

    def _decide_thread_action(self, thread: ReviewThread) -> ThreadAction:
        """Decide what to do with an unresolved review thread."""
        if not self._is_actionable(thread):
            return ThreadAction.SKIPPED
        if self._fixes_used >= self.max_fixes:
            return ThreadAction.REPLY_ONLY
        return ThreadAction.FIX_AND_REPLY

    @staticmethod
    def _is_actionable(thread: ReviewThread) -> bool:
        """Check if any comment in the thread describes a code-fixable issue.

        Inspects the full thread (not only the first comment) so follow-up
        requests like "please update this" are not missed after an initial
        question.
        """
        body = thread.combined_body.lower()
        actionable_signals = [
            "fix",
            "change",
            "update",
            "remove",
            "add",
            "rename",
            "should",
            "must",
            "please",
            "typo",
            "bug",
            "error",
            "missing",
            "incorrect",
            "wrong",
        ]
        return any(signal in body for signal in actionable_signals)

    def _draft_substantive_reply(self) -> str:
        """Draft a substantive reply when fix budget is exhausted."""
        return (
            f"Thank you for the review. The fix budget for this cycle "
            f"({self.max_fixes} commits) has been reached. "
            f"This feedback will be addressed in the next cycle. "
            f"-- {self.attribution}"
        )

    def _apply_fix(self, thread: ReviewThread) -> list[str]:
        """Invoke the optional fix worker.

        Returns a list of new commit SHAs pushed (possibly empty). Counts
        unique SHAs toward the fix cap, not threads resolved.
        """
        if self.fix_handler is None:
            return []
        try:
            raw = self.fix_handler(thread)
        except Exception:
            # Catch ordinary fix-handler failures (subprocess, network, validation,
            # etc.) so one broken handler does not abort the cycle. BaseException
            # subclasses such as KeyboardInterrupt remain uncaught.
            return []
        if not raw:
            return []
        candidates = [raw] if isinstance(raw, str) else list(raw)
        shas: list[str] = []
        for item in candidates:
            s = str(item).strip()
            if s:
                shas.append(s)
        return shas

    @staticmethod
    def _get_head_sha(pr_data: dict[str, Any]) -> str:
        """Get the HEAD SHA of the PR branch."""
        sha = pr_data.get("headRefOid")
        if isinstance(sha, str) and sha:
            return sha[:8]
        head = pr_data.get("headRefName", "HEAD")
        return str(head) if head is not None else "HEAD"


# ---------------------------------------------------------------------------
# Multi-PR orchestrator helper
# ---------------------------------------------------------------------------


# Stack base blocked states — defer later PRs (bottom-up stack order).
_STACK_BLOCKING_STATES = frozenset(
    {
        PRState.CI_FAILED,
        PRState.CONFLICTING,
        PRState.BEHIND,
        PRState.BLOCKED,
        PRState.DRAFT,
    }
)


def babysit_multiple(
    owner: str,
    repo: str,
    pr_numbers: list[int],
    attribution: str = DEFAULT_ATTRIBUTION,
    fix_handler: Callable[[ReviewThread], str | list[str] | None] | None = None,
    max_fixes: int = MAX_FIX_COMMITS_PER_CYCLE,
    allowed_owners: frozenset[str] | None = None,
) -> list[BabysitResult]:
    """Run babysit cycles on multiple PRs in the same repo.

    Expects *pr_numbers* in bottom-up stack order when stacking. If an earlier
    PR is blocked (CI/conflicts/behind/protection), later children are deferred
    without mutation.

    Per-PR exceptions are caught so one failure does not abort the rest;
    failed PRs return PRState.UNKNOWN with the error in residual_blockers.
    """
    results: list[BabysitResult] = []
    base_blocked_by: int | None = None
    for num in pr_numbers:
        if base_blocked_by is not None:
            results.append(
                BabysitResult(
                    pr_number=num,
                    state=PRState.UNKNOWN,
                    residual_blockers=[
                        f"Deferred: earlier stacked PR #{base_blocked_by} is blocked"
                    ],
                )
            )
            continue
        try:
            cycle = BabysitCycle(
                owner=owner,
                repo=repo,
                pr_number=num,
                attribution=attribution,
                fix_handler=fix_handler,
                max_fixes=max_fixes,
                allowed_owners=allowed_owners,
            )
            result = cycle.run()
            results.append(result)
            if result.state in _STACK_BLOCKING_STATES:
                base_blocked_by = num
        except Exception as e:
            results.append(
                BabysitResult(
                    pr_number=num,
                    state=PRState.UNKNOWN,
                    residual_blockers=[f"babysit cycle failed: {e}"],
                )
            )
            base_blocked_by = num
    return results
