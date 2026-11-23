"""Human-authored tasks: scaffold a draft, add it through the gates, edit a package.

A human-written task gets no shortcut: it must pass every gate like a generated
one. It is marked ``generated_by.model = "human"`` with the author's name. An
edit to an existing package re-runs the gates and is disclosed as a manual
edit with its reason.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import FactoryConfig
from ..errors import ConfigError
from ..models import EnvConfig, GeneratedBy, RepoSpec, TaskDraft, ValidationReport
from ..packaging.verify import package_repo, read_package
from ..packaging.writer import write_package
from ..sandbox.base import SandboxFactory
from ..validation.drafts import load_draft_dir
from ..validation.gates import Validator
from .store import TaskStore

log = logging.getLogger("factory.review")

HOW_TO = """# How to write this task

Fill in the files below, then run:

    factory tasks add {path}

The task must pass every validation gate (G0-G6) before it is added.

| File | What to put there |
| --- | --- |
| `task.json` | title, kind (`feature`, `bugfix` or `behavior_change`), tags |
| `requirements.md` | what to build: inputs, outputs, errors, edge cases. No hints, no test code. |
| `after/<repo path>` | the FULL new content of every source file your reference solution changes or adds |
| `tests/<repo path>` | the hidden tests, at the path they should have in the repo |
| `solution.diff` | optional: a unified diff instead of `after/` |

Rules the gates enforce:

- the new tests must pass with your solution (G2) and FAIL without it (G3);
- every existing test must keep passing (G4);
- small wrong variants of your solution must make at least one test fail (G6),
  so test the boundaries and the error cases;
- the result must be the same when repeated (G5).

Delete this file before adding (it is ignored anyway).
"""

REQUIREMENTS_TEMPLATE = """# {title}

Describe the behaviour to add or change. Name the existing modules and functions it touches.

- Inputs:
- Outputs:
- Errors:
- Edge cases:
"""

TAGS = ["validation rule", "new endpoint or command", "data transformation", "edge-case handling",
        "error handling", "performance or caching", "config or flag", "cross-feature interaction", "bug fix",
        "refactor with behaviour kept", "security", "scalability", "api design"]


def scaffold_draft(path: Path, title: str, kind: str = "feature", tags: list[str] | None = None,
                   author: str | None = None) -> Path:
    path = Path(path)
    if path.exists() and any(path.iterdir()):
        raise ConfigError(f"{path} already exists and is not empty")
    (path / "after").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(parents=True, exist_ok=True)
    meta = {"title": title, "kind": kind, "tags": tags or [], "author": author,
            "_allowed_tags": TAGS}
    (path / "task.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (path / "requirements.md").write_text(REQUIREMENTS_TEMPLATE.format(title=title), encoding="utf-8")
    (path / "HOW_TO.md").write_text(HOW_TO.format(path=path), encoding="utf-8")
    return path


@dataclass
class AddResult:
    report: ValidationReport
    draft: TaskDraft
    package: Path | None


def _check_draft(path: Path) -> None:
    req = (path / "requirements.md")
    if not req.exists() or "- Inputs:\n- Outputs:" in req.read_text(encoding="utf-8"):
        raise ConfigError(f"{req} is missing or still the template; describe the task first")
    has_after = (path / "after").is_dir() and any(p.is_file() for p in (path / "after").rglob("*"))
    if not has_after and not (path / "solution.diff").exists():
        raise ConfigError(f"{path} has no reference solution: put changed files in after/ or add solution.diff")
    if not (path / "tests").is_dir() or not any(p.is_file() for p in (path / "tests").rglob("*")):
        raise ConfigError(f"{path} has no hidden tests under tests/")


def add_task(draft_dir: Path, spec: RepoSpec, env: EnvConfig, factory: SandboxFactory, cfg: FactoryConfig,
             store: TaskStore, *, author: str | None = None, approve: bool = True,
             log_dir: Path | None = None) -> AddResult:
    """Validate a human-written draft; if every gate passes, write it to the store."""
    draft_dir = Path(draft_dir)
    _check_draft(draft_dir)
    taken = {e.task_id for e in store.entries()}
    n = len(taken) + 1
    draft = load_draft_dir(draft_dir, spec, n=n)
    while draft.task_id in taken:  # never overwrite an existing (or trashed) package
        if json.loads((draft_dir / "task.json").read_text(encoding="utf-8")).get("task_id"):
            raise ConfigError(f"{draft.task_id} already exists; to change it use 'factory tasks edit', "
                              "or remove task_id from task.json to add it as a new task")
        n += 1
        draft = load_draft_dir(draft_dir, spec, n=n)
    meta = json.loads((draft_dir / "task.json").read_text(encoding="utf-8"))
    # A draft written by a model (task.json "model", e.g. from /generate-task) keeps that model as its
    # author, so AI-made tasks are never reported as human-authored.
    model = meta.get("model") or "human"
    draft.generated_by = GeneratedBy(model=model, author=author or meta.get("author") or store.reviewer)
    report = Validator(factory, spec, env, cfg.gates, log_dir=log_dir).validate(draft)
    if report.verdict != "valid":
        return AddResult(report, draft, None)
    pkg = write_package(draft, report, spec, env, store.root)
    store.record(draft.task_id, "add", "human-authored" if model == "human" else f"generated by {model}")
    if approve:
        store.record(draft.task_id, "approve", "added by its author")
    return AddResult(report, draft, pkg)


def package_to_draft(pkg_dir: Path, dest: Path) -> Path:
    """Unpack a package into an editable draft folder (requirements.md, solution.diff, tests/)."""
    meta, draft = read_package(pkg_dir)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "tests").mkdir(parents=True)
    (dest / "requirements.md").write_text(draft.requirements_md, encoding="utf-8")
    (dest / "solution.diff").write_text(draft.solution_patch, encoding="utf-8")
    for rel, content in draft.test_files.items():
        p = dest / "tests" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    task_meta = {"task_id": meta.task_id, "title": meta.task_id, "kind": meta.kind, "tags": meta.tags,
                 "idea_id": meta.idea_id}
    (dest / "task.json").write_text(json.dumps(task_meta, indent=2) + "\n", encoding="utf-8")
    return dest


def apply_edit(store: TaskStore, task_id: str, draft_dir: Path, reason: str, factory: SandboxFactory,
               cfg: FactoryConfig, log_dir: Path | None = None) -> AddResult:
    """Re-validate an edited draft and replace the package. The edit is disclosed with ``reason``."""
    if not reason or not reason.strip():
        raise ConfigError("a reason is required for a manual edit (organizers require disclosure)")
    entry = store.get(task_id)
    if entry.status == "deleted":
        raise ConfigError(f"{entry.task_id} is deleted; restore it before editing")
    old_meta, old_draft = read_package(entry.path)
    env = EnvConfig.load(entry.path / "environment" / "env.json")
    with tempfile.TemporaryDirectory(prefix="factory-edit-") as tmp:
        spec = package_repo(entry.path, old_meta, Path(tmp))
        draft = load_draft_dir(Path(draft_dir), spec)
        draft = draft.model_copy(update={
            "task_id": entry.task_id, "idea_id": old_meta.idea_id, "touched_files": draft.touched_files,
            "generated_by": old_draft.generated_by, "manual_edit": True, "manual_edit_reason": reason.strip()})
        report = Validator(factory, spec, env, cfg.gates, log_dir=log_dir).validate(draft)
        if report.verdict != "valid":
            return AddResult(report, draft, None)
        pkg = write_package(draft, report, spec, env, store.root)
    store.record(entry.task_id, "edit", reason.strip())
    return AddResult(report, draft, pkg)
