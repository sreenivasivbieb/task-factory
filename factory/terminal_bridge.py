"""JSON-lines subprocess bridge between the Task Factory terminal and the task pipeline."""
from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from .commands import cmd_inspect, cmd_tasks_add
from .config import FactoryConfig
from .errors import FactoryError, PackageError
from .generation.author import draft_task
from .repo.loader import load_repo
from .state import Workdir
from .workspace import Workspace


def select_context(root: Path, request: str) -> list[str]:
    workspace = Workspace(root)
    words = {word.strip(".,?!:").casefold() for word in request.split() if len(word) > 3}
    ranked = sorted(workspace.files, key=lambda name: (
        -sum(word in name.casefold() for word in words),
        Path(name).name.lower() not in {"readme.md", "pyproject.toml", "package.json", "go.mod", "cargo.toml"},
        name.count("/"), name,
    ))
    files = []
    budget = 19_000
    for name in ranked[:40]:
        try:
            if workspace.locate(name).stat().st_size > budget:
                continue
            text = workspace.read(name)
        except (FactoryError, OSError, UnicodeError):
            continue
        files.append(name)
        budget -= len(text)
        if len(files) == 8:
            break
    return files


def generate(request: str, model: str, reply) -> None:
    root = Path.cwd()
    git = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                         capture_output=True, text=True, check=False)
    if git.returncode == 0:
        root = Path(git.stdout.strip())
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    parent = state / "task-factory" / "terminal"
    parent.mkdir(parents=True, exist_ok=True)
    wd = Workdir(Path(tempfile.mkdtemp(prefix="task-", dir=parent)))
    cfg = FactoryConfig(workdir=str(wd.root))
    source = root.as_uri() if (root / ".git").exists() else str(root)
    spec = load_repo(source, wd.root)
    spec.save(wd.repo_spec)
    args = argparse.Namespace(
        model=model, prompt=request, file=select_context(Path(spec.local_path), request),
        force=False, dry_run=False, pending=True, store=None, author=f"terminal:{model}",
    )
    if cmd_inspect(args, cfg):
        raise PackageError(f"Could not prepare the repository test environment. Work directory: {wd.root}")
    args.draft = draft_task(spec, wd.drafts, args, reply=reply)
    if cmd_tasks_add(args, cfg):
        raise PackageError(f"Draft rejected by Task Factory. Draft: {args.draft}")
    print(f"Review with: factory tasks list --status pending --workdir {shlex.quote(str(wd.root))}")


def main() -> None:
    protocol = sys.stdout

    def send(message):
        print(json.dumps(message), file=protocol, flush=True)

    def reply(prompt):
        send({"type": "prompt", "text": prompt})
        return json.loads(sys.stdin.readline())["answer"]

    try:
        request = json.loads(sys.stdin.readline())
        output = io.StringIO()
        with redirect_stdout(output):
            generate(request["request"], request["model"], reply)
        send({"type": "result", "text": output.getvalue().strip()})
    except (FactoryError, OSError, ValueError, KeyError) as exc:
        send({"type": "result", "text": f"Task Factory: {exc}"})


if __name__ == "__main__":
    main()
