"""Watchlist: persistent multi-job hive state across check cycles."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worktrees_hives.errors import PolicyError
from worktrees_hives.paths import _env_nonempty, user_data_dir

if TYPE_CHECKING:
    from collections.abc import Iterator
# Python orchestrator watchlist store (separate from Rust `watched.json`).
# Rust `wh status`/`wh jobs` expect a JSON *array* of JobStatus at WH_STATE_PATH/
# watched.json (see crates/wh-core/src/state.rs). This module persists a
# Python-side map envelope; do not share the same file until schemas unify.
# Override path with WH_WATCHLIST_PATH only (never WH_STATE_PATH — that is Rust).
STATE_FILENAME = "watchlist.json"
WH_WATCHLIST_PATH_ENV = "WH_WATCHLIST_PATH"
WH_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"
MAX_FIXES_CEILING = 3

# Empty default — no org hardcoding. Configure via WH_ALLOWED_OWNERS or
# Watchlist(allowed_owners=...). Empty allowlist = deny-by-default (no owner matches).
ALLOWED_OWNERS: frozenset[str] = frozenset()


def load_allowed_owners_from_env() -> frozenset[str]:
    """Parse WH_ALLOWED_OWNERS (comma-separated). Empty/unset → empty set."""
    raw = os.environ.get(WH_ALLOWED_OWNERS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class JobStatus(StrEnum):
    """Status of a watched job."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class CorruptStateError(ValueError):
    """State file is corrupt; original was quarantined. Do not silently wipe."""


@dataclass
class JobState:
    """State of a single job in the watchlist."""

    job_id: str
    owner: str
    repo: str
    branch: str
    status: JobStatus = JobStatus.PENDING
    stack_id: str | None = None
    fix_count: int = 0
    max_fixes: int = 3
    # Identifies the current babysit cycle for fix-budget accounting (AGENTS.md).
    babysit_cycle: str | None = None
    residual_blockers: list[str] = field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None
    last_check: str | None = None
    error: str | None = None

    @property
    def full_repo(self) -> str:
        """Return owner/repo format."""
        return f"{self.owner}/{self.repo}"

    @property
    def is_actionable(self) -> bool:
        """Return True if the job can accept new work."""
        return self.status in {
            JobStatus.PENDING,
            JobStatus.IN_PROGRESS,
            JobStatus.BLOCKED,
        }

    @property
    def fix_budget_remaining(self) -> int:
        """Return remaining fix commits allowed."""
        return max(0, min(self.max_fixes, MAX_FIXES_CEILING) - self.fix_count)


# Known JobState field names for additive-v1 compatibility (ignore extras on construct).
_JOB_STATE_FIELDS: frozenset[str] = frozenset(f.name for f in fields(JobState))


