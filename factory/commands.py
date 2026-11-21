"""Command handlers called by ``cli.py``. Each one calls a single component.

Commands for milestones not built yet are absent; the CLI reports them as
not implemented (exit code 2).
"""

from __future__ import annotations

import argparse
import logging

from .config import FactoryConfig
from .errors import ConfigError
from .models import EnvConfig, InspectResult, RepoSpec
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
