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

    def _has_commit(self, repo: Path, ref: str) -> bool:
        return self._git(repo, "cat-file", "-e", f"{ref}^{{commit}}", check=False).returncode == 0

    def repo_for(self, meta: TaskPackage, archive: Path | None = None) -> tuple[Path, str]:
        """The solutions repo for ``meta`` and the ref of the task's starting commit in it.

        Normally a clone of the original repo (the starting commit keeps its real SHA).
        When the original is not reachable, the repo is built from the package's
        repo.tar instead and the starting commit is tagged ``base/<sha>``.
        """
        path = self.root / slugify(meta.repo_name, 40)
        tag = f"base/{meta.commit_sha}"
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "-q", meta.repo, str(path)], capture_output=True, text=True)
            if r.returncode != 0:
                if archive is None or not Path(archive).is_file():
                    raise SolutionRepoError(f"cannot clone {meta.repo}: {r.stderr.strip()[:500]}")
                path.mkdir(parents=True, exist_ok=True)
                self._git(path, "init", "-q")
        if self._has_commit(path, meta.commit_sha):
            return path, meta.commit_sha
        if self._has_commit(path, tag):
            return path, tag
        if self._git(path, "remote", check=False).stdout.strip():
            self._git(path, "fetch", "-q", "origin", check=False)
            if self._has_commit(path, meta.commit_sha):
                return path, meta.commit_sha
        if archive is None or not Path(archive).is_file():
            raise SolutionRepoError(f"commit {meta.commit_sha} not found in {meta.repo}")
        self._import_archive(path, Path(archive), tag, meta)
        return path, tag

    def _import_archive(self, repo: Path, archive: Path, tag: str, meta: TaskPackage) -> None:
        import tarfile

        self._git(repo, "checkout", "-q", "--orphan", f"import/{meta.commit_sha[:12]}")
        self._git(repo, "rm", "-rfq", "--ignore-unmatch", ".", check=False)
        self._git(repo, "clean", "-fdqx")
        with tarfile.open(archive) as tf:
            tf.extractall(repo, filter="data")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "--allow-empty", "-m", f"{meta.repo_name} at {meta.commit_sha} (from repo.tar)")
        self._git(repo, "tag", tag)

    def record(self, meta: TaskPackage, diff: str, *, agent: str, model: str | None, attempt: int,
               run_id: str, f2p: str, p2p: str, title: str, archive: Path | None = None) -> tuple[str, str]:
        """Commit ``diff`` on top of the task's base commit; return (branch, commit SHA)."""
        repo, base_ref = self.repo_for(meta, archive)
        base = branch_name(agent, model, meta.task_id)
        branch, n = base, 1
        while self._git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0:
            n += 1
            branch = f"{base}-{n}"
        self._git(repo, "checkout", "-q", "--detach", "-f", base_ref)
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
        self._git(repo, "checkout", "-q", "--detach", base_ref)
        log.info("new branch %s at %s in %s", branch, sha[:12], repo)
        return branch, sha
