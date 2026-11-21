"""Turn every resolved attempt into a new git node: a commit on its own branch.

The solutions repo is a clone of the task's repo under ``workdir/solutions/<repo>``
(the supplied repo is never touched). For a resolved attempt the harness checks
out the task's starting commit, applies exactly the diff that passed the hidden
tests, commits it, and points a new branch at that commit::

    ai/<agent>-<model>/<task_id>          e.g. ai/opencode-nvidia-qwen3-coder/toyshop-...-007

The branch can be inspected, diffed against the base commit, or pushed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from ..errors import FactoryError
from ..models import TaskPackage, slugify

log = logging.getLogger("factory.harness")

_GIT_ID = ["-c", "user.name=task-factory harness", "-c", "user.email=harness@task-factory.local",
           "-c", "commit.gpgsign=false"]


class SolutionRepoError(FactoryError):
    """The solutions repo could not record a branch."""


def branch_name(agent: str, model: str | None, task_id: str) -> str:
    who = slugify(agent, 24) + (f"-{slugify(model.split('/')[-1], 40)}" if model else "")
    return f"ai/{who}/{task_id}"


class SolutionRepos:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _git(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", *_GIT_ID, *args], cwd=repo, capture_output=True, text=True,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if check and r.returncode != 0:
            raise SolutionRepoError(f"git {args[0]} failed in {repo}: {r.stderr.strip()[:500]}")
        return r

    def repo_for(self, meta: TaskPackage) -> Path:
        path = self.root / slugify(meta.repo_name, 40)
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "-q", meta.repo, str(path)], capture_output=True, text=True)
            if r.returncode != 0:
                raise SolutionRepoError(f"cannot clone {meta.repo}: {r.stderr.strip()[:500]}")
        if self._git(path, "cat-file", "-e", f"{meta.commit_sha}^{{commit}}", check=False).returncode != 0:
            self._git(path, "fetch", "-q", "origin")
        return path

    def record(self, meta: TaskPackage, diff: str, *, agent: str, model: str | None, attempt: int,
               run_id: str, f2p: str, p2p: str, title: str) -> tuple[str, str]:
        """Commit ``diff`` on top of the task's base commit; return (branch, commit SHA)."""
        repo = self.repo_for(meta)
        base = branch_name(agent, model, meta.task_id)
        branch, n = base, 1
        while self._git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0:
            n += 1
            branch = f"{base}-{n}"
        self._git(repo, "checkout", "-q", "--detach", "-f", meta.commit_sha)
        self._git(repo, "clean", "-fdq")
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as fh:
            fh.write(diff if diff.endswith("\n") else diff + "\n")
            patch = fh.name
        try:
            self._git(repo, "apply", "--index", "--whitespace=nowarn", patch)
        finally:
            os.unlink(patch)
        message = "\n".join([
            f"{title}: solved by {agent}" + (f" ({model})" if model else ""),
            "",
            f"Task: {meta.task_id}",
            f"Base commit: {meta.commit_sha}",
            f"Agent: {agent}" + (f" · Model: {model}" if model else "") + f" · Attempt {attempt}",
            f"Hidden tests: {f2p} fail_to_pass passed, {p2p} pass_to_pass passed",
            f"Harness run: {run_id}",
        ])
        self._git(repo, "commit", "-q", "-m", message)
        sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()
        self._git(repo, "branch", branch, sha)
        self._git(repo, "checkout", "-q", "--detach", meta.commit_sha)
        log.info("new branch %s at %s in %s", branch, sha[:12], repo)
        return branch, sha
