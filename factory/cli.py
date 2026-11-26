"""Orchestrator CLI (design doc section 17).

Thin wrappers only: each command parses flags and calls one component.
Exit codes: 0 success, 1 pipeline failure, 2 bad usage/config,
3 environment problem, 4 ran but produced no valid tasks.

Command groups::

    factory load | inspect | generate | validate | export
    factory chat | terminal   (the Task Factory terminal)
    factory tasks   list | show | approve | delete | restore | new | add | edit | publish | summary
                    assign | unassign | assignments
    factory review  (interactive)
    factory run | proposals list|show|review|approve|reject|edit | benchmark worker|status|kill|enqueue | report
    factory harness agents | run | work   (factory bench = factory harness run)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import FactoryConfig
from .errors import ConfigError, FactoryError

log = logging.getLogger("factory.cli")

EXIT_FAIL, EXIT_USAGE = 1, 2  # other codes come from FactoryError.exit_code


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workdir", type=Path, default=None, help="working folder (default: workdir/)")
    p.add_argument("--config", type=Path, default=None, help="path to factory.toml")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--no-cache", action="store_true", help="ignore cached LLM responses and images")
    p.add_argument("--force", action="store_true", help="rerun a step that already finished")
    p.add_argument("--dry-run", action="store_true", help="show what would run without running it")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--backend", choices=["docker", "local"], default=None,
                   help="sandbox backend (local = no isolation, dev only)")


def _store(p: argparse.ArgumentParser) -> None:
    p.add_argument("--store", type=Path, default=None, help="task store folder (default: <workdir>/tasks)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory", description="Coding Task Factory: turn a repository into validated coding tasks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""workflow:
  1. factory load --repo URL_OR_PATH --commit SHA     pin the repository at a commit
  2. factory inspect                                  build the Docker environment, run the existing tests
  3. factory produce --count 5 --model MODEL          ideas -> drafts -> gates -> repair -> task store
  4. factory review                                   approve or delete tasks (or produce --approve)
  5. factory tasks publish --format harbor --out DIR  export approved tasks for Harbor
  6. factory harbor-check DIR                         Harbor nop (0.0) and oracle (1.0) on every task

  Example config for pallets/click: examples/click/factory.toml (pass it with --config).
  Models: opencode/<model> runs through the local opencode CLI (e.g. opencode/beam-free);
  any other ID goes to FACTORY_MODEL_URL (OpenAI-compatible) with FACTORY_MODEL_KEY.
  Every command accepts --workdir, --config and --verbose; see 'factory COMMAND --help'.""")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # ---------------------------------------------------------------- pipeline
    p = sub.add_parser("load", help="load and pin the repo; writes RepoSpec")
    p.add_argument("--repo", required=True, help="local path, git URL or zip")
    p.add_argument("--commit", default=None, help="commit SHA (default HEAD)")
    _common(p)

    p = sub.add_parser("inspect", help="detect environment, build image, run baseline")
    _common(p)


    p = sub.add_parser("generate", help="draft and validate a task using a configured model")
    p.add_argument("--prompt", required=True, help="describe the coding task")
    p.add_argument("--model", help="model ID (defaults to FACTORY_MODEL)")
    p.add_argument("--file", action="append", default=[], help="repo-relative context file (repeatable)")
    _common(p)

    p = sub.add_parser("produce", help="the automated factory: ideas -> drafts -> gates -> repair -> tasks")
    p.add_argument("--count", type=int, default=5, help="valid tasks to produce (default 5)")
    p.add_argument("--model", action="append", default=[],
                   help="model ID (repeatable: later models take over when one fails or hits its quota); "
                        "opencode/<model> runs through the local opencode CLI. Default: FACTORY_MODEL")
    p.add_argument("--repairs", type=int, default=2, help="repair rounds per idea after a rejection (default 2)")
    p.add_argument("--max-ideas", type=int, default=12, help="ideas to try at most (default 12)")
    p.add_argument("--approve", action="store_true", help="approve valid tasks (default: pending human review)")
    p.add_argument("--retry-rejected", action="store_true",
                   help="build ideas again that the gates rejected in earlier runs (default: skip them)")
    p.add_argument("--harbor", type=Path, default=None, help="also export the new valid tasks to this Harbor folder")
    _store(p)
    _common(p)

    # ---------------------------------------------------------------- reference-guided pipeline (v2.2)
    p = sub.add_parser("run", help="reference-guided factory: profile, peers, PR bundles, novelty, drafts, "
                                   "critics, build, Harbor, background benchmark")
    p.add_argument("--count", type=int, default=None, help="valid distinct tasks to make (default [reference] target, 5)")
    p.add_argument("--once", action="store_true", help="run one finite research-and-build cycle instead of continuing until interrupted")
    p.add_argument("--model", action="append", default=[], help="model for every role not set in [reference.models] (repeatable: later models take over when one hits its quota)")
    p.add_argument("--drafter-model", action="append", default=[], help="drafter model (repeatable: 2+ drafters)")
    p.add_argument("--critic-model", action="append", default=[],
                   help="critic model (repeatable; use another model family than the drafters)")
    p.add_argument("--review", action="store_true", help="turn on the optional review step (P7) before building")
    p.add_argument("--workers", type=int, default=None,
                   help="sub-agent calls at the same time (default [reference] workers, 4; 1 = one after another)")
    p.add_argument("--peer", action="append", default=[], help="manual peer override, owner/name (repeatable)")
    p.add_argument("--snapshot", default=None, help="evidence snapshot id (default <repo>-<sha>; reused when present)")
    p.add_argument("--replay", metavar="SNAPSHOT", default=None, help="replay a snapshot offline (no web, no new LLM calls)")
    p.add_argument("--refresh", action="store_true", help="redo the research even where the snapshot has it")
    p.add_argument("--harbor", choices=["required", "if_available", "off"], default=None,
                   help="Harbor nop/oracle validation (default [reference] harbor, required)")
    p.add_argument("--no-harbor", action="store_true", help="same as --harbor off")
    p.add_argument("--out", type=Path, default=None, help="Harbor tasks + provenance/ reviews/ benchmark/ report")
    p.add_argument("--benchmark", action="append", default=[], metavar="AGENT[:MODEL]",
                   help="AI under test, run in the background after validation (repeatable)")
    p.add_argument("--no-benchmark", action="store_true")
    p.add_argument("--wait", action="store_true", help="run the benchmark in the foreground")
    p.add_argument("--stop-after", choices=["research", "proposals"], default=None, help=argparse.SUPPRESS)
    p.add_argument("--mongo-uri", default=None, help="MongoDB (default $TASK_FACTORY_MONGO_URI)")
    _store(p)
    _common(p)

    props = sub.add_parser("proposals", help="task proposals of a run (list, show, review, approve, reject, edit)")
    psub = props.add_subparsers(dest="action", required=True, metavar="ACTION")
    for name, hlp in (("list", "proposals and their states"), ("show", "one proposal card"),
                      ("review", "interactive review of pending proposals"), ("approve", "approve a pending proposal"),
                      ("reject", "reject a pending proposal (reason required)"),
                      ("edit", "edit a proposal from a JSON file (reason required; critics check it again)")):
        p = psub.add_parser(name, help=hlp)
        p.add_argument("--run", default=None, help="run id (default: the latest run)")
        p.add_argument("--mongo-uri", default=None)
        if name in {"show", "approve", "reject", "edit"}:
            p.add_argument("id", help="proposal id (a unique prefix is enough)")
        if name == "list":
            p.add_argument("--state", default=None, help="only this state, e.g. CHECKED")
        if name == "approve":
            p.add_argument("--note", default=None)
        if name in {"reject", "edit"}:
            p.add_argument("--reason", required=True)
        if name == "edit":
            p.add_argument("--file", required=True, help="JSON object with the fields to change")
        _common(p)

    bench = sub.add_parser("benchmark", help="background benchmark of the AI under test (worker, status, kill, enqueue)")
    bsub = bench.add_subparsers(dest="action", required=True, metavar="ACTION")
    p = bsub.add_parser("worker", help="lease and run benchmark jobs (resumes jobs whose worker died)")
    p.add_argument("--run", default=None, help="only this run's jobs")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--exit-when-empty", action="store_true", help="stop when no job is left (default: keep polling)")
    p.add_argument("--mongo-uri", default=None)
    _common(p)
    p = bsub.add_parser("status", help="jobs by state; solved / partial / failed per agent")
    p.add_argument("--run", default=None)
    p.add_argument("--mongo-uri", default=None)
    _common(p)
    p = bsub.add_parser("kill", help="kill switch: workers stop after their current job")
    p.add_argument("--off", action="store_true", help="turn the kill switch off")
    p.add_argument("--mongo-uri", default=None)
    _common(p)
    p = bsub.add_parser("enqueue", help="queue jobs for the run's validated tasks")
    p.add_argument("--run", default=None)
    p.add_argument("--agent", action="append", required=True, metavar="AGENT[:MODEL]")
    p.add_argument("-k", type=int, default=None, help="attempts per task")
    p.add_argument("--mongo-uri", default=None)
    _common(p)

    p = sub.add_parser("report", help="the run report: yield, peers, tasks, benchmark, declarations")
    p.add_argument("--run", default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--print", action="store_true", help="print the Markdown instead of its path")
    p.add_argument("--mongo-uri", default=None)
    _common(p)

    p = sub.add_parser("harbor-check", help="run Harbor's nop (expect 0.0) and oracle (expect 1.0) on Harbor tasks")
    p.add_argument("tasks", type=Path, help="a Harbor task folder, or a folder of them")
    p.add_argument("--out", type=Path, default=None, help="logs and summary (default: <tasks>-harbor-check)")
    _common(p)

    sub.add_parser("chat", help="launch the Task Factory terminal")
    sub.add_parser("terminal", help="use the bundled terminal engine’s native commands")

    p = sub.add_parser("validate", help="run the gates on task drafts, or re-verify packages")
    p.add_argument("--tasks", type=Path, required=True,
                   help="a package folder, a folder of packages, or a draft folder")
    _common(p)

    p = sub.add_parser("export", help="write valid tasks as packages into the task store")
    p.add_argument("--out", type=Path, default=None, help="folder (default: the task store)")
    _common(p)



    # ---------------------------------------------------------------- human review
    tasks = sub.add_parser("tasks", help="look at, approve, delete, add and publish task packages")
    tsub = tasks.add_subparsers(dest="action", required=True, metavar="ACTION")

    p = tsub.add_parser("list", help="table of tasks with their review status")
    p.add_argument("--status", choices=["all", "pending", "approved", "deleted"], default="all")
    _store(p)
    _common(p)

    p = tsub.add_parser("show", help="show one task: summary, instruction, patch, tests, gates")
    p.add_argument("task_id")
    p.add_argument("--section", choices=["all", "summary", "instruction", "patch", "tests", "gates"], default="all")
    _store(p)
    _common(p)

    p = tsub.add_parser("approve", help="approve a task for publishing")
    p.add_argument("task_ids", nargs="+")
    p.add_argument("--note", default=None)
    _store(p)
    _common(p)

    p = tsub.add_parser("delete", help="move a task to the trash (restorable); a reason is required")
    p.add_argument("task_ids", nargs="+")
    p.add_argument("--reason", required=True, help="why it is not good enough (disclosed in the report)")
    _store(p)
    _common(p)

    p = tsub.add_parser("restore", help="bring a deleted task back as pending")
    p.add_argument("task_ids", nargs="+")
    _store(p)
    _common(p)

    p = tsub.add_parser("new", help="create a draft folder to write your own task")
    p.add_argument("name", help="folder name for the draft")
    p.add_argument("--title", default=None)
    p.add_argument("--kind", choices=["feature", "behavior_change"], default="feature",
                   help="bug fixes are out of scope (organizer rule)")
    p.add_argument("--tags", default="", help="comma-separated tags")
    p.add_argument("--dir", type=Path, default=None, help="where to create it (default: <workdir>/drafts)")
    _common(p)

    p = tsub.add_parser("add", help="validate your own draft with every gate and add it to the store")
    p.add_argument("draft", type=Path)
    p.add_argument("--author", default=None)
    p.add_argument("--pending", action="store_true", help="add as pending instead of approved")
    _store(p)
    _common(p)

    p = tsub.add_parser("edit", help="edit a package: unpack to a draft, then --apply with a reason")
    p.add_argument("task_id")
    p.add_argument("--apply", action="store_true", help="re-validate the edited draft and replace the package")
    p.add_argument("--reason", default=None, help="what was changed and why (required with --apply)")
    _store(p)
    _common(p)

    p = tsub.add_parser("publish", help="copy approved tasks to a submission folder")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--format", choices=["native", "harbor"], default="native")
    p.add_argument("--include-pending", action="store_true")
    _store(p)
    _common(p)

    p = tsub.add_parser("summary", help="counts, deletions with reasons, manual edits")
    _store(p)
    _common(p)

    p = tsub.add_parser("assign", help="assign tasks to a test model (run them with: factory harness work)")
    p.add_argument("task_ids", nargs="+")
    p.add_argument("--agent", default=None, help="agent (default: harness.default_agent = task-factory)")
    p.add_argument("--model", default=None, help="model, e.g. nvidia/..., tokenrouter/..., amazon-bedrock/qwen...")
    p.add_argument("--note", default=None)
    _store(p)
    _common(p)

    p = tsub.add_parser("unassign", help="cancel open assignments")
    p.add_argument("task_ids", nargs="+")
    p.add_argument("--agent", default=None)
    p.add_argument("--model", default=None)
    _store(p)
    _common(p)

    p = tsub.add_parser("assignments", help="list assignments (open by default)")
    p.add_argument("--all", action="store_true", help="include finished ones with their result")
    _store(p)
    _common(p)

    p = sub.add_parser("review", help="interactive review: approve or delete tasks one by one")
    p.add_argument("--status", choices=["pending", "approved", "all"], default="pending")
    _store(p)
    _common(p)

    # ---------------------------------------------------------------- harness
    harness = sub.add_parser("harness", help="run AI coding agents on task packages and score them")
    hsub = harness.add_subparsers(dest="action", required=True, metavar="ACTION")
    p = hsub.add_parser("agents", help="list available agents")
    _common(p)
    p = hsub.add_parser("run", help="run an agent on tasks")
    _harness_run_args(p)
    p = hsub.add_parser("work", help="run every open assignment, record results, branch passing solutions")
    p.add_argument("--k", type=int, default=None, help="attempts per task (default: harness.k = 1)")
    p.add_argument("--workers", type=int, default=None, help="attempts at the same time (default: harness.workers = 1)")
    p.add_argument("--timeout", type=int, default=None, help="agent timeout in seconds")
    p.add_argument("--use-host-auth", action="store_true",
                   help="copy your stored agent login (that provider only) into the sandbox")
    p.add_argument("--no-branch", action="store_true", help="do not create git branches for passing solutions")
    _store(p)
    _common(p)

    # ---------------------------------------------------------------- records + images
    db = sub.add_parser("db", help="records in MongoDB (TASK_FACTORY_MONGO_URI): sync from files, status")
    dsub = db.add_subparsers(dest="action", required=True, metavar="ACTION")
    p = dsub.add_parser("sync", help="write every record of the work folders to MongoDB (idempotent)")
    p.add_argument("--path", type=Path, action="append", default=[],
                   help="a work folder or SWE store (repeatable; default: every one this machine knows)")
    _common(p)
    p = dsub.add_parser("status", help="documents per collection")
    _common(p)
    images = sub.add_parser("images", help="the factory's Docker images")
    isub = images.add_subparsers(dest="action", required=True, metavar="ACTION")
    p = isub.add_parser("prune", help="remove agent images (rebuilt on demand); --all also task images")
    p.add_argument("--all", action="store_true", help="also every factory-* task image (rebuilt from packages)")
    _common(p)

    p = sub.add_parser("bench", help="alias of 'harness run'")
    _harness_run_args(p)
    return parser


def _harness_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent", required=True, help="oracle, noop, task-factory, claude-code, codex, aider, "
                                                  "mini-swe-agent, command, or one from [harness.agents]")
    p.add_argument("--solver", dest="agent", help=argparse.SUPPRESS)
    p.add_argument("--model", default=None)
    p.add_argument("--k", type=int, default=None, help="attempts per task (default: harness.k = 1)")
    p.add_argument("--workers", type=int, default=None, help="attempts at the same time (default: harness.workers = 1)")
    p.add_argument("--cmd", default=None, help="shell command for --agent command; use {instruction_file}")
    p.add_argument("--tasks", type=Path, default=None, help="a package or folder of packages (default: store)")
    p.add_argument("--task", action="append", default=[], help="only this task ID (repeatable)")
    p.add_argument("--status", choices=["approved", "pending", "all"], default="all",
                   help="which tasks from the store (default: all not deleted)")
    p.add_argument("--timeout", type=int, default=None, help="agent timeout in seconds")
    p.add_argument("--use-host-auth", action="store_true",
                   help="copy your stored agent login (that provider only) into the sandbox")
    p.add_argument("--no-branch", action="store_true", help="do not create git branches for passing solutions")
    _store(p)
    _common(p)


