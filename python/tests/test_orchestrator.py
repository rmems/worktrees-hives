"""Tests for the multi-subagent orchestrator."""

from __future__ import annotations

import asyncio

import pytest

from worktrees_hives.orchestrator import (
    Orchestrator,
    OrchestratorReport,
    WorkerResult,
    WorkerSpec,
    WorkerStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ok(value: str = "done", delay: float = 0.0) -> str:
    await asyncio.sleep(delay)
    return value


async def _fail(msg: str = "boom") -> None:
    raise RuntimeError(msg)


async def _hang(delay: float = 999) -> None:
    await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# WorkerStatus / WorkerResult basics
# ---------------------------------------------------------------------------


class TestWorkerStatus:
    def test_enum_values(self):
        assert WorkerStatus.PENDING.value == "pending"
        assert WorkerStatus.RUNNING.value == "running"
        assert WorkerStatus.SUCCEEDED.value == "succeeded"
        assert WorkerStatus.FAILED.value == "failed"
        assert WorkerStatus.TIMED_OUT.value == "timed_out"


class TestWorkerResult:
    def test_defaults(self):
        r = WorkerResult(worker_id="w1", status=WorkerStatus.SUCCEEDED)
        assert r.result is None
        assert r.error is None
        assert r.elapsed_seconds == 0.0

    def test_frozen(self):
        r = WorkerResult(worker_id="w1", status=WorkerStatus.SUCCEEDED)
        with pytest.raises(AttributeError):
            r.worker_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OrchestratorReport
# ---------------------------------------------------------------------------


class TestOrchestratorReport:
    def test_all_succeeded_true(self):
        report = OrchestratorReport(
            total=3,
            succeeded=3,
            failed=0,
            timed_out=0,
            elapsed_seconds=0.1,
            results=[],
        )
        assert report.all_succeeded is True

    def test_all_succeeded_false(self):
        report = OrchestratorReport(
            total=3,
            succeeded=2,
            failed=1,
            timed_out=0,
            elapsed_seconds=0.1,
            results=[],
        )
        assert report.all_succeeded is False

    def test_results_are_tuple(self):
        report = OrchestratorReport(
            total=1,
            succeeded=1,
            failed=0,
            timed_out=0,
            elapsed_seconds=0.1,
            results=[WorkerResult(worker_id="w1", status=WorkerStatus.SUCCEEDED)],
        )
        assert isinstance(report.results, tuple)
        with pytest.raises(AttributeError):
            report.results.append(WorkerResult("w2", WorkerStatus.SUCCEEDED))  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Orchestrator — constructor validation
# ---------------------------------------------------------------------------


class TestOrchestratorInit:
    def test_concurrency_zero_raises(self):
        with pytest.raises(ValueError, match="concurrency must be >= 1"):
            Orchestrator(concurrency=0)

    def test_concurrency_negative_raises(self):
        with pytest.raises(ValueError, match="concurrency must be >= 1"):
            Orchestrator(concurrency=-1)

    def test_defaults(self):
        o = Orchestrator()
        assert o._concurrency == 4
        assert o._default_timeout is None


# ---------------------------------------------------------------------------
# Orchestrator.run — empty input
# ---------------------------------------------------------------------------


class TestOrchestratorEmpty:
    async def test_empty_workers(self):
        o = Orchestrator()
        report = await o.run([])
        assert report.total == 0
        assert report.all_succeeded is True
        assert report.elapsed_seconds >= 0


# ---------------------------------------------------------------------------
# Orchestrator.run — success cases
# ---------------------------------------------------------------------------


class TestOrchestratorSuccess:
    async def test_single_worker(self):
        o = Orchestrator(concurrency=2)
        spec = WorkerSpec(worker_id="w1", fn=_ok, args=("hello",))
        report = await o.run([spec])
        assert report.total == 1
        assert report.succeeded == 1
        assert report.failed == 0
        assert report.results[0].result == "hello"
        assert report.results[0].status == WorkerStatus.SUCCEEDED

    async def test_multiple_workers(self):
        o = Orchestrator(concurrency=2)
        specs = [WorkerSpec(worker_id=f"w{i}", fn=_ok, args=(f"r{i}",)) for i in range(5)]
        report = await o.run(specs)
        assert report.total == 5
        assert report.succeeded == 5
        assert report.all_succeeded is True
        results_by_id = {r.worker_id: r.result for r in report.results}
        for i in range(5):
            assert results_by_id[f"w{i}"] == f"r{i}"

    async def test_concurrency_cap_observed(self):
        """Verify that no more than `concurrency` workers run simultaneously."""
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def tracked_worker():
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1

        o = Orchestrator(concurrency=3)
        specs = [WorkerSpec(worker_id=f"w{i}", fn=tracked_worker) for i in range(10)]
        await o.run(specs)
        assert max_concurrent <= 3


# ---------------------------------------------------------------------------
# Orchestrator.run — failure cases
# ---------------------------------------------------------------------------


class TestOrchestratorFailure:
    async def test_worker_failure_does_not_cancel_siblings(self):
        o = Orchestrator(concurrency=4)
        specs = [
            WorkerSpec(worker_id="ok1", fn=_ok, args=("a",)),
            WorkerSpec(worker_id="fail", fn=_fail, args=("bad news",)),
            WorkerSpec(worker_id="ok2", fn=_ok, args=("b",)),
        ]
        report = await o.run(specs)
        assert report.total == 3
        assert report.succeeded == 2
        assert report.failed == 1
        fail_result = next(r for r in report.results if r.worker_id == "fail")
        assert fail_result.status == WorkerStatus.FAILED
        assert "RuntimeError: bad news" in fail_result.error  # type: ignore[union-attr]

    async def test_all_workers_fail(self):
        o = Orchestrator(concurrency=2)
        specs = [WorkerSpec(worker_id=f"w{i}", fn=_fail) for i in range(3)]
        report = await o.run(specs)
        assert report.failed == 3
        assert report.succeeded == 0
        assert not report.all_succeeded


# ---------------------------------------------------------------------------
# Orchestrator.run — timeout cases
# ---------------------------------------------------------------------------


class TestOrchestratorTimeout:
    async def test_default_timeout(self):
        o = Orchestrator(concurrency=2, default_timeout=0.05)
        spec = WorkerSpec(worker_id="slow", fn=_hang)
        report = await o.run([spec])
        assert report.total == 1
        assert report.timed_out == 1
        assert report.results[0].status == WorkerStatus.TIMED_OUT
        assert "timed out" in report.results[0].error  # type: ignore[union-attr]

    async def test_per_worker_timeout(self):
        o = Orchestrator(concurrency=2)
        specs = [
            WorkerSpec(worker_id="fast", fn=_ok, args=("ok",), timeout=1.0),
            WorkerSpec(worker_id="slow", fn=_hang, timeout=0.05),
        ]
        report = await o.run(specs)
        assert report.succeeded == 1
        assert report.timed_out == 1
        fast = next(r for r in report.results if r.worker_id == "fast")
        slow = next(r for r in report.results if r.worker_id == "slow")
        assert fast.result == "ok"
        assert slow.status == WorkerStatus.TIMED_OUT

    async def test_no_timeout_means_wait(self):
        o = Orchestrator(concurrency=2, default_timeout=None)
        spec = WorkerSpec(worker_id="w1", fn=_ok, args=("done",), kwargs={"delay": 0.01})
        report = await o.run([spec])
        assert report.succeeded == 1
        assert report.results[0].result == "done"

    async def test_per_worker_timeout_opt_out(self):
        """A worker with timeout=None ignores the orchestrator default."""
        o = Orchestrator(concurrency=2, default_timeout=0.05)
        spec = WorkerSpec(
            worker_id="w1",
            fn=_ok,
            kwargs={"delay": 0.1},
            timeout=None,
        )
        report = await o.run([spec])
        assert report.succeeded == 1
        assert report.results[0].status == WorkerStatus.SUCCEEDED

    async def test_worker_raised_timeout_error_is_failed(self):
        """A worker that raises asyncio.TimeoutError is reported as FAILED."""

        async def raise_timeout() -> None:
            raise TimeoutError("boom")

        o = Orchestrator(concurrency=2, default_timeout=None)
        spec = WorkerSpec(worker_id="w1", fn=raise_timeout)
        report = await o.run([spec])
        assert report.total == 1
        assert report.succeeded == 0
        assert report.failed == 1
        assert report.timed_out == 0
        result = report.results[0]
        assert result.status == WorkerStatus.FAILED
        assert "TimeoutError" in result.error
        assert "boom" in result.error


# ---------------------------------------------------------------------------
# Orchestrator.run — mixed scenarios
# ---------------------------------------------------------------------------


class TestOrchestratorMixed:
    async def test_mixed_outcomes(self):
        o = Orchestrator(concurrency=3, default_timeout=0.1)
        specs = [
            WorkerSpec(worker_id="ok", fn=_ok, args=("yep",)),
            WorkerSpec(worker_id="fail", fn=_fail),
            WorkerSpec(worker_id="timeout", fn=_hang),
        ]
        report = await o.run(specs)
        assert report.total == 3
        assert report.succeeded == 1
        assert report.failed == 1
        assert report.timed_out == 1
        assert report.elapsed_seconds > 0

    async def test_worker_receives_kwargs(self):
        async def echo(a: str, b: str = "default") -> str:
            return f"{a}-{b}"

        o = Orchestrator(concurrency=2)
        spec = WorkerSpec(worker_id="w1", fn=echo, args=("hello",), kwargs={"b": "world"})
        report = await o.run([spec])
        assert report.results[0].result == "hello-world"

    async def test_elapsed_seconds_positive(self):
        o = Orchestrator(concurrency=2)
        spec = WorkerSpec(worker_id="w1", fn=_ok, kwargs={"delay": 0.01})
        report = await o.run([spec])
        assert report.elapsed_seconds >= 0.01
        assert report.results[0].elapsed_seconds >= 0.01


# ---------------------------------------------------------------------------
# Orchestrator.run — on_status_change callback
# ---------------------------------------------------------------------------


class TestOrchestratorStatusCallback:
    async def test_callback_receives_pending_running_terminal(self):
        transitions: list[tuple[str, WorkerStatus]] = []

        def on_change(worker_id: str, status: WorkerStatus) -> None:
            transitions.append((worker_id, status))

        o = Orchestrator(concurrency=2)
        spec = WorkerSpec(worker_id="w1", fn=_ok, args=("hi",))
        await o.run([spec], on_status_change=on_change)

        statuses = [s for _, s in transitions if _ == "w1"]
        assert statuses[0] == WorkerStatus.PENDING
        assert statuses[1] == WorkerStatus.RUNNING
        assert statuses[2] == WorkerStatus.SUCCEEDED

    async def test_callback_reports_failed_terminal(self):
        transitions: list[tuple[str, WorkerStatus]] = []

        o = Orchestrator(concurrency=2)
        spec = WorkerSpec(worker_id="w1", fn=_fail)
        await o.run([spec], on_status_change=lambda wid, s: transitions.append((wid, s)))

        statuses = [s for _, s in transitions]
        assert WorkerStatus.PENDING in statuses
        assert WorkerStatus.RUNNING in statuses
        assert WorkerStatus.FAILED in statuses

    async def test_callback_reports_timed_out_terminal(self):
        transitions: list[tuple[str, WorkerStatus]] = []

        o = Orchestrator(concurrency=2, default_timeout=0.05)
        spec = WorkerSpec(worker_id="w1", fn=_hang)
        await o.run([spec], on_status_change=lambda wid, s: transitions.append((wid, s)))

        statuses = [s for _, s in transitions]
        assert WorkerStatus.TIMED_OUT in statuses

    async def test_no_callback_does_not_raise(self):
        o = Orchestrator(concurrency=2)
        spec = WorkerSpec(worker_id="w1", fn=_ok)
        report = await o.run([spec])
        assert report.succeeded == 1

    async def test_pending_precedes_running_under_contention(self):
        """Workers queued behind the cap still emit PENDING before RUNNING."""
        transitions: list[tuple[str, WorkerStatus]] = []

        def on_change(worker_id: str, status: WorkerStatus) -> None:
            transitions.append((worker_id, status))

        o = Orchestrator(concurrency=1)
        specs = [WorkerSpec(worker_id=f"w{i}", fn=_ok, kwargs={"delay": 0.01}) for i in range(3)]
        await o.run(specs, on_status_change=on_change)

        for wid in ("w0", "w1", "w2"):
            wid_statuses = [s for w, s in transitions if w == wid]
            assert wid_statuses[0] == WorkerStatus.PENDING
            assert wid_statuses[1] == WorkerStatus.RUNNING

    async def test_callback_failure_does_not_rewrite_worker_outcome(self):
        """A failing status callback is isolated and does not flip SUCCEEDED to FAILED."""

        def on_change(worker_id: str, status: WorkerStatus) -> None:
            if status == WorkerStatus.SUCCEEDED:
                raise RuntimeError("sink broken")

        o = Orchestrator(concurrency=2)
        spec = WorkerSpec(worker_id="w1", fn=_ok)
        with pytest.warns(RuntimeWarning, match="sink broken"):
            report = await o.run([spec], on_status_change=on_change)

        assert report.succeeded == 1
        assert report.results[0].status == WorkerStatus.SUCCEEDED
