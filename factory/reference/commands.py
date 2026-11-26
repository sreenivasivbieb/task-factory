"""CLI handlers of the reference pipeline: run, proposals, benchmark, report."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from ..config import FactoryConfig
from ..errors import ConfigError, EnvironmentProblem
from ..models import slugify
from ..state import Workdir

log = logging.getLogger("factory.reference")


def _store(cfg: FactoryConfig, args=None):
    from .store import ReferenceStore, connect

    uri = getattr(args, "mongo_uri", None) or cfg.reference.mongo_uri
    return ReferenceStore(connect(uri))


def _run_id(store, args) -> str:
    run_id = getattr(args, "run", None)
    if run_id:
        return run_id
    latest = store.latest_run()
    if not latest:
        raise ConfigError("no reference run yet: start one with 'factory run'")
    return latest["run_id"]


def _out(cfg: FactoryConfig, store=None, run_id: str | None = None) -> Path:
    if store is not None and run_id:
        run = store.get_run(run_id) or {}
        if run.get("out"):
            return Path(run["out"])
    return Path(cfg.reference.out or Path(cfg.workdir) / "reference" / "harbor")


def _models(cfg: FactoryConfig, args) -> dict[str, list[str]]:
    m = cfg.reference.models.model_dump()
    default = [x for x in (args.model or []) if x] or [os.environ.get("FACTORY_MODEL", "")]
    default = [x for x in default if x]
    for role in m:
        if not m[role] and default:
            m[role] = list(default)
    if args.drafter_model:
        m["drafter"] = list(args.drafter_model)
    if args.critic_model:
        m["critic"] = list(args.critic_model)
    return m


# =========================================================================== factory run


def cmd_run(args, cfg: FactoryConfig) -> int:
    from ..commands import _quiet_console_log, _store_of, load_state
    from ..generation.ui import console
    from ..review.authoring import add_task
    from ..sandbox import make_factory
    from ..validation.gates import append_rejection
    from .agents import AgentPool
    from .github import GitHub
    from .harbor import HarborValidator, harbor_command
    from .report import write_report
    from .run import Controller, RunOptions
    from .snapshot import Snapshot

    rc = cfg.reference
    if args.peer:
        rc.peers = list(args.peer)
    if args.review:
        rc.auto_approve = False
    if args.once:
        rc.continuous = False
    if args.workers:
        rc.workers = args.workers
    spec, env = load_state(cfg)
    wd = Workdir(cfg.workdir)
    c = console()
    _quiet_console_log(args)

    snap_id = args.replay or args.snapshot or f"{slugify(spec.name, 24)}-{spec.short_sha}"
    mode = "replay" if args.replay else "refresh" if args.refresh else "live"
    snap_root = Path(rc.snapshots or wd.root / "reference" / "snapshots")
    harbor_mode = "off" if args.no_harbor else (args.harbor or rc.harbor)
    agents = [] if args.no_benchmark else (args.benchmark or list(rc.benchmark.agents))
    target = args.count or rc.target
    out = Path(args.out or rc.out or wd.root / "reference" / "harbor")
    if args.dry_run:
        c.print(f"run {spec.name}@{spec.short_sha}: target {target}, snapshot {snap_id} ({mode}), "
                f"review {'on' if not rc.auto_approve else 'off'}, Harbor {harbor_mode}, "
                f"benchmark {agents or 'off'}, out {out}")
        return 0
    store = _store(cfg, args)
    snapshot = Snapshot(snap_root, snap_id, mode)
    pool = AgentPool(_models(cfg, args), snapshot, fallback=[m for m in args.model if m])
    if not pool.cross_family_critic():
        c.print("[yellow]note:[/] every critic uses the drafters' model family; add another family with "
                "--critic-model for stronger bias control")
    gh = GitHub(snapshot)
    factory = make_factory(cfg)
    task_store = _store_of(args, cfg)

    def validate(draft_dir):
        result = add_task(draft_dir, spec, env, factory, cfg, task_store, author="factory:reference", approve=True,
                          log_dir=wd.runs, cache_dir=wd.cache)
        if result.package is None:
            append_rejection(wd.rejections, result.report)
        return result.report, (result.draft.task_id if result.package else None)

    harbor = None
    if harbor_mode != "off":
        try:
            harbor_command()
            harbor = HarborValidator(wd.root / "reference" / "harbor-runs", repeats=rc.harbor_repeats)
        except EnvironmentProblem:
            if harbor_mode == "required":
                raise
            c.print("[yellow]Harbor not found: validating with the gates only[/]")

    def step(msg: str) -> None:
        c.print(f"[dim]{time.strftime('%H:%M:%S')}[/] {msg}")

    opts = RunOptions(target=target, review=not rc.auto_approve, harbor=harbor_mode, out=out, benchmark_agents=agents,
                      stop_after=args.stop_after, continuous=rc.continuous, research_pause_s=rc.research_pause_s)
    from .research import ExampleRunner

    runner = ExampleRunner(factory, spec, env) if (spec.language or "python") == "python" else None
    ctl = Controller(cfg, spec, env, store, pool, snapshot, gh, validate, opts, wd.root, on_step=step,
                     harbor_validator=harbor, task_store=task_store, example_runner=runner)
    c.print(f"[bold]factory run[/] {spec.name}@{spec.short_sha}  run {ctl.run_id}  snapshot {snap_id} ({mode})")
    try:
        result = ctl.run()
    finally:
        out.mkdir(parents=True, exist_ok=True)
        report = write_report(store, ctl.run_id, out)
    c.print(f"[bold]{result.get('valid', 0)}/{target}[/] valid task(s); report: {report}")
    queued = result.get("queued", 0)
    if queued:
        if rc.benchmark.background and not args.wait:
            log_path = wd.root / "reference" / f"benchmark-{ctl.run_id}.log"
            spawn_worker(cfg, args, ctl.run_id, log_path)
            c.print(f"benchmark: {queued} job(s) queued; a background worker runs them (log {log_path}). "
                    "Check with 'factory benchmark status'; the report updates when it finishes.")
        else:
            c.print(f"benchmark: running {queued} job(s)")
            t0 = time.monotonic()
            _work(cfg, store, ctl.run_id, exit_when_empty=True)
            store.save_run(ctl.run_id, {"timings.P10 benchmark": round(time.monotonic() - t0, 1)})
            write_report(store, ctl.run_id, out)
    if result.get("status") in {"research done", "proposals done", "stopped"}:
        return 0
    return 0 if result.get("valid", 0) >= target else 4


def spawn_worker(cfg: FactoryConfig, args, run_id: str, log_path: Path) -> None:
    cmd = [sys.executable, "-m", "factory.cli", "benchmark", "worker", "--run", run_id, "--exit-when-empty",
           "--workdir", str(cfg.workdir)]
    if getattr(args, "config", None):
        cmd += ["--config", str(args.config)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as f:
        subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                         env=os.environ.copy())


# =========================================================================== proposals


def cmd_proposals_list(args, cfg) -> int:
    from . import drafting

    store = _store(cfg, args)
    run_id = _run_id(store, args)
    props = store.list_proposals(run_id, [args.state] if args.state else None)
    for p in props:
        src = p.provenance[0].peer_repo if p.provenance else "-"
        print(f"{p.proposal_id}  {p.state.value:<15} {drafting.category(p)[:22]:<22} {src[:28]:<28} {p.title[:70]}")
    print(f"{len(props)} proposal(s) in run {run_id}")
    return 0


def cmd_proposals_show(args, cfg) -> int:
    from .review import card

    store = _store(cfg, args)
    p = store.find_proposal(args.id, getattr(args, "run", None))
    print(card(p))
    print(f"  behaviour spec:\n{p.behaviour_spec}")
    return 0


def cmd_proposals_approve(args, cfg) -> int:
    from ..review.store import default_reviewer
    from .review import approve

    store = _store(cfg, args)
    p = approve(store, store.find_proposal(args.id, args.run), default_reviewer(), args.note or "")
    print(f"{p.proposal_id} {p.state.value}")
    return 0


def cmd_proposals_reject(args, cfg) -> int:
    from ..review.store import default_reviewer
    from .review import reject

    store = _store(cfg, args)
    p = reject(store, store.find_proposal(args.id, args.run), default_reviewer(), args.reason)
    print(f"{p.proposal_id} {p.state.value}")
    return 0


def cmd_proposals_edit(args, cfg) -> int:
    import json

    from ..review.store import default_reviewer
    from .review import edit

    store = _store(cfg, args)
    changes = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(changes, dict):
        raise ConfigError("--file must hold a JSON object of the fields to change")
    p = edit(store, store.find_proposal(args.id, args.run), default_reviewer(), changes, args.reason)
    print(f"{p.proposal_id} {p.state.value} (critics check it again on the next 'factory run')")
    return 0


def cmd_proposals_review(args, cfg) -> int:
    from ..review.store import default_reviewer
    from .review import interactive

    store = _store(cfg, args)
    counts = interactive(store, _run_id(store, args), default_reviewer())
    print(", ".join(f"{k} {v}" for k, v in counts.items()) + "; continue with 'factory run'")
    return 0


# =========================================================================== benchmark


def _backend(cfg: FactoryConfig):
    from .benchmark import HarnessBackend

    if cfg.sandbox.backend != "docker":
        raise ConfigError("the AI-under-test benchmark requires sandbox.backend = 'docker'")
    if cfg.harness.egress != "allowlist":
        raise ConfigError("the AI-under-test benchmark requires harness.egress = 'allowlist'")
    return HarnessBackend(cfg, Workdir(cfg.workdir).harness)


def _work(cfg: FactoryConfig, store, run_id: str | None, exit_when_empty: bool = True,
          workers: int = 1) -> int:
    import threading

    from .benchmark import Worker

    b = cfg.reference.benchmark
    done = [0]

    def one(i: int) -> None:
        w = Worker(store, _backend(cfg), lease_s=max(300.0, b.timeout_s / 4), run_id=run_id,
                   cost_cap_usd=b.cost_cap_usd)
        done[0] += w.run_until_empty(idle_exit=exit_when_empty,
                                     on_result=lambda r: print(f"[worker {i}] {getattr(r, 'task_id', '')} "
                                                               f"{getattr(r, 'agent_id', '')} "
                                                               f"{getattr(r, 'outcome', r)} {getattr(r, 'reward', '')}",
                                                               flush=True))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return done[0]


def cmd_benchmark_worker(args, cfg) -> int:
    from .report import write_report

    store = _store(cfg, args)
    n = _work(cfg, store, args.run, exit_when_empty=args.exit_when_empty,
              workers=args.workers or cfg.reference.benchmark.workers)
    print(f"{n} job(s) run")
    runs = [args.run] if args.run else sorted({j.run_id for j in store.list_jobs()})
    for run_id in runs:
        write_report(store, run_id, _out(cfg, store, run_id))
    return 0


def cmd_benchmark_status(args, cfg) -> int:
    from collections import Counter

    from .benchmark import summary

    store = _store(cfg, args)
    run_id = _run_id(store, args)
    jobs = store.list_jobs(run_id)
    print(f"run {run_id}: jobs " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(j.state.value for j in jobs).items())))
    s = summary(store.list_results(run_id), jobs)
    for agent, a in s["agents"].items():
        n = a["solved"] + a["partial"] + a["failed"]
        print(f"  {agent}: solved {a['solved']}, partial {a['partial']}, failed {a['failed']} (of {n}); "
              f"infrastructure errors {a['infra_error']}")
    if store.killed():
        print("  kill switch is ON ('factory benchmark kill --off' to resume)")
    return 0


def cmd_benchmark_kill(args, cfg) -> int:
    store = _store(cfg, args)
    store.set_kill(not args.off, "factory benchmark kill")
    print("kill switch " + ("off" if args.off else "on: workers stop after their current job"))
    return 0


def cmd_benchmark_enqueue(args, cfg) -> int:
    from .run import enqueue_validated

    store = _store(cfg, args)
    run_id = _run_id(store, args)
    b = cfg.reference.benchmark
    if args.k:
        b.k = args.k
    n = enqueue_validated(store, run_id, _out(cfg, store, run_id), args.agent, b)
    print(f"{n} job(s) queued for run {run_id}; start a worker with 'factory benchmark worker --run {run_id}'")
    return 0


# =========================================================================== report


def cmd_report(args, cfg) -> int:
    from .report import write_report

    store = _store(cfg, args)
    run_id = _run_id(store, args)
    path = write_report(store, run_id, Path(args.out) if args.out else _out(cfg, store, run_id))
    print(path.read_text(encoding="utf-8") if args.print else path)
    return 0
