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
    print("next: factory inspect   (build the environment and run the existing tests)")
    return 0


def _inspect_view(spec, result, cfg) -> None:
    from rich.panel import Panel
    from rich.table import Table

    from .generation.ui import console

    c = console()
    env = result.env
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    grid.add_row("repository", f"{spec.name} @ {spec.commit_sha}")
    grid.add_row("image", env.base_image)
    grid.add_row("setup", "\n".join(env.setup_commands) or "-")
    grid.add_row("tests", f"{env.framework}: {env.test_command}")
    failing: list[str] = []
    if result.baseline:
        counts = result.baseline.counts
        failing = sorted(result.baseline.ids_with("failed", "error"))
        parts = [f"[green]{counts.get('passed', 0)} passed[/]",
                 f"[{'red' if failing else 'dim'}]{counts.get('failed', 0) + counts.get('error', 0)} failed[/]",
                 f"[dim]{counts.get('skipped', 0)} skipped[/]"]
        grid.add_row("baseline", " · ".join(parts))
    else:
        grid.add_row("baseline", "not run (dry run)")
    ok = not failing
    c.print(Panel(grid, title="[bold]environment ok[/]" if ok else "[bold]environment built, tests failing[/]",
                  title_align="left", border_style="green" if ok else "yellow", expand=False))
    if failing:
        c.print(f"[bold yellow]WARNING:[/] {len(failing)} existing test(s) fail at this commit, e.g. {failing[0]}")
        c.print("  A task must start from a commit where every existing test passes: add what they need")
        c.print("  (e.g. a system package) to [env] setup_commands in factory.toml, then inspect --force.", markup=False)
        c.print(f"  With [gates] max_known_failure_ratio = {cfg.gates.max_known_failure_ratio} the gates "
                + ("reject every task until then." if cfg.gates.max_known_failure_ratio == 0
                   else "skip these tests (known failures)."), markup=False)
    else:
        c.print("[dim]next:[/] factory produce --count 5 --model opencode/beam-free")


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
    _inspect_view(spec, result, cfg)
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


class ModelChain:
    """Ask models in order; when one fails (quota, HTTP error), the next one takes over for good."""

    def __init__(self, models: list[str]) -> None:
        from .model_client import ModelClient

        self.models = [m for m in models if m] or [None]
        self.clients = [ModelClient(m) for m in self.models]
        self.i = 0

    @property
    def name(self) -> str:
        return self.clients[self.i].model or "unknown"

    def __call__(self, prompt: str) -> str:
        from .errors import ModelProviderError

        while True:
            try:
                return self.clients[self.i].reply([{"role": "user", "content": prompt}])
            except ModelProviderError as exc:
                if self.i + 1 >= len(self.clients):
                    raise
                log.warning("%s failed (%s); switching to %s", self.name, exc, self.clients[self.i + 1].model)
                self.i += 1


def _quiet_console_log(args) -> None:
    """Rich views show the progress: keep INFO lines in the JSONL log only (unless --verbose)."""
    if getattr(args, "verbose", False):
        return
    for h in logging.getLogger("factory").handlers:
        if not isinstance(h, logging.FileHandler):
            h.setLevel(logging.WARNING)


def _preflight_models(chain: "ModelChain") -> None:
    """Fail before any work when no model is set or the opencode CLI a model needs is missing."""
    import shutil

    from .errors import EnvironmentProblem

    if not any(chain.models):
        raise ConfigError("no model: pass --model (repeatable), e.g. --model opencode/beam-free "
                          "--model opencode/hy4-flash-free, or set FACTORY_MODEL")
    for client in chain.clients:
        cli = getattr(client, "_cli", None)
        if cli is not None and not (shutil.which(cli.binary) or Path(cli.binary).is_file()):
            raise EnvironmentProblem(f"{client.model} needs the opencode CLI, which is not installed "
                                     "(npm install -g @opencode/cli, or set FACTORY_OPENCODE_BIN)")


