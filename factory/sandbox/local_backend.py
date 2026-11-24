"""Local sandbox backend: a temp folder on the host. NO ISOLATION.

For unit tests and development only. Every use logs a warning.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from ..config import SandboxConfig
from ..errors import BuildError, SandboxRuntimeError
from ..models import EnvConfig, ExecResult, ImageRef, RepoSpec
from .base import (base_commit_sh, FIXED_ENV, SCRATCH_DIR, Sandbox, SandboxFactory, git_archive, image_tag,
                   truncate_output)

log = logging.getLogger("factory.sandbox.local")

_WARNING = "LocalSandbox has NO isolation (no container, network allowed); use only for tests/dev"


class LocalSandboxFactory(SandboxFactory):
    backend_name = "local"

    def __init__(self, cache_dir: Path, cfg: SandboxConfig | None = None) -> None:
        self.cache_dir = Path(cache_dir) / "local-images"
        self.cfg = cfg or SandboxConfig()

    def build_image(self, spec: RepoSpec, env: EnvConfig) -> ImageRef:
        log.warning(_WARNING)
        tag = image_tag(spec, env)
        dest = self.cache_dir / tag.replace(":", "_").replace("/", "_")
        if (dest / ".git").exists():
            return ImageRef(tag=tag, backend="local", location=str(dest))
        tmp = Path(tempfile.mkdtemp(prefix="build-", dir=self._ensure_cache()))
        try:
            tar_path = git_archive(spec, tmp.parent / f"{tmp.name}.tar")
            with tarfile.open(tar_path) as tf:
                # the "data" filter blocks unsafe paths; it exists from 3.11.4/3.12 (check the feature, not the version)
                tf.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
            tar_path.unlink()
            setup = env.setup_commands if self.cfg.local_run_setup else []
            if env.setup_commands and not setup:
                log.warning("local backend skips setup commands (set sandbox.local_run_setup = true to run "
                            "them on the host): %s", env.setup_commands)
            for cmd in [base_commit_sh(env), *setup]:
                r = subprocess.run(["sh", "-c", cmd], cwd=tmp, capture_output=True, text=True,
                                   env=_host_env(), timeout=self.cfg.build_timeout_s)
                if r.returncode != 0:
                    raise BuildError(f"setup command failed: {cmd}", r.stdout + r.stderr)
            if dest.exists():
                shutil.rmtree(dest)
            tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return ImageRef(tag=tag, backend="local", location=str(dest))

    def _ensure_cache(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir

    def build_derived_image(self, base: ImageRef, name: str, commands: list[str],
                            files: dict[str, Path] | None = None) -> ImageRef:
        if commands or files:
            log.warning("local backend does not run image commands for %s (the host must already have them): %s",
                        name, commands)
        return base

    def new_sandbox(self, image: ImageRef, *, network: bool = False) -> "LocalSandbox":
        log.warning(_WARNING)
        if image.location is None:
            raise SandboxRuntimeError("local image has no location")
        root = Path(tempfile.mkdtemp(prefix="factory-sb-"))
        repo = root / "repo"
        shutil.copytree(image.location, repo, symlinks=True)
        return LocalSandbox(root, repo, self.cfg.output_limit_bytes)


def _host_env() -> dict[str, str]:
    env = dict(os.environ)
    # Make `python` resolve to the interpreter running the factory.
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return env


class LocalSandbox(Sandbox):
    def __init__(self, root: Path, repo: Path, limit: int) -> None:
        self.root = root
        self.repo = repo
        self.repo_root = str(repo)
        self.limit = limit
        self.tmp = repo / SCRATCH_DIR / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._closed = False

    def _path(self, path: str) -> Path:
        p = (self.repo / path).resolve()
        if not p.is_relative_to(self.repo.resolve()):
            raise SandboxRuntimeError(f"path escapes the sandbox: {path}")
        return p

    def exec(self, argv: list[str], *, timeout_s: int, env: dict | None = None,
             cwd: str | None = None, secret_env: dict | None = None) -> ExecResult:
        if self._closed:
            raise SandboxRuntimeError("sandbox is closed")
        full_env = {**_host_env(), **FIXED_ENV, "TMPDIR": str(self.tmp), **(env or {}), **(secret_env or {})}
        workdir = self._path(cwd) if cwd and not cwd.startswith("/") else self.repo
        start = time.monotonic()
        proc = subprocess.Popen(argv, cwd=workdir, env=full_env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)  # whole process group
                else:
                    proc.kill()  # Windows fallback
            except (ProcessLookupError, OSError):
                pass
            out, err = proc.communicate()
        code = proc.returncode if not timed_out else -9
        return ExecResult(argv=list(argv), exit_code=code, stdout=truncate_output(out, self.limit),
                          stderr=truncate_output(err, self.limit),
                          duration_s=round(time.monotonic() - start, 3), timed_out=timed_out)

    def write_file(self, path: str, content: str | bytes) -> None:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
        else:
            p.write_bytes(content)

    def read_file(self, path: str) -> bytes:
        p = self._path(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        return p.read_bytes()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            shutil.rmtree(self.root, ignore_errors=True)
