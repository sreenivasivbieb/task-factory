"""JSON-lines subprocess bridge between the Task Factory terminal and the task pipeline.

The terminal plugin starts ``python -m factory.terminal_bridge`` in the project folder,
writes one request line and reads events until a ``result``::

    -> {"op": "run", "task_ids": [...], "model": "provider/model"}
    <- {"type": "progress", "text": "..."}          any number, shown live
    <- {"type": "prompt", "text": "..."}            generate only: answer with {"answer": "..."}
    <- {"type": "result", "ok": true, "text": "...", "data": {...}}

A request without ``op`` is the original ``/generate-task`` request
(``{"request": ..., "model": ...}``).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shlex
import sys
from contextlib import redirect_stdout
from pathlib import Path

from . import terminal_ops as ops
from .errors import FactoryError, PackageError
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


def generate(request: str, model: str, reply, session_id: str | None = None) -> None:
    """Draft a task with the terminal's model, validate it, and add it to the project's store as pending.

    Every attempt, valid or not, is logged for the session it came from (``generations.jsonl``).
    """
    from .commands import cmd_tasks_add
    from .generation.author import draft_task
    from .models import RepoSpec

    root = ops.project_root()
    cfg = ops.ensure_setup(root)
    wd = Workdir(cfg.workdir)
    spec = RepoSpec.load(wd.repo_spec)
    entry = {"session_id": session_id, "model": model, "request": request, "verdict": "error"}
    args = argparse.Namespace(
        model=model, prompt=request, file=select_context(Path(spec.local_path), request),
        force=False, dry_run=False, pending=True, store=None, author=f"terminal:{model}",
    )
    try:
        print(f"asking {model} for a task on {spec.name}@{spec.short_sha}")
        args.draft = draft_task(spec, wd.drafts, args, reply=reply)
        entry["draft"] = str(args.draft)
        meta_file = args.draft / "task.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta.update(session_id=session_id, request=request)
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
        print("validating the draft in Docker (gates G0-G6)")
        before = {t["task_id"] for t in ops.tasks(root)}
        if cmd_tasks_add(args, cfg):
            entry.update(ops.last_rejection(wd), verdict="rejected")
            raise PackageError(f"Draft rejected by Task Factory. Draft: {args.draft}")
        new = [t for t in ops.tasks(root) if t["task_id"] not in before]
        entry.update(verdict="valid", task_id=new[0]["task_id"] if new else None)
    except FactoryError as exc:
        entry.setdefault("reason", str(exc))
        raise
    finally:
        ops.log_generation(wd, entry)
    print("saved as pending: open /tasks to review it, or /run-task to try a model on it")
    print(f"(terminal: factory tasks list --workdir {shlex.quote(str(wd.root))})")


def handle(request: dict, reply):
    """Run one request; returns the result's ``data``."""
    op = request.get("op", "generate")
    root = ops.project_root()
    if op == "generate":
        generate(request["request"], request["model"], reply, request.get("session_id"))
        return None
    if op == "status":
        return ops.status(root)
    if op == "setup":
        return ops.setup(root, force=bool(request.get("force")))
    if op == "tasks":
        return ops.tasks(root, request.get("status", "all"))
    if op == "task":
        return ops.task_detail(root, request["task_id"])
    if op == "approve":
        return ops.approve(root, request["task_id"])
    if op == "delete":
        return ops.delete(root, request["task_id"], request.get("reason", ""))
    if op == "run":
        ops.ensure_setup(root)
        return ops.run(root, list(request["task_ids"]), request["model"])
    if op == "runs":
        return ops.runs(root)
    if op == "overview":
        return ops.overview(root, request.get("session_id"))
    if op == "benchmark":
        return ops.benchmark(root)
    if op == "attempts":
        return ops.attempts(root, request["run_id"])
    if op == "attempt":
        return ops.attempt_detail(root, request["run_id"], request["task_id"], int(request["attempt"]))
    raise PackageError(f"unknown request {op!r}")


class _Progress(io.TextIOBase):
    """Captured stdout that also forwards every complete line as a progress event."""

    def __init__(self, send) -> None:
        self.send, self.partial, self.lines = send, "", []

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self.partial += text
        while "\n" in self.partial:
            line, self.partial = self.partial.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self.partial:
            self._emit(self.partial)
            self.partial = ""

    def _emit(self, line: str) -> None:
        if line.strip():
            self.lines.append(line)
            self.send({"type": "progress", "text": line})


class _LogToProgress(logging.Handler):
    def __init__(self, out: _Progress) -> None:
        super().__init__(logging.INFO)
        self.out = out

    def emit(self, record: logging.LogRecord) -> None:
        self.out.write(record.getMessage() + "\n")


def main() -> None:
    protocol = sys.stdout
    os.environ.setdefault("COLUMNS", "160")  # rich wraps at 80 columns when not on a terminal

    def send(message):
        print(json.dumps(message), file=protocol, flush=True)

    def reply(prompt):
        send({"type": "prompt", "text": prompt})
        answer = json.loads(sys.stdin.readline())
        if answer.get("error"):  # the terminal could not reach the model (quota, login, network)
            raise PackageError(f"model request failed: {answer['error']}")
        return answer["answer"]

    output = _Progress(send)
    logger = logging.getLogger("factory")
    logger.setLevel(logging.INFO)
    handler = _LogToProgress(output)
    logger.addHandler(handler)
    try:
        request = json.loads(sys.stdin.readline())
        with redirect_stdout(output):
            data = handle(request, reply)
        output.flush()
        send({"type": "result", "ok": True, "text": "\n".join(output.lines[-30:]).strip(), "data": data})
    except (FactoryError, OSError, ValueError, KeyError, TypeError) as exc:
        output.flush()
        # Keep what the pipeline printed (e.g. the gate and reject reason), not only the summary.
        printed = "\n".join(output.lines[-12:])
        send({"type": "result", "ok": False, "text": f"{printed}\n\nTask Factory: {exc}".strip(),
              "error": f"Task Factory: {exc}"})
    finally:
        logger.removeHandler(handler)


if __name__ == "__main__":
    main()
