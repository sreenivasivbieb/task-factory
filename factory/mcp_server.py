"""MCP server: drive the Task Factory from Claude Code (or any MCP client).

    claude mcp add task-factory -- uv run --extra mcp factory-mcp      # or the project's .mcp.json

The client is the factory's OPERATOR: it runs the pipeline, writes task drafts with its own file
tools, sends them through the gates, reviews tasks and reads the evidence. Validity is never the
client's opinion: a draft becomes a task only when the executed gates pass it.

Never connect this server to a model that is being BENCHMARKED: these tools read hidden tests and
reference solutions. Models under test run inside the harness sandbox, which has no MCP access.

Long steps (pipeline, gates, benchmark) run as background jobs: a tool returns a job id at once and
``job_status`` shows progress and the end result.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - optional extra
    FastMCP = None

ROOT = Path(os.environ.get("TASK_FACTORY_HOME") or Path(__file__).resolve().parents[1])
JOBS_DIR = ROOT / "workdir" / ".mcp-jobs"
DEFAULT_PIPELINE = "configs/hackathon.toml"
DEFAULT_WORKDIR = "workdir/pipeline/toyshop"

INSTRUCTIONS = """\
Task Factory turns repositories into validated coding tasks (instruction + hidden tests + reference
solution + pinned environment) and benchmarks AI coding agents on them.

Typical flows:
- Everything end to end: run_pipeline() -> job_status(job_id) until done -> read_report().
- Write a new task yourself: scaffold_task() -> write requirements.md, after/<changed files>,
  tests/<hidden tests> with your file tools -> validate_draft() -> job_status() -> if rejected, read
  the gate details, fix the draft, validate again. Only executed gates decide validity.
- Review: list_tasks() -> show_task() -> review_task(approve | delete with a reason).
- Benchmark: run_benchmark(agent, model) -> job_status() -> read_report().

Good tasks are realistic feature work, not puzzles: requirements name every new public API (G7
rejects tests that call unnamed names), the hidden tests fail without the change (G3) and catch
plausible wrong solutions (G6 mutants). Do not weaken tests to get a draft through: change the
requirements or the reference instead.
"""

_procs: dict[str, subprocess.Popen] = {}


# --------------------------------------------------------------------------- jobs


_RUNNER = """
import json, subprocess, sys
for argv in json.loads(sys.argv[1]):
    print("$ factory " + " ".join(argv), flush=True)
    code = subprocess.call([sys.executable, "-m", "factory", *argv])
    if code:
        sys.exit(code)
