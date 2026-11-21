"""Re-validate a task package from its files alone (section 13 ``verify_package``).

Steps: check checksums, read the package back into a TaskDraft, reload the
repo at the pinned commit, rebuild the environment, run every gate, and
compare the fresh fail_to_pass / pass_to_pass lists with the package.
"""

from __future__ import annotations

import logging
import tempfile
import tomllib
from pathlib import Path

from ..config import FactoryConfig
from ..errors import PackageError
from ..models import EnvConfig, TaskDraft, TaskPackage, ValidationReport
from ..repo.loader import load_repo
from ..sandbox.base import SandboxFactory
from ..validation.gates import Validator
from .writer import CHECKSUMS, ENV_JSON, INSTRUCTION, SOLUTION, TASK_TOML, TEST_FILES_DIR, compute_checksums

log = logging.getLogger("factory.packaging")


def verify_checksums(pkg_dir: Path) -> list[str]:
    """Return the files whose checksum does not match (missing and extra files included)."""
    pkg_dir = Path(pkg_dir)
    listed = {}
    path = pkg_dir / CHECKSUMS
    if not path.exists():
        return [CHECKSUMS]
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, rel = line.split("  ", 1)
            listed[rel] = digest
    actual = compute_checksums(pkg_dir)
    bad = [rel for rel in listed if actual.get(rel) != listed[rel]]
    bad += [rel for rel in actual if rel not in listed]
    return sorted(set(bad))


def read_package(pkg_dir: Path) -> tuple[TaskPackage, TaskDraft]:
    pkg_dir = Path(pkg_dir)
    try:
        meta = TaskPackage.model_validate(tomllib.loads((pkg_dir / TASK_TOML).read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise PackageError(f"{pkg_dir}: cannot read {TASK_TOML}: {exc}") from exc
    tests_root = pkg_dir / TEST_FILES_DIR
    test_files = {rel: (tests_root / rel).read_text(encoding="utf-8") for rel in meta.test_files}
    draft = TaskDraft(task_id=meta.task_id, idea_id=meta.idea_id, kind=meta.kind, tags=meta.tags,
                      requirements_md=(pkg_dir / INSTRUCTION).read_text(encoding="utf-8"),
                      solution_patch=(pkg_dir / SOLUTION).read_text(encoding="utf-8"),
                      test_files=test_files, fail_to_pass=meta.fail_to_pass, pass_to_pass=meta.pass_to_pass,
                      touched_files=meta.touched_files, generated_by=meta.generated_by,
                      manual_edit=meta.manual_edit, manual_edit_reason=meta.manual_edit_reason)
    return meta, draft


def verify_package(pkg_dir: Path, factory: SandboxFactory, cfg: FactoryConfig | None = None,
                   log_dir: Path | None = None) -> ValidationReport:
    """Rebuild the environment from the package and run the gates again.

    Raises PackageError when checksums or the recorded test lists do not match.
    """
    cfg = cfg or FactoryConfig()
    pkg_dir = Path(pkg_dir)
    bad = verify_checksums(pkg_dir)
    if bad:
        raise PackageError(f"{pkg_dir.name}: checksum mismatch: {', '.join(bad)}")
    meta, draft = read_package(pkg_dir)
    env = EnvConfig.load(pkg_dir / ENV_JSON)
    if env != meta.environment:
        raise PackageError(f"{pkg_dir.name}: {ENV_JSON} and task.toml disagree on the environment")
    with tempfile.TemporaryDirectory(prefix="factory-verify-") as tmp:
        spec = load_repo(meta.repo, Path(tmp), commit=meta.commit_sha)
        if spec.commit_sha != meta.commit_sha:
            raise PackageError(f"{pkg_dir.name}: repo resolved to {spec.commit_sha}, package pins {meta.commit_sha}")
        report = Validator(factory, spec, env, cfg.gates, log_dir=log_dir).validate(draft)
    if report.verdict == "valid":
        diffs = []
        if report.fail_to_pass != meta.fail_to_pass:
            diffs.append("fail_to_pass")
        if report.pass_to_pass != meta.pass_to_pass:
            diffs.append("pass_to_pass")
        if diffs:
            raise PackageError(f"{pkg_dir.name}: re-validation gives different {' and '.join(diffs)}")
    else:
        log.warning("%s no longer validates: %s", meta.task_id, report.reject_reason)
    return report

