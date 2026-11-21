"""Coding harness: run an agent on task packages and score it with the hidden tests.

Per task and attempt (design doc section 16):

1. Start a fresh sandbox from the task's starting commit (+ the agent installed),
   with network so the agent can reach its model API. Hidden tests and the
   reference patch are NOT in this sandbox.
2. Give the agent the instruction only. Let it edit the repo. Capture its diff.
3. Start a second, fresh sandbox with no network. Apply the agent's diff, put
   back any existing test file it changed, copy in the hidden tests, run them.
4. Resolved = every fail_to_pass test passes and every pass_to_pass test passes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from pydantic import Field

from ..config import FactoryConfig
from ..errors import ConfigError
from ..models import EnvConfig, Model, RepoSpec, TaskPackage
from ..packaging.verify import package_repo, read_package
from ..runner.test_runner import run_tests
from ..sandbox.base import BASE_TAG, SCRATCH_DIR, Sandbox, SandboxFactory
from .agents import AgentSpec
from .protect import RESTORE_SH

log = logging.getLogger("factory.harness")

INSTRUCTION_PATH = f"{SCRATCH_DIR}/instruction.md"
Category = str  # resolved | wrong_behaviour | regression | build_error | no_change | agent_error | timeout
#                  | patch_apply_fail | leak

# Outcomes that say nothing about the model's skill (setup, quota or harness problems). They do not
# count as "unsolved", and `harness work` keeps the assignment open so the next run retries it.
INFRA_CATEGORIES = {"agent_error", "leak"}
# Stop a run after this many agent errors in a row (an exhausted quota or a wrong model ID fails
# every task the same way; continuing only burns time).
MAX_CONSECUTIVE_AGENT_ERRORS = 2


class AttemptResult(Model):
    task_id: str
    agent: str
    model: str | None = None
    attempt: int
    resolved: bool
    category: Category
    f2p_passed: int = 0
    f2p_total: int = 0
    p2p_failed: list[str] = Field(default_factory=list)
    tests_tampered: list[str] = Field(default_factory=list)
    agent_exit_code: int | None = None
    agent_timed_out: bool = False
    agent_duration_s: float = 0.0
    grade_duration_s: float = 0.0
    diff_lines: int = 0
    cost_usd: float | None = None
    tokens: int | None = None
    branch: str | None = None  # new git branch holding the passing solution (resolved attempts only)
    commit: str | None = None


class TaskScore(Model):
    task_id: str
    attempts: int
    resolved: int
    difficulty: str  # easy (all attempts) | medium (some) | unsolved (none) | error (only infra failures)


class HarnessSummary(Model):
    run_id: str
    agent: str
    model: str | None = None
    k: int
    tasks: int
    resolved_at_k: float  # share of tasks resolved in at least one attempt
    pass_rate: float  # share of all attempts that resolved
    mean_agent_time_s: float
    total_cost_usd: float | None = None
    per_task: list[TaskScore] = Field(default_factory=list)
    categories: dict[str, int] = Field(default_factory=dict)
    stopped_early: str | None = None  # why the run stopped before every task was attempted


def _usage(stdout: str) -> tuple[float | None, int | None]:
    """Best effort: cost and tokens from a JSON result (Claude Code prints one with --output-format json)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        cost = data.get("total_cost_usd") or data.get("cost_usd")
        usage = data.get("usage") or {}
        tokens = sum(v for k, v in usage.items() if isinstance(v, int) and "tokens" in k) or None
        return (float(cost) if cost is not None else None), tokens
    return None, None


def host_auth_payload(agent: AgentSpec, model: str | None) -> bytes:
    """The user's stored login for the model's provider only (e.g. ``nvidia`` from ``nvidia/...``)."""
    if not agent.host_auth_file or not agent.sandbox_auth_file:
        raise ConfigError(f"agent '{agent.name}' has no host login file to use")
    path = Path(os.path.expanduser(agent.host_auth_file))
    if not path.exists():
        raise ConfigError(f"{path} not found; log in with the agent first (for OpenCode: opencode auth login)")
    provider = (model or agent.default_model or "").split("/", 1)[0]
    if not provider:
        raise ConfigError("--model provider/model is needed to pick the stored login")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON") from exc
    if provider not in data:
        raise ConfigError(f"no stored login for provider '{provider}' in {path}; stored: {', '.join(sorted(data))}")
    return json.dumps({provider: data[provider]}).encode("utf-8")


