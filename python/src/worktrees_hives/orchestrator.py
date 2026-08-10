"""Multi-subagent orchestrator with capped parallelism.

Spawns N worker coroutines with a concurrency cap, monitors their status,
handles timeouts and failures, and aggregates results into a final report.
"""

from __future__ import annotations

import asyncio
import enum
import math
import time
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from typing import Any


class WorkerStatus(enum.Enum):
    """Lifecycle state of a single worker."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Outcome of a single worker execution."""

    worker_id: str
    status: WorkerStatus
    result: Any = None
    error: str | None = None
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class OrchestratorReport:
    """Aggregated results from an orchestrator run."""

    total: int
    succeeded: int
    failed: int
    timed_out: int
    elapsed_seconds: float
    results: Sequence[WorkerResult]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def all_succeeded(self) -> bool:
        return self.succeeded == self.total


# Sentinel value for "inherit the orchestrator's default timeout".
_UNSET_TIMEOUT = float("nan")


@dataclass
class WorkerSpec:
    """Describes a single worker to be executed.

    Parameters
    ----------
    timeout:
        Per-worker timeout in seconds.  ``None`` means no timeout.
        The default sentinel value means fall back to the orchestrator's
        ``default_timeout``.
    """

    worker_id: str
    fn: Callable[..., Awaitable[Any]]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = field(default=_UNSET_TIMEOUT)


class WorkerTimeoutError(Exception):
    """Raised when a worker coroutine raises :exc:`TimeoutError`."""

    def __init__(self, original: TimeoutError) -> None:
        super().__init__(str(original))
        self.original = original


class Orchestrator:
    """Runs N workers with a concurrency cap.

    Parameters
    ----------
    concurrency:
        Maximum number of workers running simultaneously.
    default_timeout:
        Per-worker timeout in seconds.  ``None`` means no timeout.
        Individual ``WorkerSpec.timeout`` values override this; set a
        worker's ``timeout`` to ``None`` to disable the timeout for that
        worker even when a default is configured.
    """

    def __init__(
        self,
        concurrency: int = 4,
        default_timeout: float | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._concurrency = concurrency
        self._default_timeout = default_timeout

    async def run(
        self,
        workers: list[WorkerSpec],
        on_status_change: Callable[[str, WorkerStatus], None] | None = None,
    ) -> OrchestratorReport:
        """Execute all workers and return an aggregated report.

        Workers are dispatched subject to the concurrency cap.  Each worker
        is wrapped with timeout and error-handling logic so that one failure
        never cancels siblings.

        Parameters
        ----------
        workers:
            List of worker specifications to execute.
        on_status_change:
            Optional callback invoked on every worker state transition.
            Called with ``(worker_id, new_status)`` as each worker moves
            through PENDING → RUNNING → <terminal>.  The callback is invoked
            from within the running event loop and must not block.
        """
        if not workers:
            return OrchestratorReport(
                total=0,
                succeeded=0,
                failed=0,
                timed_out=0,
                elapsed_seconds=0.0,
                results=[],
            )

        semaphore = asyncio.Semaphore(self._concurrency)
        start = time.monotonic()

        async def _run_one(spec: WorkerSpec) -> WorkerResult:
            return await self._execute_worker(spec, semaphore, on_status_change)

        results = await asyncio.gather(
            *(_run_one(w) for w in workers),
            return_exceptions=False,
        )

        elapsed = time.monotonic() - start
        return self._build_report(list(results), elapsed)

    async def _execute_worker(
        self,
        spec: WorkerSpec,
        semaphore: asyncio.Semaphore,
        on_status_change: Callable[[str, WorkerStatus], None] | None = None,
    ) -> WorkerResult:
        """Run a single worker under the semaphore with timeout handling."""
        timeout = self._resolve_timeout(spec)

        def _notify(status: WorkerStatus) -> None:
            if on_status_change is None:
                return
            try:
                on_status_change(spec.worker_id, status)
            except Exception as err:
                warnings.warn(
                    f"on_status_change callback failed for {spec.worker_id} "
                    f"({status.value}): {err}",
                    RuntimeWarning,
                    stacklevel=2,
                )

        _notify(WorkerStatus.PENDING)

        async with semaphore:
            start = time.monotonic()
            _notify(WorkerStatus.RUNNING)
            try:
                coro = spec.fn(*spec.args, **spec.kwargs)
                result = await asyncio.wait_for(self._run_coro(coro), timeout=timeout)
                _notify(WorkerStatus.SUCCEEDED)
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.SUCCEEDED,
                    result=result,
                    elapsed_seconds=time.monotonic() - start,
                )
            except TimeoutError:
                _notify(WorkerStatus.TIMED_OUT)
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.TIMED_OUT,
                    error=f"Worker timed out after {timeout}s",
                    elapsed_seconds=time.monotonic() - start,
                )
            except WorkerTimeoutError as exc:
                _notify(WorkerStatus.FAILED)
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.FAILED,
                    error=f"{type(exc.original).__name__}: {exc.original}",
                    elapsed_seconds=time.monotonic() - start,
                )
            except Exception as exc:
                _notify(WorkerStatus.FAILED)
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_seconds=time.monotonic() - start,
                )

    def _resolve_timeout(self, spec: WorkerSpec) -> float | None:
        """Return the effective timeout for a worker.

        ``None`` means no timeout.  The default sentinel means fall back to
        the orchestrator's ``default_timeout``.
        """
        if spec.timeout is None:
            return None
        if math.isnan(spec.timeout):
            return self._default_timeout
        return spec.timeout

    @staticmethod
    async def _run_coro(coro: Awaitable[Any]) -> Any:
        """Run a worker coroutine, converting TimeoutError to a typed exception."""
        try:
            return await coro
        except TimeoutError as exc:
            raise WorkerTimeoutError(exc) from exc

    @staticmethod
    def _build_report(
        results: list[WorkerResult],
        elapsed: float,
    ) -> OrchestratorReport:
        """Tally worker outcomes into a report."""
        succeeded = sum(1 for r in results if r.status == WorkerStatus.SUCCEEDED)
        failed = sum(1 for r in results if r.status == WorkerStatus.FAILED)
        timed_out = sum(1 for r in results if r.status == WorkerStatus.TIMED_OUT)
        return OrchestratorReport(
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            timed_out=timed_out,
            elapsed_seconds=elapsed,
            results=results,
        )
