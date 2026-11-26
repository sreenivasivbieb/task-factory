"""One command, end to end: ``factory pipeline configs/hackathon.toml``.

Generate -> Validate -> Benchmark -> Report, over several repositories, resumable:

1. per repository: load + pin, inspect (build the environment, baseline run), add every task draft
   through the gates, optionally let models produce more tasks, and run the gate self-test
   (known-bad tasks must be rejected with the expected code);
2. publish every approved task to ``<out>/tasks`` (native packages) and ``<out>/harbor``;
3. sanity: the ``oracle`` agent (applies the reference) must resolve 100 %, ``noop`` 0 %;
4. benchmark every ``agent[:model]`` in ``[pipeline.benchmark] agents`` with hidden tests;
5. ``<out>/REPORT.md`` plus machine-readable evidence (``factory.evidence``).

Every step reads and writes files under ``[pipeline] root``, so a rerun skips finished work
(``--force`` redoes it). Each step is a normal ``factory`` command run as a subprocess, so the
log shows exactly what to type to redo one step by hand.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .errors import ConfigError

log = logging.getLogger("factory.pipeline")

STEPS = ("tasks", "selftest", "publish", "sanity", "benchmark", "evidence")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoJob(_Strict):
    name: str  # also the work folder name under [pipeline] root
    source: str  # local path, git URL or zip
    commit: str | None = None
    config: str | None = None  # factory.toml for this repo (default: [pipeline] config)
    drafts: list[str] = Field(default_factory=list)  # globs of draft folders (task.json, requirements.md, ...)
    selftest: str | None = None  # folder of fixtures whose task.json has "expect": "<valid|REJECT_CODE>"
    produce: int = 0  # extra tasks for models to generate (factory produce); needs models
    models: list[str] = Field(default_factory=list)


class BenchmarkJob(_Strict):
    agents: list[str] = Field(default_factory=list)  # "agent" or "agent:provider/model"
    k: int = 1
    timeout_s: int | None = None
    workers: int | None = None


class PipelineConfig(_Strict):
    out: str = "submission"
    root: str = "workdir/pipeline"
    config: str | None = None  # default factory.toml for every repo and the harness
    backend: str | None = None  # docker (default) or local (dev only, no isolation)
    harbor: bool = True  # also publish in Harbor format
    sanity: bool = True  # oracle + noop on every published task
    benchmark: BenchmarkJob = Field(default_factory=BenchmarkJob)
    repos: list[RepoJob] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "PipelineConfig":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        if "pipeline" not in data:
            raise ConfigError(f"{path} has no [pipeline] section")
        return cls.model_validate(data["pipeline"])


# --------------------------------------------------------------------------- helpers


def _factory(argv: list[str], ok: tuple[int, ...] = (0,)) -> int:
    """Run one ``factory`` command in a subprocess, streaming its output."""
    cmd = [sys.executable, "-m", "factory", *argv]
    print(f"\n$ factory {shlex.join(argv)}", flush=True)
    code = subprocess.call(cmd)
    if code not in ok:
        raise ConfigError(f"'factory {shlex.join(argv)}' failed with exit code {code}")
    return code


def _common(pc: PipelineConfig, workdir: Path, config: str | None) -> list[str]:
    args = ["--workdir", str(workdir)]
    if config or pc.config:
        args += ["--config", config or pc.config]
    if pc.backend:
        args += ["--backend", pc.backend]
    return args


def _draft_hash(d: Path) -> str:
    """The draft's files plus the validator's own code: a changed gate re-checks earlier rejections."""
    h = hashlib.sha256()
    for p in sorted((Path(__file__).parent / "validation").glob("*.py")):
        h.update(p.read_bytes())
    for p in sorted(d.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"added": {}, "rejected": {}}


def _drafts(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in patterns:
        for p in sorted(glob.glob(pattern)):
            p = Path(p)
            if (p / "task.json").exists() and p not in out:
                out.append(p)
    return out


def _ideas_in_store(store: Path) -> set[str]:
    ideas = set()
    for pkg in store.glob("*/task.toml") if store.exists() else []:
        ideas.add(tomllib.loads(pkg.read_text(encoding="utf-8")).get("idea_id", ""))
    return ideas


# --------------------------------------------------------------------------- self-test


def run_selftest(cfg, fixtures: Path, out: Path) -> list[dict]:
    """Validate known-good and known-bad drafts; each must get the verdict its task.json expects."""
    from .commands import load_state
    from .sandbox import make_factory
    from .state import Workdir
    from .validation.drafts import load_draft_dir
    from .validation.gates import Validator

    spec, env = load_state(cfg)
    wd = Workdir(cfg.workdir)
    validator = Validator(make_factory(cfg), spec, env, cfg.gates, log_dir=wd.root / "selftest-runs",
                          cache_dir=wd.cache)
    results = []
    for i, d in enumerate(sorted(p for p in Path(fixtures).iterdir() if (p / "task.json").exists()), start=1):
        expect = json.loads((d / "task.json").read_text(encoding="utf-8")).get("expect")
        if not expect:
            continue
        report = validator.validate(load_draft_dir(d, spec, n=i))
        got = "valid" if report.verdict == "valid" else report.reject_code.value
        results.append({"fixture": d.name, "expected": expect, "got": got, "ok": got == expect,
                        "gate": next((g.gate for g in report.gates if not g.passed), None),
                        "reason": report.reject_reason, "mutation_score": report.mutation_score,
                        "backend": report.backend})
        print(f"  selftest {'ok ' if got == expect else 'BAD'} {d.name:28} expected {expect:34} got {got}")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


# --------------------------------------------------------------------------- driver


def run(pipeline_file: Path, *, only: list[str] | None = None, skip: list[str] | None = None,
        force: bool = False, backend: str | None = None) -> dict:
    pc = PipelineConfig.load(pipeline_file)
    if backend:
        pc.backend = backend
    steps = [s for s in STEPS if (not only or s in only) and s not in (skip or [])]
    root, out = Path(pc.root), Path(pc.out)
    bench_wd = root / "_bench"
    started = time.monotonic()
    report: dict = {"pipeline": str(pipeline_file), "steps": steps, "repos": {}}

    for job in pc.repos:
        wd = root / job.name
        common = _common(pc, wd, job.config)
        info = report["repos"].setdefault(job.name, {"added": [], "rejected": [], "skipped": []})
        if "tasks" in steps or "selftest" in steps:
            _factory(["load", "--repo", job.source, *(["--commit", job.commit] if job.commit else []),
                      *common, *(["--force"] if force else [])])
            _factory(["inspect", *common, *(["--force"] if force else [])])
        if "tasks" in steps:
            state_path = wd / "pipeline_state.json"
            state = _state(state_path)
            have = _ideas_in_store(wd / "tasks")
            for d in _drafts(job.drafts):
                key, h = str(d), _draft_hash(d)
                if d.name in have or (not force and state["rejected"].get(key) == h):
                    info["skipped"].append(d.name)
                    continue
                code = _factory(["tasks", "add", str(d), *common], ok=(0, 4))
                if code == 0:
                    state["added"][key] = h
                    info["added"].append(d.name)
                else:
                    state["rejected"][key] = h
                    info["rejected"].append(d.name)
                state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            if job.produce > 0:
                models = job.models or ([os.environ["FACTORY_MODEL"]] if os.environ.get("FACTORY_MODEL") else [])
                if not models:
                    log.warning("%s: produce = %d but no models (set models or FACTORY_MODEL); skipped",
                                job.name, job.produce)
                else:
                    made = sum(1 for p in (wd / "tasks").glob("*/task.toml")
                               if 'model = "human"' not in p.read_text(encoding="utf-8"))
                    if made < job.produce:
                        _factory(["produce", "--count", str(job.produce - made), "--approve",
                                  *[a for m in models for a in ("--model", m)], *common], ok=(0, 4))
        if "selftest" in steps and job.selftest:
            out_file = wd / "selftest.json"
            if force or not out_file.exists():
                _factory(["selftest", "--fixtures", job.selftest, "--out", str(out_file), *common])

    if "publish" in steps:
        for job in pc.repos:
            common = _common(pc, root / job.name, job.config)
            formats = [("native", out / "tasks")] + ([("harbor", out / "harbor")] if pc.harbor else [])
            for fmt, dest in formats:
                code = _factory(["tasks", "publish", "--out", str(dest), "--format", fmt, *common], ok=(0, 4))
                summary = dest / "review_summary.json"
                if code == 0 and summary.exists():
                    summary.replace(dest / f"review_summary.{job.name}.json")

    bench_common = _common(pc, bench_wd, None)
    tasks_dir = out / "tasks"
    if "sanity" in steps and pc.sanity and tasks_dir.exists():
        for agent in ("oracle", "noop"):
            _factory(["harness", "run", "--agent", agent, "--tasks", str(tasks_dir), "--no-branch", *bench_common])

    if "benchmark" in steps and tasks_dir.exists():
        b = pc.benchmark
        for spec in b.agents:
            agent, _, model = spec.partition(":")
            argv = ["harness", "run", "--agent", agent, "--tasks", str(tasks_dir), "--k", str(b.k), *bench_common]
            argv += ["--model", model] if model else []
            argv += ["--timeout", str(b.timeout_s)] if b.timeout_s else []
            argv += ["--workers", str(b.workers)] if b.workers else []
            try:
                _factory(argv)
            except ConfigError as exc:  # one failing agent must not lose the others' results
                log.error("benchmark %s: %s", spec, exc)
                report.setdefault("benchmark_errors", []).append({"agent": spec, "error": str(exc)})

    if "evidence" in steps:
        from . import evidence

        summary = evidence.build(
            tasks=[tasks_dir], harness=[bench_wd / "harness"], out=out,
            rejections=[root / j.name / "rejections.jsonl" for j in pc.repos],
            selftests=[root / j.name / "selftest.json" for j in pc.repos],
            reproduce=f"uv run factory pipeline {pipeline_file}")
        report["summary"] = summary
        print(f"\nreport: {out / 'REPORT.md'}")
        print(json.dumps(summary, indent=2))
    report["seconds"] = round(time.monotonic() - started, 1)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pipeline_run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
