"""Task store: a folder of task packages plus the human review trail.

Layout::

    <store>/
      <task_id>/            one package per valid task (see packaging/writer.py)
      .trash/<task_id>/     soft-deleted packages (restorable)
      .review/log.jsonl     append-only review events (the source of truth for status)

Status of a task = the effect of its latest event:
``add`` / ``restore`` / ``edit`` -> pending, ``approve`` -> approved, ``delete`` -> deleted.
A package with no events is pending. Only approved tasks are published.

Assignments (``assign`` / ``unassign`` / ``result``) do not change the status: they
record which agent + model should attempt a task, and what happened when it did.
An assignment is open until a ``result`` or ``unassign`` event for the same
task, agent and model.
"""

from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..errors import ConfigError, PackageError
from ..models import Model, TaskPackage

Status = Literal["pending", "approved", "deleted"]
Action = Literal["add", "approve", "delete", "restore", "edit", "note", "assign", "unassign", "result"]

_STATUS_AFTER: dict[str, Status] = {"add": "pending", "restore": "pending", "edit": "pending",
                                    "approve": "approved", "delete": "deleted"}

TRASH = ".trash"
REVIEW_DIR = ".review"


class ReviewEvent(Model):
    ts: float = Field(default_factory=lambda: round(time.time(), 3))
    task_id: str
    action: Action
    reviewer: str
    reason: str | None = None
    agent: str | None = None
    model: str | None = None
    run_id: str | None = None
    branch: str | None = None


@dataclass
class Assignment:
    task_id: str
    agent: str
    model: str | None
    assigned_by: str
    ts: float
    note: str | None
    result: "ReviewEvent | None" = None

    @property
    def open(self) -> bool:
        return self.result is None

    @property
    def label(self) -> str:
        return f"{self.agent}:{self.model}" if self.model else self.agent


def default_reviewer() -> str:
    """The reviewer's name: git user.name, else the login name. Stays on this machine."""
    try:
        r = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


@dataclass
class TaskEntry:
    task_id: str
    path: Path
    status: Status
    meta: TaskPackage
    events: list[ReviewEvent]

    @property
    def author(self) -> str:
        gb = self.meta.generated_by
        if gb.model == "human":
            return f"human ({gb.author})" if gb.author else "human"
        return gb.model

    @property
    def last_reason(self) -> str | None:
        for e in reversed(self.events):
            if e.reason:
                return e.reason
        return None