class Harness:
    def __init__(self, factory: SandboxFactory, cfg: FactoryConfig, out_dir: Path, *,
                 use_host_auth: bool = False, solutions=None) -> None:
        self.factory = factory
        self.cfg = cfg
        self.out_dir = Path(out_dir)
        self.use_host_auth = use_host_auth
        self.solutions = solutions  # SolutionRepos or None
        self._specs: dict[tuple[str, str], RepoSpec] = {}
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-harness-")

    def close(self) -> None:
        self._tmp.cleanup()

    def _spec(self, meta: TaskPackage, pkg: Path) -> RepoSpec:
        key = (meta.repo, meta.commit_sha)
        if key not in self._specs:
            self._specs[key] = package_repo(pkg, meta, Path(self._tmp.name) / str(len(self._specs)))
        return self._specs[key]

    def run(self, packages: list[Path], agent: AgentSpec, *, model: str | None = None, k: int = 1,
            on_result=None) -> HarnessSummary:
        from ..sandbox.local_backend import LocalSandboxFactory

        from .agents import BUILTIN

        if isinstance(self.factory, LocalSandboxFactory) and agent.kind == "command" and agent.name in BUILTIN:
            raise ConfigError(f"agent '{agent.name}' must run in Docker (the local backend has no isolation)")
        auth = host_auth_payload(agent, model) if (self.use_host_auth and agent.host_auth_file) else None
        missing = [s for s in agent.secrets if not os.environ.get(s)]
        if auth is None and agent.secrets and len(missing) == len(agent.secrets):
            raise ConfigError(f"agent '{agent.name}' needs one of these environment variables: "
                              f"{', '.join(agent.secrets)}")
        run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{agent.name}"
        run_dir = self.out_dir / run_id
        results: list[AttemptResult] = []
        consecutive_errors, stopped = 0, None
        for pkg in packages:
            if stopped:
                break
            meta, draft = read_package(pkg)
            env = EnvConfig.load(Path(pkg) / "environment" / "env.json")
            spec = self._spec(meta, Path(pkg))
            base = self.factory.build_image(spec, env)
            agent_image = self.factory.build_derived_image(base, agent.name, agent.install)
            for attempt in range(1, k + 1):
                adir = run_dir / meta.task_id / f"attempt-{attempt}"
                adir.mkdir(parents=True, exist_ok=True)
                res = self._attempt(meta, draft, env, base, agent_image, agent, model, attempt, adir, auth)
                if res.resolved and self.solutions is not None and agent.kind == "command":
                    clean = (adir / "solution.diff").read_text(encoding="utf-8")
                    tar = Path(pkg) / "environment" / "repo.tar"
                    res.branch, res.commit = self.solutions.record(
                        meta, clean, agent=agent.name, model=res.model, attempt=attempt, run_id=run_id,
                        f2p=f"{res.f2p_passed}/{res.f2p_total}",
                        p2p=f"{len(meta.pass_to_pass) - len(res.p2p_failed)}/{len(meta.pass_to_pass)}",
                        title=_title(draft.requirements_md, meta.task_id), archive=tar if tar.is_file() else None)
                res.save(adir / "grade.json")
                with (run_dir / "results.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(res.model_dump_json() + "\n")
                results.append(res)
                if on_result:
                    on_result(res)
                consecutive_errors = consecutive_errors + 1 if res.category == "agent_error" else 0
                if consecutive_errors >= MAX_CONSECUTIVE_AGENT_ERRORS:
                    stopped = (f"{consecutive_errors} agent errors in a row (quota, API key or model ID?); "
                               f"see {adir / 'agent.log.json'}")
                    log.warning("stopping the run: %s", stopped)
                    break
        summary = summarize(run_id, agent.name, model, k, results)
        summary.stopped_early = stopped
        summary.save(run_dir / "summary.json")
        return summary

    # ------------------------------------------------------------------ one attempt

    def _attempt(self, meta, draft, env: EnvConfig, base, agent_image, agent: AgentSpec, model, attempt,
                 adir: Path, auth: bytes | None = None) -> AttemptResult:
        res = AttemptResult(task_id=meta.task_id, agent=agent.name, model=model or agent.default_model,
                            attempt=attempt, resolved=False, category="no_change", f2p_total=len(meta.fail_to_pass))
        # ---- 1+2: agent works in its own sandbox; hidden files are never there
        with self.factory.new_sandbox(agent_image, network=agent.network) as sb:
            leaked = self._leaks(sb, draft)
            if leaked:
                res.category = "leak"
                (adir / "leak.txt").write_text("\n".join(leaked), encoding="utf-8")
                return res
            start = time.monotonic()
            if agent.kind == "oracle":
                out = sb.apply_patch(draft.solution_patch)
            elif agent.kind == "noop":
                out = sb.exec(["true"], timeout_s=10)
            else:
                if auth is not None:
                    # the user's login for this provider only; the container is removed after the attempt
                    sb.write_file(agent.sandbox_auth_file, auth)
                    sb.exec(["chmod", "600", agent.sandbox_auth_file], timeout_s=10)
                sb.write_file(INSTRUCTION_PATH, draft.requirements_md)
                secrets = {s: os.environ[s] for s in agent.secrets if os.environ.get(s)}
                out = sb.exec(["sh", "-c", agent.command(INSTRUCTION_PATH, model)], timeout_s=agent.timeout_s,
                              env={**env.env_vars, **agent.env} or None, secret_env=secrets or None)
            res.agent_duration_s = round(time.monotonic() - start, 2)
            res.agent_exit_code, res.agent_timed_out = out.exit_code, out.timed_out
            res.cost_usd, res.tokens = _usage(out.stdout)
            (adir / "agent.log.json").write_text(out.model_dump_json(indent=2), encoding="utf-8")
            diff_cmd = f"git add -A && git diff --cached --binary {BASE_TAG} > {SCRATCH_DIR}/agent.diff"
            sb.exec(["sh", "-c", diff_cmd], timeout_s=120)
            try:
                diff = sb.read_file(f"{SCRATCH_DIR}/agent.diff").decode("utf-8", errors="replace")
            except FileNotFoundError:
                diff = ""
        (adir / "agent.diff").write_text(diff, encoding="utf-8")
        res.diff_lines = sum(1 for ln in diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
        if res.agent_timed_out:
            res.category = "timeout"
        if not diff.strip():
            if res.agent_timed_out:
                res.category = "timeout"
            elif res.agent_exit_code not in (0, None):
                res.category = "agent_error"  # e.g. bad model ID or API key: see agent.log.json
            else:
                res.category = "no_change"
            return res
        # ---- 3: grade in a fresh, offline sandbox
        start = time.monotonic()
        with self.factory.new_sandbox(base) as sb:
            applied = sb.apply_patch(diff)
            if not applied.ok:
                res.category = "patch_apply_fail"
                return res
            sb.exec(["sh", "-c", f"mkdir -p {SCRATCH_DIR}"], timeout_s=10)
            restored = sb.exec(["sh", "-c", RESTORE_SH], timeout_s=120)
            res.tests_tampered = [ln for ln in restored.stdout.splitlines() if ln.strip()]
            # the exact change that is graded: the agent's diff minus restored test files
            sb.exec(["sh", "-c", f"git add -A && git diff --cached --binary {BASE_TAG} > {SCRATCH_DIR}/clean.diff "
                                 "&& git reset -q"], timeout_s=120)
            try:
                (adir / "solution.diff").write_bytes(sb.read_file(f"{SCRATCH_DIR}/clean.diff"))
            except FileNotFoundError:
                (adir / "solution.diff").write_text("", encoding="utf-8")
            for path, content in draft.test_files.items():
                sb.write_file(path, content)
            feature = run_tests(sb, env, sorted(draft.test_files), log_to=adir / "grade-feature.json")
            full = run_tests(sb, env, None, log_to=adir / "grade-full.json")
        res.grade_duration_s = round(time.monotonic() - start, 2)
        f_status, p_status = feature.status_of(), full.status_of()
        res.f2p_passed = sum(1 for t in meta.fail_to_pass if f_status.get(t) == "passed")
        res.p2p_failed = sorted(t for t in meta.pass_to_pass if p_status.get(t) != "passed")
        if feature.crashed:
            res.category = "build_error"
        elif res.f2p_passed < len(meta.fail_to_pass):
            res.category = "wrong_behaviour"
        elif res.p2p_failed:
            res.category = "regression"
        else:
            res.category, res.resolved = "resolved", True
        return res

    @staticmethod
    def _leaks(sb: Sandbox, draft) -> list[str]:
        """Hidden test files must not exist in the agent's sandbox with their hidden content."""
        leaked = []
        for path, content in draft.test_files.items():
            try:
                if sb.read_file(path).decode("utf-8", errors="replace") == content:
                    leaked.append(path)
            except FileNotFoundError:
                continue
        for probe in ("solution/solution.diff", "solution.diff"):
            try:
                if sb.read_file(probe).decode("utf-8", errors="replace").strip() == draft.solution_patch.strip():
                    leaked.append(probe)
            except FileNotFoundError:
                continue
        return leaked


def _title(instruction: str, fallback: str) -> str:
    for line in instruction.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def summarize(run_id: str, agent: str, model: str | None, k: int, results: list[AttemptResult]) -> HarnessSummary:
    by_task: dict[str, list[AttemptResult]] = {}
    for r in results:
        by_task.setdefault(r.task_id, []).append(r)
    per_task = []
    for task_id, rs in by_task.items():
        n = sum(r.resolved for r in rs)
        if not n and all(r.category in INFRA_CATEGORIES for r in rs):
            difficulty = "error"  # the model never really tried: not evidence that the task is hard
        else:
            difficulty = "easy" if n == len(rs) else ("medium" if n else "unsolved")
        per_task.append(TaskScore(task_id=task_id, attempts=len(rs), resolved=n, difficulty=difficulty))
    categories: dict[str, int] = {}
    for r in results:
        categories[r.category] = categories.get(r.category, 0) + 1
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    return HarnessSummary(
        run_id=run_id, agent=agent, model=model, k=k, tasks=len(by_task),
        resolved_at_k=round(sum(t.resolved > 0 for t in per_task) / len(per_task), 3) if per_task else 0.0,
        pass_rate=round(sum(r.resolved for r in results) / len(results), 3) if results else 0.0,
        mean_agent_time_s=round(sum(r.agent_duration_s for r in results) / len(results), 2) if results else 0.0,
        total_cost_usd=round(sum(costs), 4) if costs else None, per_task=per_task, categories=categories)

