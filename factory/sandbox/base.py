"""Sandbox interface (design doc section 7).

A sandbox runs commands against a pinned repo in an isolated, repeatable
environment. Every gate and every solver run uses one.
"""

from __future__ import annotations

import abc
import hashlib
import json
import logging
import subprocess
from pathlib import Path

from ..errors import BuildError
from ..models import EnvConfig, ExecResult, ImageRef, RepoSpec, slugify

log = logging.getLogger("factory.sandbox")

# Bump when the generated Dockerfile changes, so cached images are rebuilt.
PRELUDE_VERSION = "1"

# Fixed environment inside every sandbox.
FIXED_ENV = {"TZ": "UTC", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "0",
             "PYTHONDONTWRITEBYTECODE": "1", "CI": "1"}

# Files the factory writes inside the repo; ignored by git in the sandbox.
SCRATCH_DIR = ".factory"

TRUNCATION_MARKER = "\n...[truncated by factory: output over {limit} bytes]...\n"


def image_tag(spec: RepoSpec, env: EnvConfig) -> str:
    """Tag = hash of commit SHA, base image and setup commands (plus prelude version)."""
    payload = json.dumps([spec.commit_sha, env.base_image, env.setup_commands, PRELUDE_VERSION])
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"factory-{slugify(spec.name, 40)}:{digest}"


def truncate_output(data: bytes, limit: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="replace") + TRUNCATION_MARKER.format(limit=limit)


def git_archive(spec: RepoSpec, out_tar: Path) -> Path:
    """Write ``git archive <commit_sha>`` of the pinned repo to ``out_tar``."""
    r = subprocess.run(["git", "-C", spec.local_path, "archive", "--format=tar", "-o", str(out_tar),
                        spec.commit_sha], capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"git archive failed for {spec.commit_sha}", r.stderr)
    return out_tar


BASE_COMMIT_SH = (
    "git init -q && git add -A && "
    "git -c user.name=factory -c user.email=factory@localhost -c commit.gpgsign=false "
    "commit -q --allow-empty -m base && "
    f"mkdir -p {SCRATCH_DIR} && printf '{SCRATCH_DIR}/\\n' >> .git/info/exclude"
)


def render_dockerfile(env: EnvConfig) -> str:
    """Generated Dockerfile: base image, git + non-root user, repo archive, setup commands."""
    lines = [
        f"FROM {env.base_image}",
        "USER root",
        "RUN command -v git >/dev/null 2>&1 || "
        "(apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates "
        "&& rm -rf /var/lib/apt/lists/*) || apk add --no-cache git || yum install -y git",
        "RUN id factory >/dev/null 2>&1 || useradd -m -u 10001 factory 2>/dev/null "
        "|| adduser -D -u 10001 factory 2>/dev/null || true",
        f"WORKDIR {env.workdir}",
        f"ADD repo.tar {env.workdir}/",
        f"RUN {BASE_COMMIT_SH}",
    ]
    lines += [f"RUN {cmd}" for cmd in env.setup_commands]
    lines.append(f"RUN (id factory >/dev/null 2>&1 && chown -R factory {env.workdir}) || true")
    return "\n".join(lines) + "\n"


class Sandbox(abc.ABC):
    """One isolated working copy of the pinned repo. Use as a context manager."""

    @abc.abstractmethod
    def exec(self, argv: list[str], *, timeout_s: int, env: dict | None = None,
             cwd: str | None = None) -> ExecResult:
        """Run ``argv``. Never raises for a non-zero exit code."""

    @abc.abstractmethod
    def write_file(self, path: str, content: str | bytes) -> None:
        """Write a file; ``path`` is relative to the repo root."""

    @abc.abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read a file; ``path`` is relative to the repo root. Raises FileNotFoundError."""

    def apply_patch(self, diff: str) -> ExecResult:
        """``git apply --check`` then ``git apply``. A bad patch returns a non-zero result."""
        patch_path = f"{SCRATCH_DIR}/patch.diff"
        if not diff.strip():
            return ExecResult(argv=["git", "apply"], exit_code=0, stdout="empty patch: nothing to apply")
        if not diff.endswith("\n"):
            diff += "\n"
        self.write_file(patch_path, diff)
        check = self.exec(["git", "apply", "--check", patch_path], timeout_s=60)
        if not check.ok:
            return check
        return self.exec(["git", "apply", patch_path], timeout_s=60)

    @abc.abstractmethod
    def close(self) -> None:
        """Remove the sandbox. Safe to call twice."""

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SandboxFactory(abc.ABC):
    @abc.abstractmethod
    def build_image(self, spec: RepoSpec, env: EnvConfig) -> ImageRef:
        """Build (or reuse) the image for a pinned repo and environment."""

    @abc.abstractmethod
    def new_sandbox(self, image: ImageRef, *, network: bool = False) -> Sandbox:
        """Start a fresh sandbox from an image."""
