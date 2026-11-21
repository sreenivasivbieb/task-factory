"""Shared helpers for factory tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
PY_REPO = FIXTURES / "toy_python_repo"
NODE_REPO = FIXTURES / "toy_node_repo"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    out = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def make_git_repo(src: Path, dst: Path) -> Path:
    """Copy a fixture folder to ``dst`` and commit it as a git repo."""
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".pytest_cache"))
    git(dst, "init", "-q", "-b", "main")
    git(dst, "add", "-A")
    git(dst, "commit", "-q", "-m", "fixture")
    return dst


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "info"], capture_output=True)
    return r.returncode == 0
