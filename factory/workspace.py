"""Index readable project files for task-generation context."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import PackageError

MAX_FILES = 4_000
MAX_TEXT = 24_000
PRIVATE_DIRS = {".git", ".aws", ".ssh", ".gnupg", ".config", ".local", "workdir"}


def _skip_dirs() -> set[str]:
    from .ecosystems import registry

    build = {p.rstrip("/") for p in registry().artifacts() if p.endswith("/") and "*" not in p}
    return PRIVATE_DIRS | build
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def visible(path: Path) -> bool:
    return (
        not any(part in _skip_dirs() for part in path.parts)
        and not path.name.startswith(".env")
        and path.name not in {".netrc", ".npmrc", "credentials.json", "auth.json"}
        and path.suffix.lower() not in PRIVATE_SUFFIXES
    )


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.scan()

    def scan(self) -> None:
        self.files: list[str] = []
        rg = shutil.which("rg")
        if rg:
            with tempfile.TemporaryFile() as output:
                subprocess.run(
                    [rg, "--no-config", "--files", "--null"], cwd=self.root,
                    stdout=output, stderr=subprocess.DEVNULL, timeout=20, check=False,
                )
                output.seek(0)
                names = output.read(2 * 1024 * 1024).split(b"\0")[:-1]
            paths = (os.fsdecode(name) for name in names)
        else:
            paths = self._walk()
        for name in paths:
            if visible(Path(name)):
                self.files.append(name)
            if len(self.files) == MAX_FILES:
                break
        self.files.sort()

    def _walk(self):
        for folder, dirs, files in os.walk(self.root):
            dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name not in _skip_dirs())
            for name in sorted(files):
                if not name.startswith("."):
                    yield (Path(folder) / name).relative_to(self.root).as_posix()

    def locate(self, name: str) -> Path:
        rel = Path(name)
        if (rel.is_absolute() or not rel.parts or ".." in rel.parts
                or not visible(rel) or any(ord(c) < 32 for c in name)):
            raise PackageError("Choose a project-relative path.")
        path = self.root / rel
        if path.resolve() != path:
            raise PackageError("Choose a path that does not cross a symlink.")
        return path

    def read(self, name: str) -> str:
        path = self.locate(name)
        if not path.is_file():
            raise PackageError(f"File not found: {name}")
        with path.open(encoding="utf-8") as stream:
            return stream.read(MAX_TEXT + 1)