def _config(args: argparse.Namespace) -> FactoryConfig:
    cfg = FactoryConfig.load(args.config)
    from .ecosystems import configure

    configure(cfg.ecosystems)  # languages added or changed in factory.toml
    if args.workdir is not None:
        cfg.workdir = str(args.workdir)
    if args.seed is not None:
        cfg.seed = args.seed
    if args.backend is not None:
        cfg.sandbox.backend = args.backend
    return cfg


def _launches_terminal(argv: list[str]) -> bool:
    """No arguments, ``chat``/``terminal``, an engine flag (``--continue``) or a project folder.

    Any other word goes to the factory parser, so a typo such as ``factory taks`` is an error
    instead of silently opening the terminal.
    """
    if not argv or argv[0] in {"chat", "terminal"}:
        return True
    first = argv[0]
    if first in {"-h", "--help"} or first in build_parser()._subparsers._group_actions[0].choices:
        return False
    return first.startswith("-") or Path(first).expanduser().is_dir()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if _launches_terminal(argv):
        from .terminal import launch

        explicit = bool(argv and argv[0] in {"chat", "terminal"})
        args = argv[1:] if explicit else argv
        try:
            return launch(args, integrate=not argv or argv[0] != "terminal")
        except (FactoryError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
    args = parser.parse_args(argv)
    try:
        cfg = _config(args)
    except (OSError, ValueError) as exc:
        print(f"error: bad config: {exc}", file=sys.stderr)
        return EXIT_USAGE
    from .logging_setup import setup_logging

    setup_logging(Path(cfg.workdir), verbose=args.verbose)
    from . import commands

    command = "harness" if args.command == "bench" else args.command
    action = "run" if args.command == "bench" else getattr(args, "action", None)
    name = f"cmd_{command}".replace("-", "_") + (f"_{action}" if action else "")
    handler = getattr(commands, name, None)
    try:
        if handler is None:
            raise ConfigError(f"command '{command}{' ' + action if action else ''}' is not implemented yet")
        return handler(args, cfg)
    except FactoryError as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
