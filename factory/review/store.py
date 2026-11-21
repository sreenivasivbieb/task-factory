"""Task store: a folder of task packages plus the human review trail.

Layout::

    <store>/
      <task_id>/            one package per valid task (see packaging/writer.py)
      .trash/<task_id>/     soft-deleted packages (restorable)
      .review/log.jsonl     append-only review events (the source of truth for status)

Status of a task = the effect of its latest event:
``add`` / ``restore`` / ``edit`` -> pending, ``approve`` -> approved, ``delete`` -> deleted.
A package with no events is pending. Only approved tasks are published.
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
Action = Literal["add", "approve", "delete", "restore", "edit", "note"]

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

    def record(self, task_id: str, action: Action, reason: str | None = None) -> ReviewEvent:
        event = ReviewEvent(task_id=task_id, action=action, reviewer=self.reviewer, reason=reason)
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
