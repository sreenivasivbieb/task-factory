"""Orchestrator CLI (design doc section 17).

Thin wrappers only: each command parses flags and calls one component.
Exit codes: 0 success, 1 pipeline failure, 2 bad usage/config,
3 environment problem, 4 ran but produced no valid tasks.

Command groups::

    factory load | inspect | validate | export | ideas | generate | report | run
    factory tasks   list | show | approve | delete | restore | new | add | edit | publish | summary
                    assign | unassign | assignments
    factory review  (interactive)
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

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_ENV, EXIT_NO_TASKS = 0, 1, 2, 3, 4


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
    parser = argparse.ArgumentParser(prog="factory", description="Coding Task Factory")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # ---------------------------------------------------------------- pipeline
    p = sub.add_parser("load", help="load and pin the repo; writes RepoSpec")
    p.add_argument("--repo", required=True, help="local path, git URL or zip")
    p.add_argument("--commit", default=None, help="commit SHA (default HEAD)")
    _common(p)

    p = sub.add_parser("inspect", help="detect environment, build image, run baseline")
    _common(p)

    p = sub.add_parser("ideas", help="run the feature source; writes ideas.json")
    p.add_argument("--limit", type=int, default=30)
    _common(p)

    p = sub.add_parser("generate", help="generate and validate tasks")
    p.add_argument("--count", type=int, default=10)
    _common(p)

    p = sub.add_parser("validate", help="run the gates on task drafts, or re-verify packages")
    p.add_argument("--tasks", type=Path, required=True,
                   help="a package folder, a folder of packages, or a draft folder")
    _common(p)

    p = sub.add_parser("export", help="write valid tasks as packages into the task store")
    p.add_argument("--out", type=Path, default=None, help="folder (default: the task store)")
    _common(p)

    p = sub.add_parser("report", help="build the evidence report")
    _common(p)

    p = sub.add_parser("run", help="all steps in order")
    p.add_argument("--repo", required=True)
    p.add_argument("--count", type=int, default=10)
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
    p.add_argument("--kind", choices=["feature", "bugfix", "behavior_change"], default="feature")
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
    p.add_argument("--agent", default=None, help="agent (default: harness.default_agent = opencode)")
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
    p.add_argument("--timeout", type=int, default=None, help="agent timeout in seconds")
    p.add_argument("--use-host-auth", action="store_true",
                   help="copy your stored agent login (e.g. OpenCode auth.json, that provider only) into the sandbox")
    p.add_argument("--no-branch", action="store_true", help="do not create git branches for passing solutions")
    _store(p)
    _common(p)

    p = sub.add_parser("bench", help="alias of 'harness run'")
    _harness_run_args(p)
    return parser


def _harness_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent", required=True, help="oracle, noop, opencode, claude-code, codex, aider, "
                                                  "mini-swe-agent, command, or one from [harness.agents]")
    p.add_argument("--solver", dest="agent", help=argparse.SUPPRESS)
    p.add_argument("--model", default=None)
    p.add_argument("--k", type=int, default=None, help="attempts per task (default: harness.k = 1)")
    p.add_argument("--cmd", default=None, help="shell command for --agent command; use {instruction_file}")
    p.add_argument("--tasks", type=Path, default=None, help="a package or folder of packages (default: store)")
    p.add_argument("--task", action="append", default=[], help="only this task ID (repeatable)")
    p.add_argument("--status", choices=["approved", "pending", "all"], default="all",
                   help="which tasks from the store (default: all not deleted)")
    p.add_argument("--timeout", type=int, default=None, help="agent timeout in seconds")
    p.add_argument("--use-host-auth", action="store_true",
                   help="copy your stored agent login (e.g. OpenCode auth.json, that provider only) into the sandbox")
    p.add_argument("--no-branch", action="store_true", help="do not create git branches for passing solutions")
    _store(p)
    _common(p)


def _config(args: argparse.Namespace) -> FactoryConfig:
    cfg = FactoryConfig.load(args.config)
    if args.workdir is not None:
        cfg.workdir = str(args.workdir)
    if args.seed is not None:
        cfg.seed = args.seed
    if args.backend is not None:
        cfg.sandbox.backend = args.backend
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
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
    name = f"cmd_{command}" + (f"_{action}" if action else "")
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