def _default_state_path() -> Path:
    """Return the default Python watchlist state file path.

    Honors WH_WATCHLIST_PATH only (non-empty). Never falls back to WH_STATE_PATH
    (Rust array-backed watched.json). Pass an explicit path or --state for tests.
    """
    env_path = _env_nonempty(WH_WATCHLIST_PATH_ENV)
    if env_path:
        return Path(env_path)
    return Path(user_data_dir()) / "worktrees-hives" / STATE_FILENAME


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Cross-process exclusive lock for watchlist read-modify-write.

    Uses a sibling lock file so concurrent CLI/orchestrator processes serialize
    mutations and do not overwrite each other's jobs via stale in-memory snapshots.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise CorruptStateError(f"Cannot create watchlist lock dir for {path}: {e}") from e
    lock_path = path.parent / f".{path.name}.lock"
    try:
        with open(lock_path, "a+", encoding="utf-8") as lock_f:
            if sys.platform == "win32":
                import msvcrt

                lock_f.seek(0)
                if lock_f.read(1) == "":
                    lock_f.write("0")
                    lock_f.flush()
                lock_f.seek(0)
                msvcrt.locking(lock_f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    if sys.platform == "win32":
                        import msvcrt

                        lock_f.seek(0)
                        msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        raise CorruptStateError(f"Cannot open watchlist lock at {lock_path}: {e}") from e


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically using temp file + rename."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".watched-")
    except OSError as e:
        raise CorruptStateError(f"Cannot create watchlist state at {path}: {e}") from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise CorruptStateError(f"Cannot write watchlist state at {path}: {e}") from e
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _quarantine_corrupt(path: Path) -> Path:
    """Move a corrupt state file aside; return quarantine path."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    quarantine = path.with_name(f"{path.name}.corrupt.{stamp}")
    # Avoid clobbering an existing quarantine path
    n = 0
    candidate = quarantine
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.corrupt.{stamp}.{n}")
    path.replace(candidate)
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    """Read JSON state file.

    Missing file → empty dict (first run).
    Corrupt JSON / I/O errors → CorruptStateError (never silent wipe / traceback).
    """
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            if not isinstance(result, dict):
                raise json.JSONDecodeError(
                    "state root must be a JSON object",
                    doc=str(result),
                    pos=0,
                )
            return result
    except OSError as e:
        raise CorruptStateError(f"Cannot read watchlist state at {path}: {e}") from e
    except json.JSONDecodeError as e:
        try:
            quarantine = _quarantine_corrupt(path)
        except OSError as oe:
            raise CorruptStateError(
                f"Corrupt watchlist state at {path} (and quarantine failed: {oe}): {e}"
            ) from e
        raise CorruptStateError(
            f"Corrupt watchlist state at {path}; quarantined to {quarantine}: {e}. "
            "Refusing to load empty state that would wipe durable data on next save."
        ) from e


def _validate_max_fixes(max_fixes: int) -> int:
    """Validate max_fixes is in [0, MAX_FIXES_CEILING] for write paths (add)."""
    if max_fixes < 0:
        raise ValueError("max_fixes must be non-negative")
    if max_fixes > MAX_FIXES_CEILING:
        raise PolicyError(
            "MAX_FIXES_CEILING",
            f"max_fixes ({max_fixes}) exceeds safety ceiling ({MAX_FIXES_CEILING}). "
            "Per AGENTS.md, at most 3 code-fix commits per PR per cycle.",
        )
    return max_fixes


def _clamp_max_fixes(max_fixes: int) -> int:
    """Clamp loaded max_fixes into [0, MAX_FIXES_CEILING] without dropping the job."""
    if max_fixes < 0:
        return 0
    return min(max_fixes, MAX_FIXES_CEILING)


def _owner_allowed(owner: str, allowed_owners: frozenset[str]) -> bool:
    """Return whether owner is within the configured repository scope.

    Empty allowlist denies all owners (deny-by-default). Operators must set
    WH_ALLOWED_OWNERS or pass allowed_owners= explicitly.
    """
    return bool(allowed_owners) and owner in allowed_owners


class Watchlist:
    """Persistent watchlist for multi-job hive state.

    State is stored as JSON at a configurable path (default: platform data dir /
    watchlist.json, or WH_WATCHLIST_PATH). Writes are atomic (temp file + rename).
    Separate from Rust ``watched.json`` array store (never shares WH_STATE_PATH).
    """

    def __init__(
        self,
        path: Path | None = None,
        allowed_owners: frozenset[str] | None = None,
    ) -> None:
        self._path = path or _default_state_path()
        self._jobs: dict[str, JobState] = {}
        # Additive v1 fields (kind, worktree_path, timestamps, …) preserved per job.
        self._job_extras: dict[str, dict[str, Any]] = {}
        # Raw job records not active in this process (disallowed owner / unparseable).
        # Re-written on save so a partial load never permanently drops durable entries.
        self._deferred_raw: dict[str, dict[str, Any]] = {}
        self._top_extras: dict[str, Any] = {}
        if allowed_owners is None:
            self._allowed_owners = load_allowed_owners_from_env()
        else:
            self._allowed_owners = frozenset(allowed_owners)
        self._load()

    @property
    def path(self) -> Path:
        """Return the state file path."""
        return self._path

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize mutations: exclusive lock + reload before the critical section.

        Callers must invoke ``_save()`` inside the ``with`` block after mutating
        in-memory state so concurrent processes cannot overwrite each other.
        """
        with _exclusive_lock(self._path):
            self._load()
            yield

    def _load(self) -> None:
        """Load state from disk; clamp max_fixes; filter by owner allowlist.

        Unknown keys on job objects are treated as additive v1 fields: filtered
        out of JobState construction and preserved for round-trip on save so a
        compatible writer (e.g. Rust) does not lose watched jobs.

        Jobs outside the owner allowlist or that fail to parse are kept in
        ``_deferred_raw`` so the next save does not permanently erase them.
        """
        data = _read_json(self._path)
        schema = data.get("schema_version", 1)
        try:
            schema_i = int(schema)
        except (TypeError, ValueError) as e:
            raise CorruptStateError(f"Invalid schema_version in {self._path}: {schema!r}") from e
        if schema_i > 1:
            raise CorruptStateError(
                f"Unsupported watchlist schema_version {schema_i} in {self._path} "
                f"(this build supports 1 only)"
            )
        # Preserve unknown top-level keys for additive v1 round-trip.
        self._top_extras = {k: v for k, v in data.items() if k not in {"schema_version", "jobs"}}
        if "jobs" in data and not isinstance(data["jobs"], dict):
            # e.g. "jobs": [] from a bad recovery — do not normalize to {} and wipe on save.
            raise CorruptStateError(
                f"Malformed jobs block in {self._path}: expected JSON object, "
                f"got {type(data['jobs']).__name__}"
            )
        jobs_data = data.get("jobs", {})
        if not isinstance(jobs_data, dict):
            # Defensive: absent key defaults above; any other type already rejected.
            jobs_data = {}
        self._jobs = {}
        self._job_extras = {}
        self._deferred_raw = {}
        for job_id, job_dict in jobs_data.items():
            jid = str(job_id)
            if not isinstance(job_dict, dict):
                # Preserve non-object entries under a wrapper so they are not dropped.
                self._deferred_raw[jid] = {"_invalid_job_entry": job_dict}
                continue
            raw = dict(job_dict)
            try:
                extras = {k: v for k, v in raw.items() if k not in _JOB_STATE_FIELDS}
                d = {k: v for k, v in raw.items() if k in _JOB_STATE_FIELDS}
                d["status"] = JobStatus(d["status"])
                max_fixes = _clamp_max_fixes(int(d.get("max_fixes", 3)))
                d["max_fixes"] = max_fixes
                fix_count = int(d.get("fix_count", 0))
                if fix_count < 0:
                    fix_count = 0
                d["fix_count"] = fix_count
                owner = str(d["owner"])
                if not _owner_allowed(owner, self._allowed_owners):
                    # Not scheduled, but keep durable record for later allowlist changes.
                    self._deferred_raw[jid] = raw
                    continue
                d["owner"] = owner
                # Dict key is authoritative for job_id (avoid silent drift).
                d["job_id"] = jid
                self._jobs[jid] = JobState(**d)
                if extras:
                    self._job_extras[jid] = extras
            except (KeyError, ValueError, TypeError, PolicyError):
                # Preserve unparseable records so they are not wiped on next save.
                self._deferred_raw[jid] = raw

    def _save(self) -> None:
        """Save state to disk atomically."""
        jobs: dict[str, dict[str, Any]] = {}
        for jid, job in self._jobs.items():
            job_dict: dict[str, Any] = asdict(job)
            job_dict["job_id"] = jid
            # Enum values → strings for JSON
            status = job_dict.get("status")
            if isinstance(status, JobStatus):
                job_dict["status"] = status.value
            # Preserve additive v1 fields from compatible writers
            for key, value in self._job_extras.get(jid, {}).items():
                if key not in job_dict:
                    job_dict[key] = value
            jobs[jid] = job_dict
        # Re-emit deferred records (disallowed / unparseable) so they are not lost.
        for jid, raw in self._deferred_raw.items():
            if jid not in jobs:
                jobs[jid] = raw
        data: dict[str, Any] = {
            "schema_version": 1,
            "jobs": jobs,
        }
        for key, value in self._top_extras.items():
            if key not in data:
                data[key] = value
        _atomic_write_json(self._path, data)

    def add(
        self,
        job_id: str,
        owner: str,
        repo: str,
        branch: str,
        stack_id: str | None = None,
        max_fixes: int = 3,
    ) -> JobState:
        """Add a new job to the watchlist.

        Raises ValueError if job_id already exists or max_fixes is negative.
        Raises PolicyError if max_fixes exceeds the safety ceiling (3) or
        owner is outside the configured allowlist.
        """
        max_fixes = _validate_max_fixes(max_fixes)
        if not _owner_allowed(owner, self._allowed_owners):
            allow = sorted(self._allowed_owners)
            hint = (
                f"allowed owners: {allow}"
                if allow
                else f"set {WH_ALLOWED_OWNERS_ENV} or pass allowed_owners="
            )
            raise PolicyError(
                "OWNER_NOT_ALLOWED",
                f"Owner {owner!r} not in allowlist ({hint})",
            )
        with self._locked():
            if job_id in self._jobs or job_id in self._deferred_raw:
                raise ValueError(f"Job {job_id!r} already exists in watchlist")
            job = JobState(
                job_id=job_id,
                owner=owner,
                repo=repo,
                branch=branch,
                stack_id=stack_id,
                max_fixes=max_fixes,
            )
            self._jobs[job_id] = job
            try:
                self._save()
            except Exception:
                self._jobs.pop(job_id, None)
                raise
            return job

    def remove(self, job_id: str) -> None:
        """Remove a job from the watchlist.

        Removes active jobs, or deferred (disallowed/unparseable) records when
        the operator explicitly names the job_id (recovery path; no second API).

        Raises KeyError if job_id not found.
        """
        with self._locked():
            if job_id not in self._jobs and job_id not in self._deferred_raw:
                raise KeyError(f"Job {job_id!r} not found in watchlist")
            self._jobs.pop(job_id, None)
            self._job_extras.pop(job_id, None)
            self._deferred_raw.pop(job_id, None)
            self._save()

    def get(self, job_id: str) -> JobState | None:
        """Get a job by ID, or None if not found."""
        return self._jobs.get(job_id)

    def list_jobs(
        self,
        owner: str | None = None,
        repo: str | None = None,
        status: JobStatus | None = None,
    ) -> list[JobState]:
        """List jobs with optional filters."""
        result = list(self._jobs.values())
        if owner is not None:
            result = [j for j in result if j.owner == owner]
        if repo is not None:
            result = [j for j in result if j.repo == repo]
        if status is not None:
            result = [j for j in result if j.status == status]
        return result

    def update_status(self, job_id: str, status: JobStatus) -> JobState:
        """Update a job's status.

        Raises KeyError if job_id not found.
        """
        with self._locked():
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job {job_id!r} not found in watchlist")
            job.status = status
            self._save()
            return job

    def begin_babysit_cycle(self, cycle_id: str, job_id: str | None = None) -> None:
        """Start a babysit cycle, resetting fix_count for the cycle budget.

        Per AGENTS.md the 3-fix cap is per babysit cycle. When ``cycle_id``
        differs from the job's stored ``babysit_cycle``, ``fix_count`` resets
        to 0. Pass ``job_id`` to scope one job; omit to apply to all active jobs.
        """
        if not cycle_id:
            raise ValueError("cycle_id must be non-empty")
        with self._locked():
            targets = [job_id] if job_id is not None else list(self._jobs)
            for jid in targets:
                job = self._jobs.get(jid)
                if job is None:
                    if job_id is not None:
                        raise KeyError(f"Job {job_id!r} not found in watchlist")
                    continue
                if job.babysit_cycle != cycle_id:
                    job.babysit_cycle = cycle_id
                    job.fix_count = 0
            self._save()

    def increment_fix_count(self, job_id: str, cycle_id: str | None = None) -> JobState:
        """Increment the fix count for a job in the current babysit cycle.

        Enforces both the per-job max_fixes budget and the global safety
        ceiling (MAX_FIXES_CEILING) so a misconfigured max cannot exceed policy.

        If ``cycle_id`` is provided and differs from the job's cycle, the
        budget resets (new babysit cycle) before incrementing.

        Raises KeyError if job_id not found.
        Raises PolicyError if fix budget or safety ceiling is exhausted.
        """
        with self._locked():
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job {job_id!r} not found in watchlist")
            if cycle_id is not None and job.babysit_cycle != cycle_id:
                job.babysit_cycle = cycle_id
                job.fix_count = 0
            effective_max = min(job.max_fixes, MAX_FIXES_CEILING)
            # AGENTS.md: cap is per PR per cycle — sum siblings sharing owner/repo/PR.
            if job.pr_number is not None:
                used = sum(
                    j.fix_count
                    for j in self._jobs.values()
                    if j.owner == job.owner
                    and j.repo == job.repo
                    and j.pr_number == job.pr_number
                    and j.babysit_cycle == job.babysit_cycle
                )
            else:
                used = job.fix_count
            if used >= effective_max:
                raise PolicyError(
                    "FIX_BUDGET_EXHAUSTED",
                    f"Job {job_id!r} has exhausted its fix budget "
                    f"({effective_max}; ceiling {MAX_FIXES_CEILING}"
                    + (
                        f"; PR #{job.pr_number} cycle total {used}"
                        if job.pr_number is not None
                        else ""
                    )
                    + ")",
                )
            job.fix_count += 1
            try:
                self._save()
            except Exception:
                job.fix_count -= 1
                raise
            return job

    def set_blockers(self, job_id: str, blockers: list[str]) -> JobState:
        """Set residual blockers for a job.

        Raises KeyError if job_id not found.
        """
        with self._locked():
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job {job_id!r} not found in watchlist")
            job.residual_blockers = list(blockers)
            self._save()
            return job

    def set_pr(self, job_id: str, pr_number: int, pr_url: str) -> JobState:
        """Set PR info for a job.

        Raises KeyError if job_id not found.
        """
        with self._locked():
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job {job_id!r} not found in watchlist")
            job.pr_number = pr_number
            job.pr_url = pr_url
            self._save()
            return job

    def record_check(self, job_id: str, error: str | None = None) -> JobState:
        """Record a check timestamp (and optional error) for a job and persist.

        Raises KeyError if job_id not found.
        """
        with self._locked():
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job {job_id!r} not found in watchlist")
            job.last_check = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job.error = error
            self._save()
            return job

    def check(
        self,
        owner: str | None = None,
        repo: str | None = None,
        *,
        record: bool = True,
    ) -> dict[str, list[JobState]]:
        """Check jobs and categorize by action needed.

        Categories:
          - needs_pr: pending/actionable job with no PR yet (not already running)
          - needs_fix: has residual blockers and remaining fix budget (not IN_PROGRESS)
          - in_progress: IN_PROGRESS — defer; do not re-queue PR creation or fixes
          - blocked: residual blockers with exhausted budget, or explicit BLOCKED status
          - ready: has PR, no residual blockers (awaiting merge / healthy)
          - done: COMPLETED or FAILED

        Optional ``owner`` / ``repo`` filters mirror ``list_jobs``.
        When ``record`` is True (default), updates each matched job's
        ``last_check`` timestamp and persists once.

        Note: a green PR with remaining budget is **ready**, not needs_fix.
        Exhausted budget with blockers is **blocked**, not ready.
        """
        if record:
            with self._locked():
                return self._check_unlocked(owner=owner, repo=repo, record=True)
        return self._check_unlocked(owner=owner, repo=repo, record=False)

    def _check_unlocked(
        self,
        owner: str | None = None,
        repo: str | None = None,
        *,
        record: bool = True,
    ) -> dict[str, list[JobState]]:
        """Categorize jobs; when ``record`` is True, stamp and save (caller holds lock)."""
        result: dict[str, list[JobState]] = {
            "needs_pr": [],
            "needs_fix": [],
            "in_progress": [],
            "blocked": [],
            "ready": [],
            "done": [],
        }
        jobs = self.list_jobs(owner=owner, repo=repo)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for job in jobs:
            if record:
                # Stamp check time only; do not wipe durable job.error details.
                job.last_check = now
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                result["done"].append(job)
            elif job.status == JobStatus.IN_PROGRESS:
                # Active worker — never re-queue PR creation or concurrent fixes.
                result["in_progress"].append(job)
            elif job.residual_blockers:
                if job.fix_budget_remaining > 0 and job.status != JobStatus.BLOCKED:
                    result["needs_fix"].append(job)
                else:
                    result["blocked"].append(job)
            elif job.status == JobStatus.BLOCKED:
                result["blocked"].append(job)
            elif job.pr_number is None:
                result["needs_pr"].append(job)
            else:
                # Has PR, no residual blockers — ready regardless of remaining budget
                result["ready"].append(job)
        if record and jobs:
            self._save()
        return result
