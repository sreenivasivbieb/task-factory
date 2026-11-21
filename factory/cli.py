"""Orchestrator CLI (design doc section 17).

Thin wrappers only: each command parses flags and calls one component.
Exit codes: 0 success, 1 pipeline failure, 2 bad usage/config,
3 environment problem, 4 ran but produced no valid tasks.
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="Coding Task Factory")
    sub = parser.add_subparsers(dest="command", required=True)

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

    p = sub.add_parser("validate", help="re-validate task packages or drafts from disk")
    p.add_argument("--tasks", type=Path, required=True,
                   help="a package folder, a folder of packages, or a draft folder")
    _common(p)

    p = sub.add_parser("export", help="write valid tasks as packages")
    p.add_argument("--out", type=Path, required=True)
    _common(p)

    p = sub.add_parser("report", help="build the evidence report")
    _common(p)

    p = sub.add_parser("bench", help="optional solver benchmark")
    p.add_argument("--tasks", type=Path, default=None)
    p.add_argument("--solver", required=True)
    p.add_argument("--k", type=int, default=3)
    _common(p)

    p = sub.add_parser("run", help="all steps in order")
    p.add_argument("--repo", required=True)
    p.add_argument("--count", type=int, default=10)
    _common(p)
    return parser


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

    handler = getattr(commands, f"cmd_{args.command}", None)
    try:
        if handler is None:
            raise ConfigError(f"command '{args.command}' is not implemented yet")
        return handler(args, cfg)
    except FactoryError as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
