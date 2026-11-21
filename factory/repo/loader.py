"""Repo loader (section 8): copy a supplied repo into the workdir and pin one commit.

Accepts a local path, a git URL or a zip. The supplied original is never edited.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ..errors import RepoLoadError
from ..models import RepoSpec, slugify

log = logging.getLogger("factory.repo")

# Fixed identity and dates so a synthetic history gives the same SHA every run.
SYNTHETIC_GIT_ENV = {
    "GIT_AUTHOR_NAME": "task-factory", "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "task-factory", "GIT_COMMITTER_EMAIL": "factory@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}

_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://|file://)")


def _git(cwd: Path | str | None, *args: str, env: dict | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if r.returncode != 0:
        raise RepoLoadError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def source_kind(source: str) -> str:
    """'zip', 'path' or 'url'."""
    if _URL_RE.match(source):
        return "url"
    p = Path(source)
    if p.is_file() and zipfile.is_zipfile(p):
        return "zip"
    if p.is_dir():
        return "path"
    raise RepoLoadError(f"repo source not found or not supported: {source}")


def repo_name(source: str) -> str:
    base = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    for suffix in (".git", ".zip"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return slugify(base, 40)


def load_repo(source: str, workdir: Path, commit: str | None = None) -> RepoSpec:
    """Copy ``source`` to ``workdir/repos/<name>``, check out ``commit`` (default HEAD), pin it."""
    kind = source_kind(source)
    name = repo_name(source)
    dest = Path(workdir) / "repos" / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    synthetic = False
    if kind == "url":
        _git(None, "clone", "-q", source, str(dest))
    elif kind == "path":
        src = Path(source).resolve()
        if (src / ".git").exists():
            _refuse_dirty(src)
            _git(None, "clone", "-q", "--no-hardlinks", str(src), str(dest))
        else:
            _copy_tree(src, dest)
            synthetic = True
    else:  # zip
        with tempfile.TemporaryDirectory(prefix="factory-zip-") as tmp:
            with zipfile.ZipFile(source) as zf:
                _safe_extract(zf, Path(tmp))
            root = _zip_root(Path(tmp))
            if (root / ".git").exists():
                _refuse_dirty(root)
                _git(None, "clone", "-q", "--no-hardlinks", str(root), str(dest))
            else:
                _copy_tree(root, dest)
                synthetic = True
    if synthetic:
        if commit:
            raise RepoLoadError("a commit was given but the repo has no git history")
        _make_synthetic_history(dest)
    elif commit:
        _git(dest, "checkout", "-q", "--detach", commit)
    sha = _git(dest, "rev-parse", "HEAD^{commit}")
    from .inspector import detect_language

    spec = RepoSpec(name=name, source=source, commit_sha=sha, local_path=str(dest.resolve()),
                    language=detect_language(dest), synthetic_history=synthetic)
    log.info("loaded %s at %s%s", name, sha[:12], " (synthetic history)" if synthetic else "")
    return spec


def _refuse_dirty(path: Path) -> None:
    status = _git(path, "status", "--porcelain", "--untracked-files=no")
    if status:
        raise RepoLoadError(f"working tree at {path} has uncommitted changes; commit or stash them first:\n{status}")
    untracked = _git(path, "status", "--porcelain")
    if untracked:
        log.warning("untracked files in %s are ignored (only committed files are loaded)", path)


def _copy_tree(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".venv", "*.pyc"))


def _safe_extract(zf: zipfile.ZipFile, out: Path) -> None:
    for member in zf.namelist():
        target = (out / member).resolve()
        if not target.is_relative_to(out.resolve()):
            raise RepoLoadError(f"zip entry escapes the target folder: {member}")
    zf.extractall(out)


def _zip_root(folder: Path) -> Path:
    """A zip with one top-level folder (and nothing else) is unwrapped."""
    entries = [p for p in folder.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return folder


def _make_synthetic_history(dest: Path) -> None:
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "synthetic: imported without history",
         env=SYNTHETIC_GIT_ENV)


def export_clean(spec: RepoSpec, out_dir: Path) -> Path:
    """Write a clean copy of the pinned commit (``git archive``) to ``out_dir``."""
    import tarfile

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir.parent / f".{out_dir.name}.tar"
    _git(spec.local_path, "archive", "--format=tar", "-o", str(tar_path), spec.commit_sha)
    with tarfile.open(tar_path) as tf:
        tf.extractall(out_dir, filter="data")
    tar_path.unlink()
    return out_dir
