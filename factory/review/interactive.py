"""``factory review``: step through tasks one by one and decide.

Keys::

    a  approve           d  delete (asks for a reason)     s  skip to the next task
    i  instruction       p  reference patch                 t  hidden tests
    g  gates + logs      e  edit (re-validated, disclosed)  q  quit
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console
from rich.rule import Rule

from . import render
from .store import TaskEntry, TaskStore

KEYS = {
    "a": "approve", "d": "delete", "s": "skip", "i": "instruction", "p": "patch", "t": "tests",
    "g": "gates", "e": "edit", "q": "quit",
}
MENU = "  ".join(f"[bold]{k}[/]={v}" for k, v in KEYS.items())

Ask = Callable[[str], str]
EditFn = Callable[[TaskEntry], str | None]  # returns a status line, or None when the edit was cancelled


def review_loop(store: TaskStore, console: Console, ask: Ask, *, status: str = "pending",
                edit: EditFn | None = None) -> dict[str, int]:
    """Run the review loop. ``ask`` reads one line of input (injectable for tests)."""
    entries = store.entries(status)
    done = {"approved": 0, "deleted": 0, "skipped": 0, "edited": 0}
    if not entries:
        console.print(f"[green]Nothing to review: no {status} tasks in {store.root}[/]")
        return done
    console.print(f"[bold]{len(entries)} {status} task(s) to review[/] · reviewer: {store.reviewer}")
    for i, entry in enumerate(entries, 1):
        console.print(Rule(f"task {i}/{len(entries)}"))
        render.show(console, entry, "summary")
        render.show(console, entry, "instruction")
        while True:
            console.print(MENU)
            key = (ask("choice> ") or "").strip().lower()[:1]
            if key == "q":
                _summary(console, done)
                return done
            if key == "s" or key == "":
                done["skipped"] += 1
                break
            if key == "a":
                note = ask("note (optional)> ").strip() or None
                store.approve(entry.task_id, note)
                console.print(f"[green]approved[/] {entry.task_id}")
                done["approved"] += 1
                break
            if key == "d":
                reason = ask("why delete? (required)> ").strip()
                if not reason:
                    console.print("[red]a reason is required; not deleted[/]")
                    continue
                store.delete(entry.task_id, reason)
                console.print(f"[red]deleted[/] {entry.task_id} (restore with: factory tasks restore {entry.task_id})")
                done["deleted"] += 1
                break
            if key in ("i", "p", "t", "g"):
                render.show(console, entry, KEYS[key])
                continue
            if key == "e":
                if edit is None:
                    console.print("[yellow]editing is not available here; use: factory tasks edit TASK_ID[/]")
                    continue
                line = edit(entry)
                if line:
                    console.print(line)
                    done["edited"] += 1
                    entry = store.get(entry.task_id)
                    render.show(console, entry, "summary")
                continue
            console.print(f"[yellow]unknown key {key!r}[/]")
    _summary(console, done)
    return done


def _summary(console: Console, done: dict[str, int]) -> None:
    console.print(Rule("review finished"))
    console.print("  ".join(f"{k}: [bold]{v}[/]" for k, v in done.items()))
