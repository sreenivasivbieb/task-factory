"""Hand-written task drafts on disk (used by fixtures and for manual tasks).

Layout of a draft folder::

    task.json         {"title", "kind", "tags", "idea_id"?, "manual_edit"?, "manual_edit_reason"?}
    requirements.md   the task statement
    after/            full new content of every source file the reference changes or adds
    tests/            feature test files at their repo-relative paths
    solution.diff     optional: a literal patch, used instead of computing one from after/

The reference patch is computed with git against the pinned commit, so it
never goes stale when the fixture repo changes.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ..ecosystems import registry
from ..errors import PackageError
from ..models import GeneratedBy, RepoSpec, TaskDraft, make_task_id
from ..repo.loader import SYNTHETIC_GIT_ENV, export_clean


def _files_under(root: Path, ecosystem: str | None = None) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*"))
            if p.is_file() and not registry().ignored(p.relative_to(root).as_posix(), ecosystem)}


def diff_from_overlay(spec: RepoSpec, overlay: dict[str, str]) -> str:
    """Unified diff (git format) that turns the pinned commit into commit + overlay files."""
    with tempfile.TemporaryDirectory(prefix="factory-diff-") as tmp:
        work = export_clean(spec, Path(tmp) / "repo")
        env = {**os.environ, **SYNTHETIC_GIT_ENV}

        def git(*args: str) -> str:
            r = subprocess.run(["git", *args], cwd=work, env=env, capture_output=True, text=True)
            if r.returncode != 0:
                raise PackageError(f"git {args[0]} failed: {r.stderr}")
            return r.stdout

        git("init", "-q")
        git("add", "-A")
        git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "base", "--allow-empty")
        for rel, content in overlay.items():
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git("add", "-A")
        return git("diff", "--cached", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/")


def load_draft_dir(path: Path, spec: RepoSpec, n: int = 1) -> TaskDraft:
    path = Path(path)
    meta_path = path / "task.json"
    if not meta_path.exists():
        raise PackageError(f"{path} has no task.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    after = _files_under(path / "after", spec.language)
    if (path / "solution.diff").exists():
        patch = (path / "solution.diff").read_text(encoding="utf-8")
    else:
        patch = diff_from_overlay(spec, after)
    title = meta.get("title", path.name)
    return TaskDraft(
        task_id=meta.get("task_id") or make_task_id(spec.name, spec.commit_sha, title, n),
        idea_id=meta.get("idea_id", path.name),
        kind=meta.get("kind", "feature"),
        tags=meta.get("tags", []),
        requirements_md=(path / "requirements.md").read_text(encoding="utf-8"),
        solution_patch=patch,
        test_files=_files_under(path / "tests", spec.language),
        touched_files=sorted(after) or _touched_from_patch(patch),
        generated_by=GeneratedBy(model=meta.get("model", "human")),
        manual_edit=meta.get("manual_edit", False),
        manual_edit_reason=meta.get("manual_edit_reason"),
    )


def _touched_from_patch(patch: str) -> list[str]:
    out = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            out.append(line[6:])
    return sorted(set(out))