def cmd_produce(args, cfg) -> int:
    import json
    import time

    from .generation.pipeline import produce
    from .generation.ui import ProduceView
    from .review.authoring import add_task
    from .sandbox import make_factory
    from .validation.gates import append_rejection

    spec, env = load_state(cfg)
    wd = Workdir(cfg.workdir)
    store = _store_of(args, cfg)
    chain = ModelChain(args.model or [os.environ.get("FACTORY_MODEL", "")])
    _preflight_models(chain)
    factory = make_factory(cfg)
    if args.dry_run:
        print(f"produce {args.count} task(s) from {spec.name}@{spec.short_sha} with {', '.join(map(str, chain.models))}")
        return 0
    _quiet_console_log(args)
    view = ProduceView()

    def validate(draft_dir):
        result = add_task(draft_dir, spec, env, factory, cfg, store, author=f"factory:{chain.name}",
                          approve=args.approve, log_dir=wd.runs, cache_dir=wd.cache)
        if result.package is None:
            append_rejection(wd.rejections, result.report)
        return result.report, (result.draft.task_id if result.package else None)

    done = {e.meta.idea_id for e in store.entries()}
    rejected_before: dict[str, str] = {}
    if not args.retry_rejected:
        for f in sorted((wd.root / "produce").glob("run-*.json")):
            try:
                for o in json.loads(f.read_text(encoding="utf-8")).get("outcomes", []):
                    if o.get("verdict") == "rejected":
                        rejected_before[o["idea"]] = o.get("title", o["idea"])
                    elif o.get("verdict") == "valid":
                        rejected_before.pop(o["idea"], None)
            except (OSError, ValueError, KeyError):
                continue
    view.header(spec.name, spec.commit_sha, [str(m) for m in chain.models], args.count, args.repairs, args.approve)
    if done or rejected_before:
        view.c.print(f"[dim]skipping {len(done)} idea(s) that already have a task and {len(rejected_before)} "
                     "rejected in earlier runs (--retry-rejected builds those again)[/]")
    started = time.time()
    outcomes: list = []
    errors: list[str] = []

    def collect(o):
        outcomes.append(o)
        view.outcome(o)

    def error(message):
        errors.append(message)
        view.error(message)

    try:
        produce(spec, chain, lambda: chain.name, wd.drafts, wd.root / "ideas.json", validate,
                count=args.count, repairs=args.repairs, max_ideas=args.max_ideas, on_outcome=collect,
                done=done, on_error=error, on_step=view.step, on_idea=view.idea,
                rejected_before=rejected_before)
    finally:  # the record is evidence: write it even when the run stops early
        view.stop()
        valid = [o for o in outcomes if o.verdict == "valid"]
        record = {"repo": spec.name, "commit": spec.commit_sha, "models": [str(m) for m in chain.models],
                  "started": started, "seconds": round(time.time() - started, 1), "goal": args.count,
                  "valid": len(valid), "tried": len(outcomes),
                  "attempts": sum(o.attempts for o in outcomes), "errors": errors,
                  "outcomes": [o.to_dict() for o in outcomes]}
        out = wd.root / "produce" / time.strftime("run-%Y%m%d-%H%M%S.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        view.summary(outcomes, record, out)
    if args.harbor and valid:
        from .packaging.harbor import to_harbor

        from .errors import PackageError

        for o in valid:
            try:
                view.c.print(f"[dim]harbor[/]    {to_harbor(store.get(o.task_id).path, args.harbor)}")
            except PackageError as exc:
                view.error(f"harbor export of {o.task_id}: {exc}")
        view.c.print(f"[dim]next:[/] factory harbor-check {args.harbor}")
    elif valid and not args.approve:
        view.c.print("[dim]next:[/] factory review   (approve the new tasks), then "
                     "factory tasks publish --format harbor --out DIR")
    if len(valid) < args.count:
        return 4
    return 0


def cmd_harbor_check(args, cfg) -> int:
    import json

    from .generation.ui import console, harbor_row, harbor_table
    from .packaging.harbor_check import run_matrix

    _quiet_console_log(args)
    c = console()
    out = args.out or args.tasks.with_name(args.tasks.name + "-harbor-check")
    status = c.status("running Harbor (nop, then oracle) on each task", spinner="dots") if c.is_terminal else None
    if status:
        status.start()
    try:
        rows = run_matrix(args.tasks, out, on_row=lambda r: harbor_row(c, r))
    finally:
        if status:
            status.stop()
    version = json.loads((out / "summary.json").read_text()).get("harbor_version", "")
    if rows:
        harbor_table(c, rows, version)
    ok = sum(r["ok"] for r in rows)
    style = "bold green" if rows and ok == len(rows) else "bold red"
    c.print(f"[{style}]{ok}/{len(rows)} tasks pass the validation matrix[/]; summary: {out / 'summary.md'}")
    return 0 if rows and ok == len(rows) else 4


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
                      approve=not args.pending, log_dir=Workdir(cfg.workdir).runs,
                      cache_dir=Workdir(cfg.workdir).cache)  # G0 baseline: run once per environment
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
        # A republish must not leave behind tasks that were deleted or unapproved since the last one,
        # but it only ever removes tasks this store knows: a folder from another store, another
        # machine or an empty/wrong --store is never touched.
        keep = {e.task_id for e in wanted}
        known = {e.task_id for e in store.entries()}
        stale = [d for d in sorted(out.iterdir())
                 if d.is_dir() and d.name in known and d.name not in keep and (d / "task.toml").exists()]
        for d in stale:
            shutil.rmtree(d)
            print(f"removed   {d.name}  (no longer approved)")
        unknown = [d.name for d in sorted(out.iterdir()) if d.is_dir() and d.name not in known
                   and (d / "task.toml").exists()]
        if unknown:
            print(f"kept      {len(unknown)} task folder(s) this store does not know: {', '.join(unknown[:5])}"
                  + (" …" if len(unknown) > 5 else ""))
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

            subprocess.call([*shlex.split(editor, posix=(os.name != "nt")), str(draft_dir)])
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
                              k=args.k or cfg.harness.k, on_result=on_result, workers=args.workers)
    finally:
        harness.close()
    t = Table(header_style="bold", title=f"{agent.name} · resolved@{summary.k} {summary.resolved_at_k:.0%} · "
                                         f"pass rate {summary.pass_rate:.0%}")
    for col in ("task", "resolved", "difficulty"):
        t.add_column(col)
    for ts in summary.per_task:
        t.add_row(ts.task_id, f"{ts.resolved}/{ts.attempts}", ts.difficulty)
        if store is not None and ts.difficulty == "none" and agent.kind == "command":
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
        if r.branch and r.commit:
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
    only = getattr(args, "only", None)  # {(task_id, agent, model)}: the terminal runs just what it picked
    for a in store.assignments():
        if only is not None and (a.task_id, a.agent, a.model) not in only:
            continue
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

            summary = harness.run([e.path for e in entries], agent, model=model, k=k, on_result=collect,
                                  workers=getattr(args, "workers", None))
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


