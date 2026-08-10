"""Discovery module for finding eligible work across allowed GitHub owners.

**Layering:** GitHub reads go through ``wh gh-safe`` (Rust ``SafeGhCommand``),
not a raw ``gh`` subprocess. Policy, parsing, and owner allowlists stay in
Python; path/branch/merge safety and allowlisted ``gh`` invocation stay in Rust.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Literal

from worktrees_hives.bridge import WhClient
from worktrees_hives.contract import ErrorResponse, SuccessResponse
from worktrees_hives.errors import (
    PolicyError,
    WhBinaryNotFoundError,
    WhError,
    WhProcessError,
)

# Soft per-resource fetch cap. gh list commands paginate internally up to --limit.
# When a response hits this cap, DiscoveryResult.truncated is set True (no silent drop).
DISCOVERY_ITEM_LIMIT = 1000

# Env var: comma-separated owner allowlist. Empty/unset = empty default (no org hardcoding).
WH_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"

# Discovery list calls can be slow against large orgs; bridge default is 30s.
_DISCOVERY_WH_TIMEOUT = 60.0

DiscoveryKind = Literal["all", "issues", "prs"]

logger = logging.getLogger(__name__)

# Shared client for process-local discovery (tests may monkeypatch _run_gh).
_default_wh_client: WhClient | None = None


def _wh_client() -> WhClient:
    global _default_wh_client
    if _default_wh_client is None:
        _default_wh_client = WhClient(timeout=_DISCOVERY_WH_TIMEOUT)
    return _default_wh_client


class IssueState(StrEnum):
    """State of a GitHub issue or PR.

    GitHub's issue/PR list APIs only support open, closed, and all.
    An "in_progress" workflow state is not a first-class GitHub issue state.
    """

    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class Issue:
    """Represents a GitHub issue or PR."""

    number: int
    title: str
    state: str
    labels: list[str]
    milestone: str | None
    url: str
    owner: str
    repo: str
    is_pr: bool
    assignees: list[str]
    created_at: str
    updated_at: str
    ci_status: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """Result of discovering issues across allowed owners.

    truncated is True when any list call returned a full page at DISCOVERY_ITEM_LIMIT,
    meaning further items may exist. Callers should treat results as incomplete.
    """

    issues: list[Issue]
    errors: list[str]
    owners_scanned: list[str]
    truncated: bool = False


# Empty default — product must not hardcode org owners. Use WH_ALLOWED_OWNERS or
# an explicit owners= argument for non-empty scans.
ALLOWED_OWNERS: list[str] = []
ALLOWED_OWNERS_SET: frozenset[str] = frozenset()


class OwnerPolicyError(ValueError):
    """Raised when discovery is asked to scan a non-allowlisted owner without override."""


def load_allowed_owners_from_env() -> list[str]:
    """Parse WH_ALLOWED_OWNERS (comma-separated). Empty/unset → empty list."""
    raw = os.environ.get(WH_ALLOWED_OWNERS_ENV, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _run_gh(args: list[str], *, client: WhClient | None = None) -> tuple[str, str, int]:
    """Run ``gh`` via ``wh gh-safe`` and return child stdout, stderr, exit code.

    Never spawns ``gh`` directly — that would bypass Rust ``SafeGhCommand``
    (merge deny-list, allowlisted subcommands, blocked flags).
    """
    wh = client if client is not None else _wh_client()
    try:
        resp = wh.gh_safe(*args)
    except WhBinaryNotFoundError as exc:
        return "", str(exc), 1
    except PolicyError as exc:
        return "", f"Policy violation [{exc.code}]: {exc.message}", 2
    except WhProcessError as exc:
        detail = (exc.stderr or str(exc)).strip() or "wh gh-safe failed"
        code = exc.returncode if isinstance(exc.returncode, int) and exc.returncode > 0 else 1
        return "", detail, code
    except WhError as exc:
        return "", str(exc), 1

    if isinstance(resp, ErrorResponse):
        return "", resp.error.message, 1
    if not isinstance(resp, SuccessResponse):
        return "", "unexpected wh response type", 1

    data = resp.data
    stdout = data.get("stdout", "")
    stderr = data.get("stderr", "")
    if not isinstance(stdout, str):
        stdout = str(stdout) if stdout is not None else ""
    if not isinstance(stderr, str):
        stderr = str(stderr) if stderr is not None else ""
    exit_code = data.get("exit_code", 0)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = 1
    return stdout, stderr, exit_code


def ensure_gh_auth() -> str | None:
    """Fail fast when gh authentication is missing or broken.

    Returns an error message when auth is unusable, else None.
    """
    stdout, stderr, returncode = _run_gh(["auth", "status"])
    if returncode == 0:
        return None
    detail = (stderr or stdout or "gh auth status failed").strip()
    return (
        "GitHub CLI authentication failed. Run `gh auth login` (or refresh the token) "
        f"before discovery. Detail: {detail}"
    )


def _summarize_ci_rollup(rollup: Any) -> str | None:
    """Reduce statusCheckRollup to success|failure|pending|neutral|unknown."""
    if rollup is None:
        return None
    if not isinstance(rollup, list) or not rollup:
        return "unknown"

    has_failure = False
    has_pending = False
    has_success = False
    for item in rollup:
        if not isinstance(item, dict):
            continue
        conclusion = (item.get("conclusion") or "").upper()
        state = (item.get("state") or item.get("status") or "").upper()
        if conclusion in (
            "FAILURE",
            "ERROR",
            "ACTION_REQUIRED",
            "TIMED_OUT",
            "CANCELLED",
        ) or state in (
            "FAILURE",
            "ERROR",
            "CANCELLED",
        ):
            has_failure = True
        elif state in (
            "IN_PROGRESS",
            "QUEUED",
            "PENDING",
            "WAITING",
            "EXPECTED",
            "REQUESTED",
        ) or (conclusion in ("", "NONE") and state not in ("SUCCESS", "COMPLETED", "SKIPPED")):
            # Pending / incomplete check
            if conclusion not in ("SUCCESS", "SKIPPED", "NEUTRAL"):
                has_pending = True
        elif conclusion == "SUCCESS" or state == "SUCCESS":
            has_success = True

    if has_failure:
        return "failure"
    if has_pending:
        return "pending"
    if has_success:
        return "success"
    return "neutral"


def _extract_label_names(data: dict[str, Any]) -> list[str]:
    """Soft-extract label names; skip malformed entries."""
    labels = data.get("labels") or []
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and "name" in label:
            names.append(str(label["name"]))
        elif isinstance(label, str):
            names.append(label)
    return names


def _extract_assignee_logins(data: dict[str, Any]) -> list[str]:
    """Soft-extract assignee logins; skip malformed entries."""
    assignees = data.get("assignees") or []
    if not isinstance(assignees, list):
        return []
    logins: list[str] = []
    for assignee in assignees:
        if isinstance(assignee, dict) and "login" in assignee:
            logins.append(str(assignee["login"]))
        elif isinstance(assignee, str):
            logins.append(assignee)
    return logins


def _parse_issue(data: dict[str, Any], owner: str, repo: str) -> Issue:
    """Parse a GitHub issue/PR JSON response into an Issue object.

    Label/assignee extraction is soft; required field failures raise ValueError.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Issue data must be a dict, got {type(data).__name__}")

    labels = _extract_label_names(data)
    milestone_data = data.get("milestone")
    milestone: str | None
    if isinstance(milestone_data, dict):
        title = milestone_data.get("title")
        milestone = str(title) if title is not None else None
    else:
        milestone = None
    assignees = _extract_assignee_logins(data)
    ci_status = _summarize_ci_rollup(data.get("statusCheckRollup"))

    try:
        number = data["number"]
        title = data["title"]
        state_raw = data["state"]
    except KeyError as e:
        raise ValueError(f"Missing required field in issue data: {e}") from e

    return Issue(
        number=int(number),
        title=str(title),
        state=str(state_raw).lower(),
        labels=labels,
        milestone=milestone,
        url=str(data.get("url") or data.get("html_url") or ""),
        owner=owner,
        repo=repo,
        is_pr="pullRequest" in data or data.get("pull_request") is not None,
        assignees=assignees,
        created_at=str(data["createdAt"] if "createdAt" in data else data.get("created_at", "")),
        updated_at=str(data["updatedAt"] if "updatedAt" in data else data.get("updated_at", "")),
        ci_status=ci_status,
    )


