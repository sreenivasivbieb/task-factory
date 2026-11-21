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
    written = 0
    for report_path in sorted(wd.runs.glob("*/report.json")):
        draft_path = report_path.with_name("draft.json")
        report = ValidationReport.load(report_path)
        if report.verdict != "valid" or not draft_path.exists():
            continue
        write_package(TaskDraft.load(draft_path), report, spec, env, args.out)
        written += 1
    print(f"exported {written} package(s) to {args.out}")
    return 0 if written else 4