def read_meta(pkg_dir: Path) -> TaskPackage:
    try:
        return TaskPackage.model_validate(tomllib.loads((pkg_dir / "task.toml").read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise PackageError(f"{pkg_dir}: cannot read task.toml: {exc}") from exc


class TaskStore:
    def __init__(self, root: Path | str, reviewer: str | None = None) -> None:
        self.root = Path(root)
        self.reviewer = reviewer or default_reviewer()

    # ------------------------------------------------------------------ paths + log

    @property
    def log_path(self) -> Path:
        return self.root / REVIEW_DIR / "log.jsonl"

    @property
    def trash(self) -> Path:
        return self.root / TRASH

    def events(self) -> list[ReviewEvent]:
        if not self.log_path.exists():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(ReviewEvent.model_validate_json(line))
        return out

    def record(self, task_id: str, action: Action, reason: str | None = None, **extra) -> ReviewEvent:
        event = ReviewEvent(task_id=task_id, action=action, reviewer=self.reviewer, reason=reason, **extra)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        return event

    # ------------------------------------------------------------------ queries

    def _status(self, events: list[ReviewEvent], in_trash: bool) -> Status:
        status: Status = "pending"
        for e in events:
            status = _STATUS_AFTER.get(e.action, status)
        if in_trash:
            return "deleted"
        return "pending" if status == "deleted" else status

    def entries(self, status: str = "all") -> list[TaskEntry]:
        by_task: dict[str, list[ReviewEvent]] = {}
        for e in self.events():
            by_task.setdefault(e.task_id, []).append(e)
        out = []
        for base, in_trash in ((self.root, False), (self.trash, True)):
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if d.name.startswith(".") or not (d / "task.toml").exists():
                    continue
                events = by_task.get(d.name, [])
                st = self._status(events, in_trash)
                if status in ("all", st):
                    out.append(TaskEntry(d.name, d, st, read_meta(d), events))
        return out

    def get(self, task_id: str) -> TaskEntry:
        matches = [e for e in self.entries() if e.task_id == task_id]
        if not matches:
            partial = [e for e in self.entries() if e.task_id.startswith(task_id) or task_id in e.task_id]
            if len(partial) == 1:
                return partial[0]
            if len(partial) > 1:
                raise ConfigError(f"'{task_id}' matches {len(partial)} tasks; use the full task ID")
            raise ConfigError(f"no task '{task_id}' in {self.root}")
        return matches[0]

    # ------------------------------------------------------------------ actions

    def approve(self, task_id: str, note: str | None = None) -> TaskEntry:
        entry = self.get(task_id)
        if entry.status == "deleted":
            raise ConfigError(f"{entry.task_id} is deleted; restore it first")
        self.record(entry.task_id, "approve", note)
        return self.get(entry.task_id)

    def delete(self, task_id: str, reason: str) -> TaskEntry:
        if not reason or not reason.strip():
            raise ConfigError("a reason is required to delete a task (it is disclosed in the report)")
        entry = self.get(task_id)
        if entry.status == "deleted":
            return entry
        self.trash.mkdir(parents=True, exist_ok=True)
        dest = self.trash / entry.task_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(entry.path), dest)
        self.record(entry.task_id, "delete", reason.strip())
        return self.get(entry.task_id)

    def restore(self, task_id: str) -> TaskEntry:
        entry = self.get(task_id)
        if entry.status != "deleted":
            raise ConfigError(f"{entry.task_id} is not deleted")
        dest = self.root / entry.task_id
        if dest.exists():
            raise ConfigError(f"{dest} already exists")
        shutil.move(str(entry.path), dest)
        self.record(entry.task_id, "restore")
        return self.get(entry.task_id)

    def note(self, task_id: str, text: str) -> None:
        self.record(self.get(task_id).task_id, "note", text)

    # ------------------------------------------------------------------ assignments

    def assign(self, task_id: str, agent: str, model: str | None, note: str | None = None) -> Assignment:
        entry = self.get(task_id)
        if entry.status == "deleted":
            raise ConfigError(f"{entry.task_id} is deleted; restore it before assigning")
        for a in self.assignments():
            if a.task_id == entry.task_id and a.agent == agent and a.model == model:
                return a  # already open
        self.record(entry.task_id, "assign", note, agent=agent, model=model)
        return next(a for a in self.assignments() if a.task_id == entry.task_id and a.agent == agent
                    and a.model == model)

    def unassign(self, task_id: str, agent: str | None = None, model: str | None = None) -> int:
        tid = self.get(task_id).task_id
        closed = 0
        for a in self.assignments():
            if a.task_id == tid and (agent is None or a.agent == agent) and (model is None or a.model == model):
                self.record(tid, "unassign", agent=a.agent, model=a.model)
                closed += 1
        return closed

    def record_result(self, task_id: str, agent: str, model: str | None, summary: str,
                      run_id: str | None = None, branch: str | None = None) -> ReviewEvent:
        return self.record(task_id, "result", summary, agent=agent, model=model, run_id=run_id, branch=branch)

    def assignments(self, open_only: bool = True) -> list[Assignment]:
        """Assignments in the order they were made; open ones only unless ``open_only=False``."""
        out: list[Assignment] = []
        for e in self.events():
            if e.action == "assign":
                out.append(Assignment(e.task_id, e.agent or "", e.model, e.reviewer, e.ts, e.reason))
            elif e.action in ("result", "unassign"):
                for a in out:
                    if a.task_id == e.task_id and a.agent == e.agent and a.model == e.model and a.open:
                        a.result = e
        return [a for a in out if a.open] if open_only else out

    def summary(self) -> dict:
        """Counts plus every human deletion with its reason (for the report and the submission)."""
        entries = self.entries()
        counts = {s: sum(1 for e in entries if e.status == s) for s in ("pending", "approved", "deleted")}
        deleted = [{"task_id": e.task_id, "reason": e.last_reason, "by": e.events[-1].reviewer if e.events else None}
                   for e in entries if e.status == "deleted"]
        human = [e.task_id for e in entries if e.meta.generated_by.model == "human"]
        edited = [{"task_id": e.task_id, "reason": e.meta.manual_edit_reason} for e in entries if e.meta.manual_edit]
        return {"counts": counts, "deleted": deleted, "human_authored": human, "manual_edits": edited}

    def write_summary(self, path: Path) -> Path:
        path.write_text(json.dumps(self.summary(), indent=2) + "\n", encoding="utf-8")
        return path
