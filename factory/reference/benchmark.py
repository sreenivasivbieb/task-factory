"""P10 background benchmark: how many tasks the AI under test solved, and how many it failed.

A validated package makes one job per (task, agent, attempt) in the MongoDB queue. Workers lease a
job with a deadline and send heartbeats; a killed worker's job is leased again when its lease runs
out, so a restart resumes the queue. The result never changes a task's validity.

Zone C always uses the factory harness: the AI under test sees the base repository and
``instruction.md`` only; web tools are off, and the sandbox reaches only model APIs through the
egress allowlist. Hidden tests run after the agent stops.

``oracle`` (replays the reference patch: solves) and ``nop`` (does nothing: fails) run on both.
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import FactoryError
from .schema import BenchmarkJob, BenchmarkResult, JobBudget, JobState, outcome_of, stable_id
from .store import ReferenceStore

log = logging.getLogger("factory.reference.benchmark")


def dir_checksum(path: Path) -> str:
    """Checksum of a task folder: a changed package invalidates older benchmark results."""
    h = hashlib.sha256()
    root = Path(path)
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:16]


def split_agent(agent_id: str) -> tuple[str, str | None]:
    agent, _, model = agent_id.partition(":")
    return agent, (model or None)


def enqueue(store: ReferenceStore, run_id: str, task_id: str, task_dir: Path, agents: list[str], k: int = 1,
            proposal_id: str | None = None, package_dir: Path | None = None, budget: JobBudget | None = None) -> list[str]:
    """One job per (agent, attempt) for the task's current package; older packages' jobs are invalidated."""
    checksum = dir_checksum(task_dir)
    store.invalidate_jobs(task_id, keep_checksum=checksum)
    made = []
    for agent_id in agents:
        for attempt in range(1, k + 1):
            job = BenchmarkJob(job_id=stable_id(task_id, checksum, agent_id, str(attempt), n=20), run_id=run_id,
                               task_id=task_id, proposal_id=proposal_id, task_dir=str(task_dir),
                               package_dir=str(package_dir) if package_dir else None, package_checksum=checksum,
                               agent_id=agent_id, attempt=attempt, budget=budget or JobBudget())
            if store.enqueue(job):
                made.append(job.job_id)
    return made


# --------------------------------------------------------------------------- backends


@dataclass
class RunOutcome:
    reward: float | None
    passed: int | None = None
    total: int | None = None
    cost: float | None = None
    logs: str | None = None
    error: str | None = None


class HarnessBackend:
    """The factory's harness on the store package (web off, egress allowlist)."""

    name = "harness"

    def __init__(self, cfg, out_root: Path) -> None:
        self.cfg = cfg
        self.out_root = Path(out_root)

    def run(self, job: BenchmarkJob) -> RunOutcome:
        from ..harness.agents import get_agent
        from ..harness.runner import INFRA_CATEGORIES, Harness
        from ..sandbox import make_factory

        if not job.package_dir:
            raise FactoryError(f"job {job.job_id} has no store package; use the harbor backend")
        agent_name, model = split_agent(job.agent_id)
        agent = get_agent(agent_name, self.cfg.harness.agents)
        harness = Harness(make_factory(self.cfg), self.cfg, self.out_root)
        try:
            summary_box: list = []
            harness.run([Path(job.package_dir)], agent, model=model, k=1, on_result=summary_box.append, workers=1)
        finally:
            harness.close()
        if not summary_box:
            return RunOutcome(None, error="the harness returned no attempt")
        a = summary_box[-1]
        if a.category in INFRA_CATEGORIES:
            return RunOutcome(None, error=f"{a.category}", cost=a.cost_usd)
        if a.resolved:
            reward = 1.0
        elif a.p2p_failed or not a.f2p_total:
            reward = 0.0
        else:
            reward = round(a.f2p_passed / a.f2p_total, 4)
        return RunOutcome(reward, a.f2p_passed, a.f2p_total, a.cost_usd, str(self.out_root))


class FakeBackend:
    """Tests: rewards by agent name (oracle 1.0, nop 0.0, crash None)."""

    name = "fake"

    def __init__(self, rewards: dict[str, float | None] | None = None) -> None:
        self.rewards = rewards or {"oracle": 1.0, "nop": 0.0, "crash": None}
        self.calls: list[str] = []

    def run(self, job: BenchmarkJob) -> RunOutcome:
        self.calls.append(job.job_id)
        agent, _ = split_agent(job.agent_id)
        reward = self.rewards.get(agent, 0.0)
        return RunOutcome(reward, error=None if reward is not None else "crashed")


# --------------------------------------------------------------------------- worker


