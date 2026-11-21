"""Package writer (section 13): write each validated task to disk.

Layout (Harbor-style, assumed until the official template is released; this
module is the only place that knows it)::

    <task_id>/
      instruction.md          requirements. VISIBLE to the solver.
      task.toml               metadata
      solution/solution.diff  reference patch. HIDDEN.
      tests/files/...         feature test files at their repo paths. HIDDEN.
      tests/run_tests.sh      copies the tests in and runs them. HIDDEN.
      environment/Dockerfile  rebuilds the sandbox image
      environment/env.json    EnvConfig
      environment/repo.tar    the pinned commit (git archive), so the package is self-contained
      validation/report.json  gate verdicts; validation/logs/ holds the gate logs
      checksums.txt           SHA-256 of every task file (see docs/decisions.md D23)
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path

from ..errors import PackageError
from ..models import EnvConfig, RepoSpec, TaskDraft, TaskPackage, ValidationReport, ValidationSummary
from ..runner.test_runner import build_command
from ..sandbox.base import git_archive, render_dockerfile
from . import toml_writer

log = logging.getLogger("factory.packaging")

INSTRUCTION = "instruction.md"
TASK_TOML = "task.toml"
SOLUTION = "solution/solution.diff"
TEST_FILES_DIR = "tests/files"
RUN_SCRIPT = "tests/run_tests.sh"
DOCKERFILE = "environment/Dockerfile"
ENV_JSON = "environment/env.json"
REPO_TAR = "environment/repo.tar"
REPORT = "validation/report.json"
LOGS_DIR = "validation/logs"
CHECKSUMS = "checksums.txt"

# Files outside these prefixes are evidence (timings, logs) and are not checksummed.
CHECKSUMMED = (INSTRUCTION, TASK_TOML, "solution/", "tests/", "environment/")

# What the solver may see. Everything else stays hidden.
SOLVER_VISIBLE = (INSTRUCTION,)


def _run_script(env: EnvConfig, test_files: list[str]) -> str:
    cmd = build_command(env, test_files)
    return "\n".join([
        "#!/bin/sh",
        "# Runs the hidden feature tests against a solver's working copy.",
        "# Usage: REPO_DIR=/repo sh tests/run_tests.sh   (run from the package folder)",
        "set -eu",
        'PKG_DIR=$(cd "$(dirname "$0")/.." && pwd)',
        'REPO_DIR=${REPO_DIR:-/repo}',
        'cp -R "$PKG_DIR/tests/files/." "$REPO_DIR/"',
        'cd "$REPO_DIR"',
        f"mkdir -p {shlex.quote(os.path.dirname(env.report_path.rstrip('/')) or '.')}",
        cmd,
        "",
    ])


def package_meta(draft: TaskDraft, report: ValidationReport, spec: RepoSpec, env: EnvConfig) -> TaskPackage:
    return TaskPackage(
        task_id=draft.task_id, repo=spec.source, repo_name=spec.name, commit_sha=spec.commit_sha,
        language=spec.language, test_command=env.test_command, kind=draft.kind, tags=draft.tags,
        idea_id=draft.idea_id, touched_files=draft.touched_files, test_files=sorted(draft.test_files),
        fail_to_pass=report.fail_to_pass, pass_to_pass=report.pass_to_pass,
        known_failures=report.known_failures, manual_edit=draft.manual_edit,
        manual_edit_reason=draft.manual_edit_reason, environment=env, generated_by=draft.generated_by,
        validation=ValidationSummary(verdict=report.verdict, gates=[g.gate for g in report.gates],
                                     mutation_score=report.mutation_score, mutants_run=report.mutants_run,
                                     mutants_caught=report.mutants_caught),
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_checksums(pkg_dir: Path) -> dict[str, str]:
    pkg_dir = Path(pkg_dir)
    out = {}
    for p in sorted(pkg_dir.rglob("*")):
        rel = p.relative_to(pkg_dir).as_posix()
        if p.is_file() and rel.startswith(CHECKSUMMED):
            out[rel] = sha256_file(p)
    return out


def write_package(draft: TaskDraft, report: ValidationReport, spec: RepoSpec, env: EnvConfig,
                  out_dir: Path, log_dir: Path | None = None) -> Path:
    """Write a valid task as a package folder (atomically). Rejected tasks write nothing."""
    if report.verdict != "valid":
        raise PackageError(f"{draft.task_id} is {report.verdict}; only valid tasks are written")
    if report.task_id != draft.task_id:
        raise PackageError(f"report is for {report.task_id}, draft is {draft.task_id}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / draft.task_id
    tmp = Path(tempfile.mkdtemp(prefix=f".{draft.task_id}-", dir=out_dir))
    try:
        def put(rel: str, content: str) -> None:
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        meta = package_meta(draft, report, spec, env)
        put(INSTRUCTION, draft.requirements_md if draft.requirements_md.endswith("\n") else draft.requirements_md + "\n")
        put(TASK_TOML, toml_writer.dumps(meta.model_dump(mode="json")))
        put(SOLUTION, draft.solution_patch)
        for rel, content in sorted(draft.test_files.items()):
            put(f"{TEST_FILES_DIR}/{rel}", content)
        put(RUN_SCRIPT, _run_script(env, sorted(draft.test_files)))
        put(DOCKERFILE, render_dockerfile(env))
        put(ENV_JSON, env.to_json())
        git_archive(spec, tmp / REPO_TAR)  # the Dockerfile ADDs it: build with environment/ as the context
        put(REPORT, report.to_json())
        for gate in report.gates:
            for p in gate.exec_log_paths:
                src = Path(p)
                if src.is_file():
                    dest = tmp / LOGS_DIR / src.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest)
        sums = compute_checksums(tmp)
        put(CHECKSUMS, "".join(f"{h}  {rel}\n" for rel, h in sums.items()))
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log.info("wrote package %s", final)
    return final
