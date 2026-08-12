"""Lab hypothesis jobs: allocate / list / teardown (GH #85).

Maps **hypothesis_id → agent|subagent → sandboxed worktree**. Worktree
mutations go only through :class:`~worktrees_hives.bridge.WhClient`
(``wh worktree create|remove``). This module is **not** PR babysit state
(:class:`~worktrees_hives.watchlist.JobState` / ``Watchlist``).

Persistence (explicit)
----------------------
JSON file default::

    {user_data_dir}/worktrees-hives/lab_jobs.json

Override with env ``WH_LAB_JOBS_PATH`` (never ``WH_STATE_PATH`` or
``WH_WATCHLIST_PATH``). Schema::

    {"schema_version": 1, "jobs": { "<job_id>": { ... } }}

Writes are atomic (temp file + rename) under a cross-process exclusive lock.
Teardown keeps a tombstone with status ``torn_down``.

Owner allowlist: multi-owner scheduling is **deny-by-default** when
``WH_ALLOWED_OWNERS`` / ``allowed_owners`` is empty (same policy as babysit
discovery). Explicit non-empty allowlist required to allocate.

Default policy: no production remote mutation (no merge / bare force-push APIs).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worktrees_hives.contract import ErrorResponse, SuccessResponse
from worktrees_hives.errors import (
    PolicyError,
    WhBinaryNotFoundError,
    WhError,
    WhProcessError,
)
from worktrees_hives.findings import AgentRole
from worktrees_hives.paths import default_worktree_base, user_data_dir

if TYPE_CHECKING:
    from collections.abc import Iterator

    from worktrees_hives.bridge import WhClient

LAB_JOBS_SCHEMA_VERSION: int = 1
WH_LAB_JOBS_PATH_ENV = "WH_LAB_JOBS_PATH"
WH_ALLOWED_OWNERS_ENV = "WH_ALLOWED_OWNERS"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/-]*$")
_HYP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LabJobError(WhError):
    """Base error for lab job orchestration policy failures."""


class LabJobExistsError(LabJobError):
    """Raised when a job_id is already present in the store (active or tombstone)."""


class LabJobNotFoundError(LabJobError):
    """Raised when job_id is missing from the store."""


class LabJobStatus(StrEnum):
    """Lifecycle status for a lab hypothesis job."""

    PENDING = "pending"
    ALLOCATED = "allocated"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TORN_DOWN = "torn_down"


_LAB_JOB_FIELDS = frozenset(
    {
        "job_id",
        "hypothesis_id",
        "agent_id",
        "role",
        "owner",
        "repo",
        "branch",
        "worktree_path",
        "status",
        "created_at",
        "updated_at",
    }
)


@dataclass(frozen=True, slots=True)
class LabJob:
    """One lab hypothesis job (not a babysit PR job)."""

    job_id: str
    hypothesis_id: str
    agent_id: str
    role: AgentRole
    owner: str
    repo: str
    branch: str
    worktree_path: str | None
    status: LabJobStatus
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON persistence."""
        d = asdict(self)
        d["role"] = str(self.role)
        d["status"] = str(self.status)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LabJob:
        """Parse a job object from store JSON."""
        if not isinstance(raw, dict):
            raise LabJobError(f"job record must be an object, got {type(raw).__name__}")
        missing = _LAB_JOB_FIELDS - set(raw)
        if missing:
            raise LabJobError(f"job record missing fields: {sorted(missing)}")
        role_raw = raw["role"]
        status_raw = raw["status"]
        try:
            role = AgentRole(str(role_raw))
        except ValueError as exc:
            raise LabJobError(f"invalid role: {role_raw!r}") from exc
        try:
            status = LabJobStatus(str(status_raw))
        except ValueError as exc:
            raise LabJobError(f"invalid status: {status_raw!r}") from exc
        wt = raw["worktree_path"]
        if wt is not None and not isinstance(wt, str):
            raise LabJobError("worktree_path must be a string or null")
        return cls(
            job_id=str(raw["job_id"]),
            hypothesis_id=str(raw["hypothesis_id"]),
            agent_id=str(raw["agent_id"]),
            role=role,
            owner=str(raw["owner"]),
            repo=str(raw["repo"]),
            branch=str(raw["branch"]),
            worktree_path=wt,
            status=status,
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
        )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_nonempty(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value


def default_lab_jobs_path(*, platform: str | None = None) -> Path:
    """Return default lab jobs JSON path (override with ``WH_LAB_JOBS_PATH``)."""
    if override := _env_nonempty(WH_LAB_JOBS_PATH_ENV):
        return Path(override).expanduser()
    return Path(user_data_dir(platform=platform)) / "worktrees-hives" / "lab_jobs.json"


def _load_allowed_owners_from_env() -> frozenset[str]:
    """Parse ``WH_ALLOWED_OWNERS``. Empty/unset/``*`` → empty set (deny allocate)."""
    if WH_ALLOWED_OWNERS_ENV not in os.environ:
        return frozenset()
    raw = os.environ[WH_ALLOWED_OWNERS_ENV].strip()
    if not raw or raw == "*":
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _validate_segment(field_name: str, value: str) -> None:
    if not value or value in (".", "..") or not _SEGMENT_RE.fullmatch(value):
        raise LabJobError(
            f"Invalid {field_name} segment {value!r}: "
            "must be a plain name without separators or leading dash"
        )


def _validate_ref(field_name: str, value: str) -> None:
    if not value or not _REF_RE.fullmatch(value) or value.startswith("-"):
        raise LabJobError(
            f"Invalid {field_name} {value!r}: must be a plain git ref, not empty or option-looking"
        )


def _validate_hypothesis_id(value: str) -> None:
    if not value or not _HYP_RE.fullmatch(value):
        raise LabJobError(
            f"Invalid hypothesis_id {value!r}: use alphanumeric / . _ - (max 128, no separators)"
        )


def _slug_hypothesis(hypothesis_id: str) -> str:
    """Filesystem-safe job_id fragment from hypothesis_id."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", hypothesis_id).strip("-._")
    if not slug:
        slug = "hyp"
    return slug[:64]


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Cross-process exclusive lock for lab jobs read-modify-write."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise LabJobError(f"Cannot create lab jobs lock dir for {path}: {e}") from e
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
        raise LabJobError(f"Cannot open lab jobs lock at {lock_path}: {e}") from e


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".lab_jobs.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class LabJobStore:
    """Persistent lab job registry (store-primary list in v1)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_lab_jobs_path()
        self._jobs: dict[str, LabJob] = {}
        self._load()

    @property
    def path(self) -> Path:
        """State file path."""
        return self._path

    def _load(self) -> None:
        if not self._path.is_file():
            self._jobs = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LabJobError(f"failed to load lab jobs store {self._path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise LabJobError(f"lab jobs root must be an object: {self._path}")
        schema = raw.get("schema_version", 1)
        if (
            not isinstance(schema, int)
            or isinstance(schema, bool)
            or schema != LAB_JOBS_SCHEMA_VERSION
        ):
            raise LabJobError(f"unsupported lab jobs schema_version: {schema!r}")
        jobs_raw = raw.get("jobs", {})
        if not isinstance(jobs_raw, dict):
            raise LabJobError("lab jobs.jobs must be an object keyed by job_id")
        self._jobs = {}
        for jid, rec in jobs_raw.items():
            if not isinstance(rec, dict):
                raise LabJobError(f"job {jid!r} must be an object")
            job = LabJob.from_dict(rec)
            # Key is authoritative for the map.
            if job.job_id != str(jid):
                job = replace(job, job_id=str(jid))
            self._jobs[str(jid)] = job

    def _save(self) -> None:
        payload = {
            "schema_version": LAB_JOBS_SCHEMA_VERSION,
            "jobs": {jid: job.to_dict() for jid, job in sorted(self._jobs.items())},
        }
        _atomic_write_json(self._path, payload)

    def get(self, job_id: str) -> LabJob | None:
        """Return a job by id, or None (reloads under lock)."""
        with _exclusive_lock(self._path):
            self._load()
            return self._jobs.get(job_id)

    def list_jobs(self, *, status: LabJobStatus | None = None) -> list[LabJob]:
        """Return jobs, optionally filtered by status (store-primary)."""
        with _exclusive_lock(self._path):
            self._load()
            jobs = list(self._jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return sorted(jobs, key=lambda j: j.created_at)

    def put(self, job: LabJob) -> None:
        """Upsert a job under lock (reload then save)."""
        with _exclusive_lock(self._path):
            self._load()
            self._jobs[job.job_id] = job
            self._save()

    def contains(self, job_id: str) -> bool:
        """Whether job_id exists (including torn_down tombstones)."""
        with _exclusive_lock(self._path):
            self._load()
            return job_id in self._jobs

    def reserve_pending(self, job: LabJob) -> None:
        """Atomically claim job_id as PENDING or raise if taken."""
        with _exclusive_lock(self._path):
            self._load()
            if job.job_id in self._jobs:
                raise LabJobExistsError(f"lab job already exists: {job.job_id}")
            self._jobs[job.job_id] = job
            self._save()

    def drop_if_pending(self, job_id: str) -> None:
        """Remove a PENDING reservation after a failed allocate."""
        with _exclusive_lock(self._path):
            self._load()
            existing = self._jobs.get(job_id)
            if existing is not None and existing.status == LabJobStatus.PENDING:
                del self._jobs[job_id]
                self._save()

    def commit_allocated(self, job: LabJob) -> LabJob:
        """PENDING → ALLOCATED under lock; fail if reservation was cancelled.

        Prevents a late allocate ``put`` from resurrecting a job that concurrent
        teardown already tombstoned (or otherwise left non-PENDING).
        """
        if job.status != LabJobStatus.ALLOCATED:
            raise LabJobError("commit_allocated requires status=allocated")
        with _exclusive_lock(self._path):
            self._load()
            existing = self._jobs.get(job.job_id)
            if existing is None:
                raise LabJobError(f"allocation aborted: job {job.job_id!r} reservation missing")
            if existing.status != LabJobStatus.PENDING:
                raise LabJobError(
                    f"allocation aborted: job {job.job_id!r} is "
                    f"{existing.status.value}, expected pending "
                    "(concurrent teardown or cancel)"
                )
            self._jobs[job.job_id] = job
            self._save()
            return job


class LabJobManager:
    """Allocate and tear down lab jobs via ``wh worktree``.

    Parameters
    ----------
    wh_client:
        Required bridge to the Rust ``wh`` binary.
    worktree_base:
        Path layout root (must match ``wh`` / ``WH_WORKTREE_BASE``).
    repo_root:
        Local git root passed as ``--repo`` to ``wh worktree create``.
    store:
        Persistent job registry.
    allowed_owners:
        Non-empty owner allowlist. ``None`` loads ``WH_ALLOWED_OWNERS``.
        Empty set **denies** all allocate calls (fail closed).
    """

    def __init__(
        self,
        wh_client: WhClient,
        *,
        worktree_base: str | None = None,
        repo_root: str | None = None,
        store: LabJobStore | None = None,
        allowed_owners: frozenset[str] | None = None,
    ) -> None:
        self.wh_client = wh_client
        self.worktree_base = os.path.abspath(
            os.path.expanduser(
                worktree_base if worktree_base is not None else default_worktree_base()
            )
        )
        self.repo_root = os.path.abspath(
            os.path.expanduser(repo_root if repo_root is not None else os.getcwd())
        )
        self.store = store if store is not None else LabJobStore()
        if allowed_owners is None:
            self.allowed_owners = _load_allowed_owners_from_env()
        else:
            self.allowed_owners = frozenset(allowed_owners)

    def derive_path(self, owner: str, repo: str, job_id: str) -> str:
        """Derive sandboxed worktree path under ``worktree_base``.

        Segments are validated; path is absolute join without following
        intermediate symlinks for the escape check (component names only).
        """
        _validate_segment("owner", owner)
        _validate_segment("repo", repo)
        _validate_segment("job_id", job_id)
        path = os.path.abspath(os.path.join(self.worktree_base, owner, repo, job_id))
        base = self.worktree_base
        if path != base and not path.startswith(base + os.sep):
            raise LabJobError(f"worktree path escapes base {base!r}: {path!r}")
        return path

    def allocate(
        self,
        *,
        owner: str,
        repo: str,
        hypothesis_id: str,
        agent_id: str,
        role: AgentRole | str,
        branch: str | None = None,
        job_id: str | None = None,
    ) -> LabJob:
        """Create a worktree via ``wh`` and register an allocated lab job."""
        _validate_segment("owner", owner)
        _validate_segment("repo", repo)
        _validate_hypothesis_id(hypothesis_id)
        if not agent_id or not str(agent_id).strip():
            raise LabJobError("agent_id is required")
        self._assert_owner_allowed(owner)

        if isinstance(role, str):
            try:
                role = AgentRole(role)
            except ValueError as exc:
                raise LabJobError(f"invalid role: {role!r}") from exc

        jid = job_id if job_id is not None else self._default_job_id(hypothesis_id)
        _validate_segment("job_id", jid)

        br = branch if branch is not None else f"lab/{hypothesis_id}"
        _validate_ref("branch", br)

        worktree_path = self.derive_path(owner, repo, jid)
        now = _now_iso()
        pending = LabJob(
            job_id=jid,
            hypothesis_id=hypothesis_id,
            agent_id=str(agent_id).strip(),
            role=role,
            owner=owner,
            repo=repo,
            branch=br,
            worktree_path=None,
            status=LabJobStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        # Reserve job id before create so concurrent managers cannot collide.
        self.store.reserve_pending(pending)

        path: str | None = None
        try:
            if Path(worktree_path).exists():
                raise LabJobExistsError(f"worktree already exists for this job: {worktree_path}")
            path, ret_branch = self._wh_create(owner, repo, jid, br)
            if ret_branch != br:
                raise LabJobError(f"wh returned branch {ret_branch!r}, expected {br!r}")
            done = replace(
                pending,
                worktree_path=path,
                branch=ret_branch,
                status=LabJobStatus.ALLOCATED,
                updated_at=_now_iso(),
            )
            # Only promote if still PENDING — concurrent teardown must win.
            return self.store.commit_allocated(done)
        except Exception:
            if path is not None:
                with contextlib.suppress(LabJobError):
                    self._wh_remove(path, force=True)
            self.store.drop_if_pending(jid)
            raise

    def list_jobs(self, *, status: LabJobStatus | None = None) -> list[LabJob]:
        """List jobs from the store (primary source in v1)."""
        return self.store.list_jobs(status=status)

    def get(self, job_id: str) -> LabJob | None:
        """Lookup by job_id."""
        return self.store.get(job_id)

    def teardown(self, job_id: str, *, force: bool = False) -> LabJob:
        """Remove worktree via ``wh`` and mark job ``torn_down`` (tombstone)."""
        job = self.store.get(job_id)
        if job is None:
            raise LabJobNotFoundError(f"lab job not found: {job_id}")
        if job.status == LabJobStatus.TORN_DOWN:
            return job
        path = job.worktree_path or self.derive_path(job.owner, job.repo, job.job_id)
        self._wh_remove(path, force=force)
        done = replace(job, status=LabJobStatus.TORN_DOWN, updated_at=_now_iso())
        self.store.put(done)
        return done

    def _default_job_id(self, hypothesis_id: str) -> str:
        base = f"lab-{_slug_hypothesis(hypothesis_id)}"
        if not self.store.contains(base):
            return base
        return f"{base}-{secrets.token_hex(4)}"

    def _assert_owner_allowed(self, owner: str) -> None:
        allowed = self.allowed_owners
        if not allowed:
            raise LabJobError(
                "Owner allowlist is empty (deny-by-default). "
                f"Set {WH_ALLOWED_OWNERS_ENV} or pass allowed_owners= with at least one owner."
            )
        if owner not in allowed and owner.casefold() not in {a.casefold() for a in allowed}:
            raise LabJobError(
                f"Owner {owner!r} is not in the configured allowlist "
                f"({sorted(allowed)}). Set {WH_ALLOWED_OWNERS_ENV} or "
                "LabJobManager(allowed_owners=...)."
            )

    def _wh_create(self, owner: str, repo: str, job_id: str, branch: str) -> tuple[str, str]:
        resp = self._wh_run(
            "worktree",
            "create",
            "--repo",
            self.repo_root,
            owner,
            repo,
            job_id,
            branch,
        )
        if not isinstance(resp, SuccessResponse):
            raise LabJobError(f"wh worktree create failed: {resp.error.code}: {resp.error.message}")
        path = resp.data.get("path")
        ret_branch = resp.data.get("branch")
        if not isinstance(path, str) or not path:
            path = self.derive_path(owner, repo, job_id)
        if not isinstance(ret_branch, str) or not ret_branch:
            ret_branch = branch
        return path, ret_branch

    def _wh_remove(self, path: str, *, force: bool) -> None:
        """Remove worktree; treat already-missing path as success for tombstones."""
        args: list[str] = ["worktree", "remove", path]
        if force:
            args.append("--force")
        try:
            resp = self._wh_run(*args)
        except LabJobError:
            if not Path(path).exists():
                return
            raise
        if isinstance(resp, SuccessResponse):
            return
        if not Path(path).exists():
            return
        raise LabJobError(f"wh worktree remove failed: {resp.error.code}: {resp.error.message}")

    def _wh_run(self, *args: str) -> SuccessResponse | ErrorResponse:
        try:
            return self.wh_client.run(*args)
        except WhBinaryNotFoundError as exc:
            raise LabJobError(
                "wh binary not found; install wh or set WH_BIN "
                f"(lab jobs require Rust worktree CLI): {exc}"
            ) from exc
        except PolicyError as exc:
            raise LabJobError(f"wh policy rejection [{exc.code}]: {exc.message}") from exc
        except WhProcessError as exc:
            raise LabJobError(f"wh exited {exc.returncode}: {exc.stderr or 'no stderr'}") from exc
        except WhError as exc:
            raise LabJobError(str(exc)) from exc