# --------------------------------------------------------------------------- records + images


def cmd_db_sync(args, cfg) -> int:
    from .records import default_workdirs, records, sync_workdir

    if not records.uri:
        raise ConfigError("set TASK_FACTORY_MONGO_URI (e.g. mongodb://127.0.0.1:27017/task_factory; "
                          "`docker compose up -d mongo` starts one)")
    total: dict[str, int] = {}
    for wd in args.path or default_workdirs():
        for k, v in sync_workdir(wd).items():
            total[k] = total.get(k, 0) + v
        if not records.enabled:
            raise ConfigError(f"MongoDB at {records.uri} is not reachable")
        print(f"synced {wd}")
    print("written:", ", ".join(f"{k} {v}" for k, v in sorted(total.items())) or "nothing")
    return 0


def cmd_db_status(args, cfg) -> int:
    from .records import records

    if not records.uri:
        raise ConfigError("TASK_FACTORY_MONGO_URI is not set: records are in files only")
    try:
        counts = records.counts()
    except Exception as exc:  # noqa: BLE001 - any driver error means "not reachable"
        raise ConfigError(f"MongoDB at {records.uri} is not reachable: {str(exc).splitlines()[0][:200]}") from exc
    for name, n in counts.items():
        print(f"{name:15} {n}")
    return 0


