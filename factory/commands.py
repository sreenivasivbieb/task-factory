"""Command handlers called by ``cli.py``. Each one calls a single component.

Commands for milestones not built yet are absent; the CLI reports them as
not implemented (exit code 2).
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from .config import FactoryConfig
from .errors import ConfigError
from .models import EnvConfig, InspectResult, RepoSpec
from .review.store import TRASH, TaskStore
from .state import Workdir

log = logging.getLogger("factory.commands")


def _need(path, step: str):
    if not path.exists():
        raise ConfigError(f"{path} not found; run 'factory {step}' first")
    return path


def cmd_load(args: argparse.Namespace, cfg: FactoryConfig) -> int:
    from .repo.loader import load_repo

    wd = Workdir(cfg.workdir)
    if wd.repo_spec.exists() and not args.force:
        spec = RepoSpec.load(wd.repo_spec)
        if spec.source == args.repo and (args.commit is None or spec.commit_sha.startswith(args.commit)):
            log.info("repo already loaded (%s@%s); use --force to reload", spec.name, spec.short_sha)
            return 0
    spec = load_repo(args.repo, wd.root, args.commit)
    spec.save(wd.repo_spec)
    print(f"loaded {spec.name} at {spec.commit_sha} (language: {spec.language or 'unknown'})")
    return 0


def cmd_inspect(args: argparse.Namespace, cfg: FactoryConfig) -> int:
    from .repo.inspector import inspect_repo, smoke_check
    from .sandbox import make_factory

    wd = Workdir(cfg.workdir)
    spec = RepoSpec.load(_need(wd.repo_spec, "load"))
    if wd.env_config.exists() and not args.force:
        log.info("environment already inspected; use --force to redo")
        return 0
    result = inspect_repo(spec, cfg.env)
    if result.status == "ok" and not args.dry_run:
        result = smoke_check(spec, result, make_factory(cfg))
    result.save(wd.inspect_result)
    if result.status != "ok":
        for reason in result.reasons:
            log.error("needs manual config: %s", reason)
        return 2
    result.env.save(wd.env_config)
    counts = result.baseline.counts if result.baseline else "not run (dry run)"
    print(f"environment ok: {result.env.base_image}, framework {result.env.framework}; baseline {counts}")
    return 0


def load_state(cfg: FactoryConfig) -> tuple[RepoSpec, EnvConfig]:
    wd = Workdir(cfg.workdir)
    spec = RepoSpec.load(_need(wd.repo_spec, "load"))
    env = EnvConfig.load(_need(wd.env_config, "inspect"))
    return spec, env


__all__ = ["cmd_load", "cmd_inspect", "load_state", "InspectResult"]


def cmd_generate(args, cfg) -> int:
    from .generation.author import draft_task

    spec, _ = load_state(cfg)
    if args.dry_run:
        print(f"Generate a task from {spec.name}@{spec.short_sha} using {args.model or 'FACTORY_MODEL'}")
        return 0
    draft = draft_task(spec, Workdir(cfg.workdir).drafts, args)
    print(f"Generated draft: {draft}")
    args.tasks = draft
    return cmd_validate(args, cfg)


def _validation_targets(path):
    """A draft folder, a package folder, or a folder of either."""
    from pathlib import Path

    path = Path(path)
    if (path / "task.json").exists() or (path / "task.toml").exists():
        return [path]
    if not path.is_dir():
        raise ConfigError(f"{path} is not a folder")
    found = sorted(p for p in path.iterdir() if (p / "task.json").exists() or (p / "task.toml").exists())
    if not found:
        raise ConfigError(f"no task drafts (task.json) or packages (task.toml) under {path}")
    return found


def cmd_validate(args: argparse.Namespace, cfg: FactoryConfig) -> int:
    from .errors import PackageError
    from .sandbox import make_factory
    from .validation.drafts import load_draft_dir
    from .validation.gates import Validator, append_rejection

    wd = Workdir(cfg.workdir)
    targets = _validation_targets(args.tasks)
    factory = make_factory(cfg)
    validator = None
    valid = 0
    for i, target in enumerate(targets, start=1):
        if (target / "task.toml").exists():
            from .packaging.verify import verify_package

            try:
                report = verify_package(target, factory, cfg, log_dir=wd.runs)
            except PackageError as exc:
                log.error("%s", exc)
                print(f"{'invalid':9} {'PACKAGE_ERROR':34} {target.name}")
                continue
        else:
            spec, env = load_state(cfg)
            if validator is None:
                validator = Validator(factory, spec, env, cfg.gates, log_dir=wd.runs, cache_dir=wd.cache)
            draft = load_draft_dir(target, spec, n=i)
            draft.save(wd.runs / draft.task_id / "draft.json")
            report = validator.validate(draft)
        if report.verdict == "valid":
            valid += 1
        else:
            append_rejection(wd.rejections, report)
        code = report.reject_code.value if report.reject_code else ""
        print(f"{report.verdict:9} {code:34} {report.task_id}")
    print(f"{valid}/{len(targets)} valid")
    return 0 if valid else 4


def cmd_export(args: argparse.Namespace, cfg: FactoryConfig) -> int:
    from .models import TaskDraft, ValidationReport
    from .packaging.writer import write_package

    wd = Workdir(cfg.workdir)
    spec, env = load_state(cfg)
    written = skipped = 0
    for report_path in sorted(wd.runs.glob("*/report.json")):
        draft_path = report_path.with_name("draft.json")
        report = ValidationReport.load(report_path)
        if report.verdict != "valid" or not draft_path.exists():
            continue
        out = args.out or wd.tasks
        if (Path(out) / TRASH / report.task_id).exists():
            log.info("%s was deleted by a reviewer; not exported again", report.task_id)
            continue
        if (Path(out) / report.task_id).exists() and not args.force:
            # never overwrite a package a reviewer may have approved or edited
            skipped += 1
            continue
        write_package(TaskDraft.load(draft_path), report, spec, env, out)
        written += 1
    print(f"exported {written} package(s) to {args.out or wd.tasks}"
          + (f"; {skipped} already there (kept; --force overwrites)" if skipped else "")
          + ("" if args.out else "  (review them with: factory review)"))
    return 0 if (written or skipped) else 4


# =========================================================================== human review


def _console():
    from rich.console import Console

    return Console(highlight=False)


def _store_of(args: argparse.Namespace, cfg: FactoryConfig) -> TaskStore:
    return TaskStore(args.store or Workdir(cfg.workdir).tasks)


def cmd_tasks_list(args, cfg) -> int:
    from .review import render

    store = _store_of(args, cfg)
    entries = store.entries(args.status)
    console = _console()
    if not entries:
        console.print(f"no {args.status if args.status != 'all' else ''} tasks in {store.root}".replace("  ", " "))
        return 0
    assigned: dict[str, list[str]] = {}
    for a in store.assignments():
        assigned.setdefault(a.task_id, []).append(a.label)
    console.print(render.tasks_table(entries, assigned))
    c = store.summary()["counts"]
    console.print(f"[yellow]{c['pending']} pending[/]  [green]{c['approved']} approved[/]  "
                  f"[red]{c['deleted']} deleted[/]   store: {store.root}")
    return 0


def cmd_tasks_show(args, cfg) -> int:
    from .review import render

    render.show(_console(), _store_of(args, cfg).get(args.task_id), args.section)
    return 0


def cmd_tasks_approve(args, cfg) -> int:
    store = _store_of(args, cfg)
    for tid in args.task_ids:
        e = store.approve(tid, args.note)
        print(f"approved  {e.task_id}")
    return 0


def cmd_tasks_delete(args, cfg) -> int:
    store = _store_of(args, cfg)
    for tid in args.task_ids:
        e = store.delete(tid, args.reason)
        print(f"deleted   {e.task_id}  (moved to {store.trash}; undo: factory tasks restore {e.task_id})")
    return 0


def cmd_tasks_restore(args, cfg) -> int:
    store = _store_of(args, cfg)
    for tid in args.task_ids:
        e = store.restore(tid)
        print(f"restored  {e.task_id}  (pending)")
    return 0


def cmd_tasks_new(args, cfg) -> int:
    from .review.authoring import scaffold_draft
    from .review.store import default_reviewer

    root = args.dir or Workdir(cfg.workdir).drafts
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    path = scaffold_draft(Path(root) / args.name, args.title or args.name.replace("-", " ").replace("_", " "),
                          args.kind, tags, default_reviewer())
    print(f"draft created at {path}")
    print(f"  1. read {path / 'HOW_TO.md'}")
    print("  2. write requirements.md, put changed files in after/ and hidden tests in tests/")
    print(f"  3. factory tasks add {path}")
    return 0


def _print_report(report) -> None:
    console = _console()
    for g in report.gates:
        mark = "[green]pass[/]" if g.passed else "[red]FAIL[/]"
        console.print(f"  {g.gate}  {mark}  {g.details}")


def cmd_tasks_add(args, cfg) -> int:
    from .review.authoring import add_task
    from .sandbox import make_factory

    spec, env = load_state(cfg)
    store = _store_of(args, cfg)
    result = add_task(args.draft, spec, env, make_factory(cfg), cfg, store, author=args.author,
                      approve=not args.pending, log_dir=Workdir(cfg.workdir).runs)
    _print_report(result.report)
    if result.package is None:
        print(f"NOT added: {result.report.reject_code.value}: {result.report.reject_reason}")
        print(f"logs: {Workdir(cfg.workdir).runs / result.draft.task_id}")
        return 4
    print(f"added {result.draft.task_id} ({'pending' if args.pending else 'approved'}) -> {result.package}")
    return 0


def cmd_tasks_edit(args, cfg) -> int:
    from .review.authoring import apply_edit, package_to_draft
    from .sandbox import make_factory

    store = _store_of(args, cfg)
    entry = store.get(args.task_id)
    draft_dir = Workdir(cfg.workdir).edits / entry.task_id
    if not args.apply:
        package_to_draft(entry.path, draft_dir)
        print(f"editable copy at {draft_dir}")
        print("  edit requirements.md, solution.diff or tests/, then run:")
        print(f"  factory tasks edit {entry.task_id} --apply --reason \"what you changed and why\"")
        return 0
    if not draft_dir.exists():
        raise ConfigError(f"no draft at {draft_dir}; run 'factory tasks edit {entry.task_id}' first")
    result = apply_edit(store, entry.task_id, draft_dir, args.reason or "", make_factory(cfg), cfg,
                        log_dir=Workdir(cfg.workdir).runs)
    _print_report(result.report)
    if result.package is None:
        print(f"edit NOT applied (package unchanged): {result.report.reject_code.value}: {result.report.reject_reason}")
        return 4
    print(f"edited {entry.task_id}: re-validated, marked as a manual edit, back to pending")
    return 0


def cmd_tasks_publish(args, cfg) -> int:
    store = _store_of(args, cfg)
    wanted = [e for e in store.entries() if e.status == "approved" or (args.include_pending and e.status == "pending")]
    out = Path(args.out)
    if out.is_dir():
        # a republish must not leave behind tasks that were deleted or unapproved since the last one
        keep = {e.task_id for e in wanted}
        stale = [d for d in sorted(out.iterdir()) if d.is_dir() and d.name not in keep and (d / "task.toml").exists()]
        for d in stale:
            shutil.rmtree(d)
            print(f"removed   {d.name}  (no longer approved)")
    if not wanted:
        print("nothing to publish: no approved tasks (approve with 'factory review' or 'factory tasks approve')")
        return 4
    out.mkdir(parents=True, exist_ok=True)
    for e in wanted:
        if args.format == "harbor":
            from .packaging.harbor import to_harbor

            to_harbor(e.path, out)
        else:
            dest = out / e.task_id
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(e.path, dest)
    store.write_summary(out / "review_summary.json")
    print(f"published {len(wanted)} task(s) to {out} ({args.format} format); review_summary.json lists deletions "
          "and manual edits")
    if args.format == "harbor":
        print(f"  run them with Harbor:  harbor run -p {out} -a oracle")
    return 0


def cmd_tasks_summary(args, cfg) -> int:
    import json

    print(json.dumps(_store_of(args, cfg).summary(), indent=2))
    return 0


def cmd_review(args, cfg) -> int:
    from .review.authoring import apply_edit, package_to_draft
    from .review.interactive import review_loop
    from .sandbox import make_factory

    store = _store_of(args, cfg)
    console = _console()
    if not sys.stdin.isatty():
        raise ConfigError("factory review is interactive; use 'factory tasks list/show/approve/delete' in scripts")

    def ask(prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return "q"

    def edit(entry):
        draft_dir = Workdir(cfg.workdir).edits / entry.task_id
        package_to_draft(entry.path, draft_dir)
        editor = os.environ.get("EDITOR")
        console.print(f"editable copy at [bold]{draft_dir}[/]")
        if editor:
            import shlex
            import subprocess

            subprocess.call([*shlex.split(editor), str(draft_dir)])
        if ask("press Enter when done editing (or type 'cancel')> ").strip().lower() == "cancel":
            return None
        reason = ask("what did you change and why? (required)> ").strip()
        if not reason:
            return "[red]no reason given; edit not applied[/]"
        result = apply_edit(store, entry.task_id, draft_dir, reason, make_factory(cfg), cfg,
                            log_dir=Workdir(cfg.workdir).runs)
        if result.package is None:
            return f"[red]edit rejected by {result.report.reject_code.value}[/]: {result.report.reject_reason}"
        return "[green]edit applied and re-validated[/] (marked as a manual edit)"

    review_loop(store, console, ask, status=args.status, edit=edit)
    return 0


# =========================================================================== harness


def cmd_harness_agents(args, cfg) -> int:
    from rich.table import Table

    from .harness.agents import agent_registry

    t = Table(header_style="bold")
    for col in ("agent", "description", "needs", "state"):
        t.add_column(col, overflow="fold")
    for name, a in agent_registry(cfg.harness.agents).items():
        t.add_row(name, a.description, " or ".join(a.secrets) or "-", "experimental" if a.experimental else "ready")
    t.add_row("command", "any shell command: --cmd 'tool {instruction_file}'", "-", "ready")
    _console().print(t)
    return 0


def cmd_harness_run(args, cfg) -> int:
    from rich.table import Table

    from .harness.agents import get_agent
    from .harness.runner import Harness
    from .sandbox import make_factory

    agent = get_agent(args.agent, cfg.harness.agents, args.cmd)
    if args.timeout:
        agent = agent.model_copy(update={"timeout_s": args.timeout})
    if args.tasks:
        root = Path(args.tasks)
        packages = [root] if (root / "task.toml").exists() else sorted(
            p for p in root.iterdir() if (p / "task.toml").exists())
        store = None
    else:
        store = _store_of(args, cfg)
        packages = [e.path for e in store.entries() if e.status != "deleted"
                    and (args.status == "all" or e.status == args.status)]
    if args.task:
        packages = [p for p in packages if any(t in p.name for t in args.task)]  # same partial IDs as tasks show
    if not packages:
        raise ConfigError("no task packages to run on")
    console = _console()
    console.print(f"running [bold]{agent.name}[/] on {len(packages)} task(s), k={args.k or cfg.harness.k}")

    on_result = _printer(console)

    harness = _make_harness(args, cfg)
    try:
        summary = harness.run(packages, agent, model=args.model or cfg.harness.default_model,
                              k=args.k or cfg.harness.k, on_result=on_result)
    finally:
        harness.close()
    t = Table(header_style="bold", title=f"{agent.name} · resolved@{summary.k} {summary.resolved_at_k:.0%} · "
                                         f"pass rate {summary.pass_rate:.0%}")
    for col in ("task", "resolved", "difficulty"):
        t.add_column(col)
    for ts in summary.per_task:
        t.add_row(ts.task_id, f"{ts.resolved}/{ts.attempts}", ts.difficulty)
        if store is not None and ts.difficulty == "unsolved" and agent.kind == "command":
            store.note(ts.task_id, f"unsolved by {agent.name} in {ts.attempts} attempt(s); check it is solvable")
    console.print(t)
    if summary.stopped_early:
        console.print(f"[red]stopped early:[/] {summary.stopped_early}")
    console.print(f"results: {Workdir(cfg.workdir).harness / summary.run_id}")
    return 0


def _printer(console):
    def on_result(r):
        style = "green" if r.resolved else "red"
        line = f"  [{style}]{r.category:16}[/] {r.task_id} (attempt {r.attempt}, agent {r.agent_duration_s}s)"
        if r.branch:
            line += f"\n      [green]new branch[/] {r.branch} @ {r.commit[:12]}"
        console.print(line)
    return on_result


def _make_harness(args, cfg):
    from .harness.runner import Harness
    from .harness.solutions import SolutionRepos
    from .sandbox import make_factory

    wd = Workdir(cfg.workdir)
    branches = cfg.harness.branches and not getattr(args, "no_branch", False)
    return Harness(make_factory(cfg), cfg, wd.harness,
                   use_host_auth=getattr(args, "use_host_auth", False) or cfg.harness.use_host_auth,
                   solutions=SolutionRepos(wd.root / "solutions") if branches else None)


def cmd_tasks_assign(args, cfg) -> int:
    from .harness.agents import get_agent

    store = _store_of(args, cfg)
    agent = args.agent or cfg.harness.default_agent
    get_agent(agent, cfg.harness.agents, cmd="x" if agent == "command" else None)  # fail early on a typo
    model = args.model or cfg.harness.default_model
    for tid in args.task_ids:
        a = store.assign(tid, agent, model, args.note)
        print(f"assigned  {a.task_id}  ->  {a.label}")
    print("run them with: factory harness work")
    return 0


def cmd_tasks_unassign(args, cfg) -> int:
    store = _store_of(args, cfg)
    for tid in args.task_ids:
        print(f"unassigned {store.unassign(tid, args.agent, args.model)} assignment(s) of {tid}")
    return 0


def cmd_tasks_assignments(args, cfg) -> int:
    from rich.table import Table

    store = _store_of(args, cfg)
    rows = store.assignments(open_only=not args.all)
    if not rows:
        print("no open assignments" if not args.all else "no assignments")
        return 0
    t = Table(header_style="bold")
    for col in ("task", "agent:model", "assigned by", "state", "result", "branch"):
        t.add_column(col, overflow="fold")
    for a in rows:
        r = a.result
        state = "open" if a.open else ("done" if r.action == "result" else "cancelled")
        t.add_row(a.task_id, a.label, a.assigned_by, state, (r.reason or "") if r else "", (r.branch or "") if r else "")
    _console().print(t)
    return 0


def cmd_harness_work(args, cfg) -> int:
    from .harness.agents import get_agent
    from .harness.runner import INFRA_CATEGORIES

    store = _store_of(args, cfg)
    console = _console()
    groups: dict[tuple[str, str | None], list] = {}
    for a in store.assignments():
        entry = store.get(a.task_id)
        if entry.status == "deleted":
            console.print(f"[yellow]skip[/] {a.task_id}: deleted")
            continue
        groups.setdefault((a.agent, a.model), []).append(entry)
    if not groups:
        console.print("no open assignments (assign with: factory tasks assign TASK_ID --model ...)")
        return 0
    k = args.k or cfg.harness.k
    resolved_total = 0
    harness = _make_harness(args, cfg)
    try:
        for (agent_name, model), entries in groups.items():
            agent = get_agent(agent_name, cfg.harness.agents)
            if args.timeout:
                agent = agent.model_copy(update={"timeout_s": args.timeout})
            console.print(f"[bold]{agent_name}[/]" + (f" · {model}" if model else "") + f": {len(entries)} task(s), k={k}")
            results = []
            printer = _printer(console)

            def collect(r, printer=printer, results=results):
                results.append(r)
                printer(r)

            summary = harness.run([e.path for e in entries], agent, model=model, k=k, on_result=collect)
            who = f"{agent_name}{' ' + model if model else ''}"
            for e in entries:
                rs = [r for r in results if r.task_id == e.task_id]
                if not rs:
                    console.print(f"  [yellow]not attempted[/] {e.task_id} (run stopped); assignment stays open")
                    continue
                ok = [r for r in rs if r.resolved]
                if not ok and all(r.category in INFRA_CATEGORIES for r in rs):
                    # a setup/quota problem, not a verdict on the task: keep the assignment open
                    store.note(e.task_id, f"{who}: {', '.join(sorted({r.category for r in rs}))} in run "
                                          f"{summary.run_id}; assignment left open, retry with 'factory harness work'")
                    console.print(f"  [yellow]kept open[/] {e.task_id} (agent error, not a result)")
                    continue
                cats = ", ".join(sorted({r.category for r in rs}))
                text = f"resolved {len(ok)}/{len(rs)}" + ("" if ok else f" ({cats})")
                branch = next((r.branch for r in ok if r.branch), None)
                store.record_result(e.task_id, agent_name, model, text, run_id=summary.run_id, branch=branch)
                if not ok:
                    store.note(e.task_id, f"unsolved by {who} in {len(rs)} attempt(s); check it is solvable")
                resolved_total += bool(ok)
            if summary.stopped_early:
                console.print(f"  [red]stopped early:[/] {summary.stopped_early}")
            console.print(f"  resolved@{k}: {summary.resolved_at_k:.0%} · results: "
                          f"{Workdir(cfg.workdir).harness / summary.run_id}")
    finally:
        harness.close()
    console.print(f"done: {resolved_total} task(s) resolved; passing solutions are branches in "
                  f"{Workdir(cfg.workdir).root / 'solutions'}")
    return 0