def _soft_parse_items(
    data: list[Any],
    owner: str,
    repo: str,
    *,
    force_pr: bool = False,
) -> tuple[list[Issue], list[str]]:
    """Parse list items one-by-one; one bad item does not abort the repo scan."""
    issues: list[Issue] = []
    errors: list[str] = []
    for idx, item in enumerate(data):
        try:
            if not isinstance(item, dict):
                raise ValueError(f"expected object, got {type(item).__name__}")
            issue = _parse_issue(item, owner, repo)
            if force_pr:
                issue = replace(issue, is_pr=True)
            issues.append(issue)
        except (ValueError, TypeError, KeyError) as e:
            msg = f"Skipped malformed item #{idx} in {owner}/{repo}: {e}"
            errors.append(msg)
            logger.warning(msg)
    return issues, errors


def _hit_item_limit(data: list[Any]) -> bool:
    """True when response length meets the soft fetch cap (possible silent truncation)."""
    return len(data) >= DISCOVERY_ITEM_LIMIT


def discover_issues_for_repo(
    owner: str,
    repo: str,
    state: IssueState = IssueState.OPEN,
) -> tuple[list[Issue], str | None, bool]:
    """Discover issues for a specific repository.

    Returns:
        Tuple of (issues, error_or_none, truncated)
    """
    args = [
        "issue",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--state",
        state.value,
        "--json",
        "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt",
        "--limit",
        str(DISCOVERY_ITEM_LIMIT),
    ]

    stdout, stderr, returncode = _run_gh(args)

    if returncode != 0:
        if "Could not resolve to a Repository" in stderr:
            return [], None, False
        return [], f"Failed to query {owner}/{repo}: {stderr}", False

    try:
        data = json.loads(stdout)
        if not isinstance(data, list):
            return [], f"Failed to parse JSON for {owner}/{repo}: expected list", False
        issues, soft_errors = _soft_parse_items(data, owner, repo)
        truncated = _hit_item_limit(data)
        if truncated:
            warn = (
                f"Issue list for {owner}/{repo} hit DISCOVERY_ITEM_LIMIT "
                f"({DISCOVERY_ITEM_LIMIT}); results may be incomplete"
            )
            soft_errors.append(warn)
            logger.warning(warn)
        # Soft parse errors are non-fatal; surface via error only if no issues and had soft errs
        combined_error = (
            "; ".join(soft_errors)
            if soft_errors and not issues
            else ("; ".join(soft_errors) if soft_errors else None)
        )
        # Always surface soft errors as warnings in the error channel when present
        # alongside successful items so orchestrators can log them.
        if soft_errors:
            # Prefer returning issues + a single combined warning string
            combined_error = "; ".join(soft_errors)
        return issues, combined_error, truncated
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON for {owner}/{repo}: {e}", False