def cmd_images_prune(args, cfg) -> int:
    """Agent images (factory-<repo>-<agent>:...) are cheap to rebuild; task images are rebuilt from packages."""
    import subprocess

    docker = cfg.sandbox.docker_binary
    out = subprocess.run([docker, "images", "--format", "{{.Repository}}:{{.Tag}} {{.Size}}"],
                         capture_output=True, text=True, check=False).stdout.split("\n")
    from .harness.agents import BUILTIN

    agents = tuple(f"-{name}" for name in BUILTIN)
    chosen = [ln.split(" ", 1) for ln in out if ln.startswith("factory-")
              and (args.all or ln.split(":", 1)[0].endswith(agents))]
    for tag, size in chosen:
        print(f"{'would remove' if args.dry_run else 'remove'} {tag} ({size})")
        if not args.dry_run:
            subprocess.run([docker, "rmi", tag], capture_output=True, text=True, check=False)
    if not args.dry_run:  # engine volumes no sandbox uses (an older engine build); refilled on demand
        subprocess.run([docker, "volume", "prune", "-f", "-a", "--filter", "label=factory-file=1"],
                       capture_output=True, text=True, check=False)
    print(f"{len(chosen)} image(s){' (dry run)' if args.dry_run else ''}; `docker builder prune` frees the build cache")
    return 0


# reference-guided pipeline (v2.2): factory run | proposals | benchmark | report
from .reference.commands import (  # noqa: E402,F401
    cmd_benchmark_enqueue, cmd_benchmark_kill, cmd_benchmark_status, cmd_benchmark_worker, cmd_proposals_approve,
    cmd_proposals_edit, cmd_proposals_list, cmd_proposals_reject, cmd_proposals_review, cmd_proposals_show, cmd_report,
    cmd_run)


# =========================================================================== pipeline, evidence, self-test


def cmd_pipeline(args, cfg) -> int:
    from .pipeline import run

    report = run(args.pipeline_file, only=args.only, skip=args.skip, force=args.force, backend=args.backend)
    s = report.get("summary") or {}
    if s and (s.get("selftest_pass") != s.get("selftest_total") or s.get("oracle_resolved") not in (None, 1.0)
              or s.get("noop_resolved") not in (None, 0.0)):
        print("WARNING: a sanity check failed (self-test, oracle or noop); see REPORT.md", file=sys.stderr)
        return 1
    return 0


def cmd_evidence(args, cfg) -> int:
    import json

    from .evidence import build

    wd = Workdir(cfg.workdir)
    summary = build(args.tasks, args.harness or [wd.harness], args.out,
                    rejections=args.rejections or [wd.rejections],
                    selftests=args.selftest or [wd.root / "selftest.json"])
    print(json.dumps(summary, indent=2))
    print(f"report: {Path(args.out) / 'REPORT.md'}")
    return 0 if summary["tasks"] else 4


def cmd_selftest(args, cfg) -> int:
    from .pipeline import run_selftest

    out = args.out or Workdir(cfg.workdir).root / "selftest.json"
    results = run_selftest(cfg, args.fixtures, out)
    bad = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(bad)}/{len(results)} fixtures got the expected verdict -> {out}")
    return 1 if bad or not results else 0