class Worker:
    def __init__(self, store: ReferenceStore, backend, worker_id: str | None = None, lease_s: float = 300.0,
                 backoff_s: float = 30.0, run_id: str | None = None, cost_cap_usd: float | None = None) -> None:
        self.store = store
        self.backend = backend
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident() % 10000}"
        self.lease_s = lease_s
        self.backoff_s = backoff_s
        self.run_id = run_id
        self.cost_cap_usd = cost_cap_usd

    def _beat(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.lease_s / 3):
            if not self.store.heartbeat(job_id, self.worker_id, self.lease_s):
                return

    def _over_budget(self) -> bool:
        if self.cost_cap_usd is None:
            return False
        spent = sum(r.cost or 0.0 for r in self.store.list_results(self.run_id))
        if spent >= self.cost_cap_usd:
            self.store.set_kill(True, f"cost cap {self.cost_cap_usd} reached ({spent:.2f})")
            return True
        return False

    def run_one(self) -> BenchmarkResult | str | None:
        """Lease and run one job. Returns the result, "retry"/"failed" for infra errors, None when idle."""
        if self.store.killed() or self._over_budget():
            return None
        job = self.store.lease(self.worker_id, self.lease_s, run_id=self.run_id)
        if job is None:
            return None
        if Path(job.task_dir).exists() and dir_checksum(Path(job.task_dir)) != job.package_checksum:
            self.store.invalidate_jobs(job.task_id, keep_checksum=dir_checksum(Path(job.task_dir)))
            return "invalidated"
        stop = threading.Event()
        beat = threading.Thread(target=self._beat, args=(job.job_id, stop), daemon=True)
        beat.start()
        start = time.monotonic()
        try:
            out = self.backend.run(job)
        except Exception as exc:  # noqa: BLE001 - any crash of the backend is an infrastructure error
            log.exception("job %s crashed", job.job_id)
            out = RunOutcome(None, error=f"{type(exc).__name__}: {exc}")
        finally:
            stop.set()
            beat.join(timeout=5)
        wall = round(time.monotonic() - start, 1)
        result = BenchmarkResult(job_id=job.job_id, run_id=job.run_id, task_id=job.task_id, agent_id=job.agent_id,
                                 attempt=job.attempt, package_checksum=job.package_checksum, reward=out.reward,
                                 outcome=outcome_of(out.reward), feature_tests_passed=out.passed,
                                 feature_tests_total=out.total, cost=out.cost, wall_time_s=wall, logs=out.logs)
        if out.reward is None:
            state = self.store.infra_error(job.job_id, out.error or "no reward", self.backoff_s, result)
            log.warning("job %s infra error (%s): %s", job.job_id, state.value, out.error)
            return "failed" if state == JobState.RUN_FAILED else "retry"
        self.store.complete(job.job_id, result)
        log.info("job %s %s: reward %s", job.job_id, result.outcome, result.reward)
        return result

    def run_until_empty(self, poll_s: float = 5.0, idle_exit: bool = True, on_result=None) -> int:
        """Work until the queue has nothing runnable (waiting out backoffs), the kill switch, or forever."""
        done = 0
        while True:
            r = self.run_one()
            if r is None:
                if self.store.killed():
                    return done
                pending = self.store.list_jobs(self.run_id, [JobState.QUEUED, JobState.RUNNING])
                if idle_exit and not pending:
                    return done
                time.sleep(poll_s)
                continue
            done += 1
            if on_result:
                on_result(r)


def summary(results: list[BenchmarkResult], jobs: list[BenchmarkJob]) -> dict:
    """Solved / partial / failed (of N), infra errors apart, per agent."""
    by_agent: dict[str, dict] = {}
    for r in results:
        a = by_agent.setdefault(r.agent_id, {"solved": 0, "partial": 0, "failed": 0, "infra_error": 0, "n": 0,
                                             "cost": 0.0, "wall_time_s": 0.0})
        a[r.outcome] += 1
        a["n"] += 1
        a["cost"] += r.cost or 0.0
        a["wall_time_s"] += r.wall_time_s
    pending = sum(1 for j in jobs if j.state in {JobState.QUEUED, JobState.RUNNING})
    tasks = sorted({r.task_id for r in results})
    solved_by = {t: {r.agent_id for r in results if r.task_id == t and r.outcome == "solved"} for t in tasks}
    agents = set(by_agent)
    return {"agents": by_agent, "pending_jobs": pending,
            "nobody_solved": [t for t, s in solved_by.items() if not s],
            "everybody_solved": [t for t, s in solved_by.items() if agents and s == agents and len(agents) > 1]}