def discover_pull_requests_for_repo(
    owner: str,
    repo: str,
    state: str = "open",
) -> tuple[list[Issue], str | None, bool]:
    """Discover pull requests for a specific repository.

    Includes CI rollup (`statusCheckRollup`) when available from gh pr list.

    Returns:
        Tuple of (PRs as Issue objects, error_or_none, truncated)
    """
    args = [
        "pr",
        "list",
        "--repo",
        f"{owner}/{repo}",
        "--state",
        state,
        "--json",
        "number,title,state,labels,milestone,url,assignees,createdAt,updatedAt,statusCheckRollup",
        "--limit",
        str(DISCOVERY_ITEM_LIMIT),
    ]

    stdout, stderr, returncode = _run_gh(args)

    if returncode != 0:
        if "Could not resolve to a Repository" in stderr:
            return [], None, False
        return [], f"Failed to query PRs for {owner}/{repo}: {stderr}", False

    try:
        data = json.loads(stdout)
        if not isinstance(data, list):
            return [], f"Failed to parse JSON for PRs in {owner}/{repo}: expected list", False
        issues, soft_errors = _soft_parse_items(data, owner, repo, force_pr=True)
        truncated = _hit_item_limit(data)
        if truncated:
            warn = (
                f"PR list for {owner}/{repo} hit DISCOVERY_ITEM_LIMIT "
                f"({DISCOVERY_ITEM_LIMIT}); results may be incomplete"
            )
            soft_errors.append(warn)
            logger.warning(warn)
        combined_error = "; ".join(soft_errors) if soft_errors else None
        return issues, combined_error, truncated
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON for PRs in {owner}/{repo}: {e}", False