"""


def _start(argv: list[str] | list[list[str]], title: str) -> dict:
    """Start one ``factory`` command, or several in order (stopping at the first failure), in the background."""
    steps = argv if argv and isinstance(argv[0], list) else [argv]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    log = JOBS_DIR / f"{job_id}.log"
    fh = log.open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", _RUNNER, json.dumps(steps)], cwd=ROOT, stdout=fh,
                            stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    _procs[job_id] = proc
    meta = {"job_id": job_id, "title": title, "command": " && ".join("factory " + shlex.join(a) for a in steps),
            "pid": proc.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "log": str(log)}
    (JOBS_DIR / f"{job_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {**meta, "status": "running", "next": f"job_status('{job_id}')"}


def _job(job_id: str) -> dict:
    meta_path = JOBS_DIR / f"{job_id}.json"
    if not meta_path.exists():
        raise ValueError(f"unknown job {job_id}; see list_jobs()")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    proc = _procs.get(job_id)
    if proc is not None:
        code = proc.poll()
        if code is not None and "exit_code" not in meta:
            meta["exit_code"] = code
            meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if "exit_code" in meta:
        meta["status"] = "succeeded" if meta["exit_code"] == 0 else f"failed (exit {meta['exit_code']})"
    elif proc is None:
        meta["status"] = "unknown (started by another server process)"
    else:
        meta["status"] = "running"
    return meta


def _tail(path: str, lines: int) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def _wd_args(workdir: str, config: str | None) -> list[str]:
    args = ["--workdir", workdir]
    if config:
        args += ["--config", config]
    return args


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- server


def build_server():
    if FastMCP is None:
        raise SystemExit("the MCP server needs the 'mcp' extra: uv sync --extra mcp  (or pip install 'mcp>=1.2,<2')")
    mcp = FastMCP("task-factory", instructions=INSTRUCTIONS)

    @mcp.tool()
    def factory_overview(pipeline_file: str = DEFAULT_PIPELINE) -> dict:
        """What exists now: repositories of a pipeline, tasks per review status, benchmark runs, report summary."""
        from .pipeline import PipelineConfig

        pc = PipelineConfig.load(ROOT / pipeline_file)
        out: dict = {"pipeline": pipeline_file, "out": pc.out, "repos": {}, "benchmark_agents": pc.benchmark.agents}
        for job in pc.repos:
            wd = ROOT / pc.root / job.name
            info: dict = {"workdir": str(Path(pc.root) / job.name), "source": job.source,
                          "loaded": (wd / "repo_spec.json").exists(), "inspected": (wd / "env_config.json").exists()}
            if (wd / "tasks").exists():
                from .review.store import TaskStore

                info["tasks"] = TaskStore(wd / "tasks").summary()["counts"]
            out["repos"][job.name] = info
        runs = ROOT / pc.root / "_bench" / "harness"
        if runs.exists():
            out["harness_runs"] = []
            for r in sorted(runs.iterdir()):
                if (r / "summary.json").exists():
                    s = json.loads((r / "summary.json").read_text(encoding="utf-8"))
                    out["harness_runs"].append({k: s.get(k) for k in ("run_id", "agent", "model", "tasks",
                                                                      "resolved_at_k", "pass_rate", "categories")})
        summary = ROOT / pc.out / "evidence" / "summary.json"
        if summary.exists():
            out["evidence_summary"] = json.loads(summary.read_text(encoding="utf-8"))
        return out

    @mcp.tool()
    def run_pipeline(pipeline_file: str = DEFAULT_PIPELINE, only: list[str] | None = None,
                     skip: list[str] | None = None, force: bool = False, backend: str | None = None) -> dict:
        """Start the end-to-end pipeline as a background job (tasks -> gates -> self-test -> publish ->
        oracle/noop -> benchmark -> REPORT.md). Steps: tasks, selftest, publish, sanity, benchmark, evidence.
        Returns a job id: poll job_status(job_id)."""
        argv = ["pipeline", pipeline_file]
        for s in only or []:
            argv += ["--only", s]
        for s in skip or []:
            argv += ["--skip", s]
        argv += ["--force"] if force else []
        argv += ["--backend", backend] if backend else []
        return _start(argv, f"pipeline {pipeline_file}")

    @mcp.tool()
    def job_status(job_id: str, tail: int = 60) -> dict:
        """Status of a background job, with the last lines of its log."""
        meta = _job(job_id)
        meta["log_tail"] = _tail(meta["log"], tail)
        return meta

    @mcp.tool()
    def list_jobs(limit: int = 20) -> list[dict]:
        """Recent background jobs, newest first."""
        if not JOBS_DIR.exists():
            return []
        ids = sorted((p.stem for p in JOBS_DIR.glob("*.json")), reverse=True)[:limit]
        return [{k: v for k, v in _job(i).items() if k in ("job_id", "title", "status", "started")} for i in ids]

    @mcp.tool()
    def cancel_job(job_id: str) -> dict:
        """Stop a running background job."""
        proc = _procs.get(job_id)
        if proc is None or proc.poll() is not None:
            return _job(job_id)
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        return _job(job_id)

    @mcp.tool()
    def prepare_repo(source: str, workdir: str = DEFAULT_WORKDIR, commit: str | None = None,
                     config: str | None = None) -> dict:
        """Pin a repository (local path, git URL or zip) and build its environment + baseline test run.
        Background job; required once per workdir before scaffold_task / validate_draft."""
        load = ["load", "--repo", source, *(["--commit", commit] if commit else []), *_wd_args(workdir, config)]
        return _start([load, ["inspect", *_wd_args(workdir, config)]], f"prepare {source}")

    @mcp.tool()
    def repo_context(workdir: str = DEFAULT_WORKDIR, max_files: int = 200) -> dict:
        """The pinned repository: path of its clean copy, language, test command, files, baseline tests.
        Read the source with your own file tools at `repo_path` before writing a task."""
        wd = ROOT / workdir
        spec = json.loads((wd / "repo_spec.json").read_text(encoding="utf-8"))
        env = json.loads((wd / "env_config.json").read_text(encoding="utf-8")) if (wd / "env_config.json").exists() else {}
        repo = Path(spec["local_path"])
        files = sorted(str(p.relative_to(repo)) for p in repo.rglob("*")
                       if p.is_file() and ".git" not in p.parts)[:max_files]
        return {"repo_path": str(repo), "name": spec["name"], "commit": spec["commit_sha"],
                "language": spec.get("language"), "test_command": env.get("test_command"),
                "setup_commands": env.get("setup_commands"), "files": files}

    @mcp.tool()
    def scaffold_task(name: str, title: str, workdir: str = DEFAULT_WORKDIR, kind: str = "feature",
                      tags: list[str] | None = None) -> dict:
        """Create an empty task draft folder. Then write, with your file tools:
        requirements.md (what the solver sees: name every new public API, defaults, errors),
        after/<repo path> (the complete changed or new source files = the reference solution),
        tests/<repo path> (the hidden tests; they must fail without the change).
        Finally call validate_draft(path)."""
        from .review.authoring import scaffold_draft

        path = scaffold_draft(ROOT / workdir / "drafts" / name, title, kind, tags or [], "claude-code (mcp)")
        task_json = path / "task.json"
        meta = json.loads(task_json.read_text(encoding="utf-8"))
        meta["author"] = "claude-code (mcp)"
        task_json.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {"draft": str(path), "files": sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()),
                "how_to": (path / "HOW_TO.md").read_text(encoding="utf-8") if (path / "HOW_TO.md").exists() else ""}

    @mcp.tool()
    def validate_draft(draft: str, workdir: str = DEFAULT_WORKDIR, config: str | None = "configs/factory.strict.toml",
                       approve: bool = False) -> dict:
        """Run every gate (G0 baseline, G1 applies, G7 spec, G2 passes, G3 fails without, G4 no regression,
        G6 mutants, G5 stable) on a draft folder. Valid drafts enter the task store (pending unless approve).
        Background job: job_status() shows each gate; gate_report() explains a rejection."""
        argv = ["tasks", "add", draft, *_wd_args(workdir, config)] + ([] if approve else ["--pending"])
        return _start(argv, f"gates on {Path(draft).name}")

    @mcp.tool()
    def gate_report(task_id: str, workdir: str = DEFAULT_WORKDIR) -> dict:
        """The validation report of a task or a rejected draft: every gate, its details, mutants, log paths."""
        runs = ROOT / workdir / "runs"
        matches = sorted(runs.glob(f"*{task_id}*/report.json")) if runs.exists() else []
        if not matches:
            pkg = ROOT / workdir / "tasks" / task_id / "validation" / "report.json"
            matches = [pkg] if pkg.exists() else []
        if not matches:
            raise ValueError(f"no validation report for {task_id} under {workdir}")
        return json.loads(matches[-1].read_text(encoding="utf-8"))

    @mcp.tool()
    def list_tasks(workdir: str = DEFAULT_WORKDIR, status: str = "all") -> list[dict]:
        """Tasks in a store with review status, kind, tags, author, F2P count and mutation score."""
        from .review.store import TaskStore

        return [{"task_id": e.task_id, "status": e.status, "kind": e.meta.kind, "tags": e.meta.tags,
                 "author": e.author, "fail_to_pass": len(e.meta.fail_to_pass),
                 "touched_files": e.meta.touched_files, "mutation_score": e.meta.validation.mutation_score,
                 "last_reason": e.last_reason}
                for e in TaskStore(ROOT / workdir / "tasks").entries(status)]

    @mcp.tool()
    def show_task(task_id: str, workdir: str = DEFAULT_WORKDIR, section: str = "all") -> dict:
        """One task package: instruction (what the solver sees), reference patch, hidden tests, gate results.
        section: all | instruction | patch | tests | gates."""
        from .review.store import TaskStore

        e = TaskStore(ROOT / workdir / "tasks").get(task_id)
        pkg = e.path
        out: dict = {"task_id": e.task_id, "status": e.status, "path": str(pkg)}
        if section in ("all", "instruction"):
            out["instruction"] = (pkg / "instruction.md").read_text(encoding="utf-8")
        if section in ("all", "patch"):
            out["patch"] = (pkg / "solution" / "solution.diff").read_text(encoding="utf-8")
        if section in ("all", "tests"):
            out["tests"] = {str(p.relative_to(pkg / "tests" / "files")): p.read_text(encoding="utf-8")
                            for p in sorted((pkg / "tests" / "files").rglob("*")) if p.is_file()}
        if section in ("all", "gates"):
            rp = pkg / "validation" / "report.json"
            out["gates"] = json.loads(rp.read_text(encoding="utf-8")).get("gates") if rp.exists() else None
        return out

    @mcp.tool()
    def review_task(task_id: str, decision: str, reason: str = "", workdir: str = DEFAULT_WORKDIR) -> dict:
        """Approve a task, or delete it (decision = approve | delete; delete needs a reason, which the
        report discloses). Only approved tasks are published."""
        from .review.store import TaskStore

        store = TaskStore(ROOT / workdir / "tasks", reviewer="claude-code (mcp)")
        if decision == "approve":
            e = store.approve(task_id, reason or None)
        elif decision == "delete":
            if not reason.strip():
                raise ValueError("a reason is required to delete a task")
            e = store.delete(task_id, reason)
        else:
            raise ValueError("decision must be 'approve' or 'delete'")
        return {"task_id": e.task_id, "status": e.status}

    @mcp.tool()
    def run_benchmark(agent: str, model: str | None = None, tasks: str = "submission/tasks", k: int = 1,
                      workdir: str = "workdir/pipeline/_bench", timeout_s: int | None = None) -> dict:
        """Run an AI coding agent on published tasks in isolated sandboxes, graded by hidden tests.
        agent: oracle | noop | task-factory | claude-code | codex | aider | mini-swe-agent.
        Background job; then build_evidence() / read_report()."""
        argv = ["harness", "run", "--agent", agent, "--tasks", tasks, "--k", str(k), "--workdir", workdir]
        argv += ["--model", model] if model else []
        argv += ["--timeout", str(timeout_s)] if timeout_s else []
        return _start(argv, f"benchmark {agent}{':' + model if model else ''}")

    @mcp.tool()
    def build_evidence(pipeline_file: str = DEFAULT_PIPELINE) -> dict:
        """Recompute REPORT.md and the evidence files (validation matrix, diversity, leaderboard) from logs."""
        from . import evidence
        from .pipeline import PipelineConfig

        pc = PipelineConfig.load(ROOT / pipeline_file)
        root, out = ROOT / pc.root, ROOT / pc.out
        return evidence.build(tasks=[out / "tasks"], harness=[root / "_bench" / "harness"], out=out,
                              rejections=[root / j.name / "rejections.jsonl" for j in pc.repos],
                              selftests=[root / j.name / "selftest.json" for j in pc.repos],
                              reproduce=f"uv run factory pipeline {pipeline_file}")

    @mcp.tool()
    def read_report(pipeline_file: str = DEFAULT_PIPELINE) -> str:
        """The current REPORT.md (headline numbers, validity matrix, variation, leaderboard, failure modes)."""
        from .pipeline import PipelineConfig

        pc = PipelineConfig.load(ROOT / pipeline_file)
        p = ROOT / pc.out / "REPORT.md"
        return p.read_text(encoding="utf-8") if p.exists() else "no report yet: run_pipeline() or build_evidence()"

    @mcp.prompt()
    def author_task(feature: str, workdir: str = DEFAULT_WORKDIR) -> str:
        """Write one realistic feature task for the pinned repository and get it through every gate."""
        return f"""You are writing a coding task for evaluating AI coding agents, in the Task Factory at {ROOT}.

Feature to turn into a task: {feature}

1. repo_context(workdir="{workdir}") and read the relevant source with your file tools.
2. scaffold_task(...) to create a draft.
3. Write requirements.md as a product request a maintainer would accept: behaviour, defaults, edge
   cases, errors; name every new public function, class, constant and module path the tests use.
4. Write the reference solution as complete files under after/ (repo-relative paths). Keep it a focused
   change, preferably touching 2+ files when the feature needs it. Never change test files there.
5. Write hidden tests under tests/ that fail without the change, pass with it, and would catch a
   plausible wrong solution (boundaries, error branches, interactions with existing features).
6. validate_draft(draft) and poll job_status(). If a gate rejects it, read gate_report() and fix the
   cause (spec too vague -> requirements; mutant survived -> stronger assertions; regression -> reference).
   Never weaken a test only to pass a gate.
7. When it is valid, show me the task (show_task) and wait for my approval before review_task(approve).
"""

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