def list_repos_for_owner(owner: str) -> tuple[list[str], str | None, bool]:
    """List repositories for a given owner.

    Returns:
        Tuple of (repo names, error_or_none, truncated)
    """
    args = [
        "repo",
        "list",
        owner,
        "--no-archived",
        "--json",
        "name",
        "--limit",
        str(DISCOVERY_ITEM_LIMIT),
    ]

    stdout, stderr, returncode = _run_gh(args)

    if returncode != 0:
        return [], f"Failed to list repos for {owner}: {stderr}", False

    try:
        data = json.loads(stdout)
        if not isinstance(data, list):
            return [], f"Failed to parse JSON for repos of {owner}: expected list", False
        repos: list[str] = []
        for item in data:
            if not isinstance(item, dict) or "name" not in item:
                continue  # Skip malformed items
            repos.append(str(item["name"]))
        truncated = _hit_item_limit(data)
        if truncated:
            logger.warning(
                "Repo list for %s hit DISCOVERY_ITEM_LIMIT (%s); results may be incomplete",
                owner,
                DISCOVERY_ITEM_LIMIT,
            )
        return repos, None, truncated
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON for repos of {owner}: {e}", False


def _resolve_owners(
    owners: list[str] | None,
    *,
    allow_non_default_owners: bool,
) -> list[str]:
    """Resolve and validate the owner scan list.

    Default allowlist is empty. Owners are resolved from:
      1. Explicit ``owners=`` argument, or
      2. ``WH_ALLOWED_OWNERS`` env (comma-separated).

    When the env allowlist is non-empty, owners outside it require
    ``allow_non_default_owners=True``. When the env allowlist is empty,
    an explicit ``owners=`` list is required for a non-empty scan (passing
    owners= is itself the operator-supplied list).
    """
    allowed = load_allowed_owners_from_env()
    allowed_set = frozenset(allowed)

    if owners is None:
        return list(allowed)

    resolved = list(owners)
    if not allowed_set:
        # Empty configured allowlist: explicit owners list is accepted as operator intent.
        return resolved

    disallowed = [o for o in resolved if o not in allowed_set]
    if disallowed and not allow_non_default_owners:
        raise OwnerPolicyError(
            "Owner(s) not in allowlist "
            f"{sorted(allowed_set)}: {disallowed}. "
            "Pass allow_non_default_owners=True for an explicit override, "
            f"or set {WH_ALLOWED_OWNERS_ENV}."
        )
    return resolved


def discover_all(
    owners: list[str] | None = None,
    state: IssueState = IssueState.OPEN,
    include_prs: bool = True,
    include_issues: bool = True,
    kind: DiscoveryKind | None = None,
    allow_non_default_owners: bool = False,
    check_auth: bool = True,
) -> DiscoveryResult:
    """Discover all eligible work across allowed owners.

    Args:
        owners: List of GitHub owners to scan. Defaults to WH_ALLOWED_OWNERS
            (empty when unset). Empty owners list yields an empty result.
        state: Filter by issue state
        include_prs: Whether to also discover pull requests (ignored if kind set)
        include_issues: Whether to discover issues (ignored if kind set)
        kind: High-level mode: "all" | "issues" | "prs". When set, overrides
            include_prs/include_issues for orchestrator kind=prs|issues|all.
        allow_non_default_owners: Explicit override to scan owners outside the
            configured WH_ALLOWED_OWNERS allowlist.
        check_auth: When True, fail fast if `gh auth status` is broken.

    Returns:
        DiscoveryResult with all found issues, any errors, and truncated flag.
    """
    if kind == "prs":
        include_issues, include_prs = False, True
    elif kind == "issues":
        include_issues, include_prs = True, False
    elif kind == "all":
        include_issues, include_prs = True, True

    if not include_issues and not include_prs:
        raise ValueError("At least one of include_issues or include_prs must be True")

    owners = _resolve_owners(owners, allow_non_default_owners=allow_non_default_owners)

    all_issues: list[Issue] = []
    all_errors: list[str] = []
    any_truncated = False

    if check_auth:
        auth_error = ensure_gh_auth()
        if auth_error:
            return DiscoveryResult(
                issues=[],
                errors=[auth_error],
                owners_scanned=list(owners),
                truncated=False,
            )

    if not owners:
        all_errors.append("No owners to scan: set WH_ALLOWED_OWNERS or pass owners= explicitly")
        return DiscoveryResult(
            issues=[],
            errors=all_errors,
            owners_scanned=[],
            truncated=False,
        )

    for owner in owners:
        repos, error, repos_truncated = list_repos_for_owner(owner)
        if repos_truncated:
            any_truncated = True
            all_errors.append(
                f"Repo list for {owner} hit DISCOVERY_ITEM_LIMIT "
                f"({DISCOVERY_ITEM_LIMIT}); results may be incomplete"
            )
        if error:
            all_errors.append(error)
            continue

        for repo in repos:
            if include_issues:
                issues, error, truncated = discover_issues_for_repo(owner, repo, state)
                if truncated:
                    any_truncated = True
                if error:
                    all_errors.append(error)
                all_issues.extend(issues)

            if include_prs:
                prs, error, truncated = discover_pull_requests_for_repo(owner, repo, state.value)
                if truncated:
                    any_truncated = True
                if error:
                    all_errors.append(error)
                all_issues.extend(prs)

    return DiscoveryResult(
        issues=all_issues,
        errors=all_errors,
        # Copy so callers cannot mutate the resolved list via the result.
        owners_scanned=list(owners),
        truncated=any_truncated,
    )


def filter_issues(
    issues: list[Issue],
    labels: list[str] | None = None,
    milestone: str | None = None,
    assignee: str | None = None,
    is_pr: bool | None = None,
) -> list[Issue]:
    """Filter issues by various criteria.

    Args:
        issues: List of issues to filter
        labels: Filter by labels (must have all specified labels)
        milestone: Filter by milestone title
        assignee: Filter by assignee username
        is_pr: Filter by whether it's a PR

    Returns:
        Filtered list of issues
    """
    result = issues

    if labels:
        result = [issue for issue in result if all(label in issue.labels for label in labels)]

    if milestone is not None:
        result = [issue for issue in result if issue.milestone == milestone]

    if assignee is not None:
        result = [issue for issue in result if assignee in issue.assignees]

    if is_pr is not None:
        result = [issue for issue in result if issue.is_pr == is_pr]

    return result


def format_for_orchestrator(result: DiscoveryResult) -> dict[str, Any]:
    """Format discovery result for orchestrator consumption.

    Args:
        result: DiscoveryResult to format

    Returns:
        Dictionary suitable for orchestrator consumption
    """
    return {
        "total_issues": len(result.issues),
        "total_errors": len(result.errors),
        "owners_scanned": list(result.owners_scanned),
        "truncated": result.truncated,
        "issues": [
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "labels": issue.labels,
                "milestone": issue.milestone,
                "url": issue.url,
                "owner": issue.owner,
                "repo": issue.repo,
                "is_pr": issue.is_pr,
                "assignees": issue.assignees,
                "created_at": issue.created_at,
                "updated_at": issue.updated_at,
                "ci_status": issue.ci_status,
            }
            for issue in result.issues
        ],
        "errors": result.errors,
    }
