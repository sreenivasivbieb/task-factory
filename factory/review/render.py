"""Readable terminal views of task packages (uses ``rich``)."""

from __future__ import annotations

import json
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .store import TaskEntry

STATUS_STYLE = {"pending": "yellow", "approved": "green", "deleted": "red"}
def status_text(status: str) -> Text:
    return Text(status, style=f"bold {STATUS_STYLE.get(status, 'white')}")


def tasks_table(entries: list[TaskEntry], assigned: dict[str, list[str]] | None = None) -> Table:
    assigned = assigned or {}
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold", expand=False)
    for col in ("#", "task", "status", "kind", "tags", "tests", "mutants", "author", "assigned"):
        t.add_column(col, overflow="fold", no_wrap=(col == "task"))
    for i, e in enumerate(entries, 1):
        v = e.meta.validation
        score = f"{v.mutants_caught}/{v.mutants_run}" if v.mutants_run else "-"
        t.add_row(str(i), e.task_id, status_text(e.status), e.meta.kind, ", ".join(e.meta.tags),
                  str(len(e.meta.fail_to_pass)), score, e.author, ", ".join(assigned.get(e.task_id, [])) or "-")
    return t


def _gate_line(pkg: Path) -> Text:
    report_path = pkg / "validation" / "report.json"
    text = Text()
    if not report_path.exists():
        return Text("no validation report", style="red")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for g in report.get("gates", []):
        text.append(f" {g['gate']} ", style="black on green" if g["passed"] else "white on red")
        text.append(" ")
    return text


def header(e: TaskEntry) -> Panel:
    m = e.meta
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    grid.add_row("status", status_text(e.status))
    grid.add_row("repo", f"{m.repo_name} @ {m.commit_sha[:12]}  ({m.language or 'unknown'})")
    grid.add_row("kind / tags", f"{m.kind}  ·  {', '.join(m.tags) or '-'}")
    grid.add_row("author", e.author)
    grid.add_row("touches", ", ".join(m.touched_files) or "-")
    grid.add_row("tests", f"{len(m.fail_to_pass)} fail_to_pass · {len(m.pass_to_pass)} pass_to_pass"
                 + (f" · {len(m.known_failures)} known failures" if m.known_failures else ""))
    v = m.validation
    if v.mutants_run:
        grid.add_row("mutants", f"{v.mutants_caught}/{v.mutants_run} caught (score {v.mutation_score})")
    grid.add_row("gates", _gate_line(e.path))
    if m.manual_edit:
        grid.add_row("manual edit", Text(m.manual_edit_reason or "(no reason)", style="magenta"))
    for ev in e.events[-3:]:
        who = f" [{ev.agent}{':' + ev.model if ev.model else ''}]" if ev.agent else ""
        extra = f" → branch {ev.branch}" if ev.branch else ""
        grid.add_row("review", f"{ev.action}{who} by {ev.reviewer}" + (f": {ev.reason}" if ev.reason else "") + extra)
    return Panel(grid, title=f"[bold]{e.task_id}", border_style=STATUS_STYLE.get(e.status, "white"))


def instruction(e: TaskEntry) -> Panel:
    text = (e.path / "instruction.md").read_text(encoding="utf-8")
    return Panel(Markdown(text), title="instruction.md  (the only file the solver sees)", border_style="cyan")


def patch(e: TaskEntry) -> Panel:
    diff = (e.path / "solution" / "solution.diff").read_text(encoding="utf-8")
    return Panel(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True),
                 title="solution/solution.diff  (hidden reference)", border_style="yellow")


def tests(e: TaskEntry) -> Panel:
    root = e.path / "tests" / "files"
    parts = []
    for rel in e.meta.test_files:
        p = root / rel
        lexer = Syntax.guess_lexer(str(p))
        parts.append(Text(rel, style="bold"))
        parts.append(Syntax(p.read_text(encoding="utf-8"), lexer, theme="ansi_dark", line_numbers=True))
    parts.append(Text("fail_to_pass:", style="bold"))
    parts += [Text(f"  • {t}") for t in e.meta.fail_to_pass]
    return Panel(Group(*parts), title="hidden feature tests", border_style="yellow")


def gates(e: TaskEntry) -> Panel:
    report_path = e.path / "validation" / "report.json"
    t = Table(box=box.SIMPLE, header_style="bold")
    for col in ("gate", "result", "time", "details"):
        t.add_column(col, overflow="fold")
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for g in report.get("gates", []):
            t.add_row(g["gate"], Text("pass", style="green") if g["passed"] else Text("FAIL", style="red"),
                      f"{g.get('duration_s', 0):.1f}s", g.get("details", ""))
    logs = sorted((e.path / "validation" / "logs").glob("*.json"))
    footer = Text(f"\n{len(logs)} gate logs in {e.path / 'validation' / 'logs'}", style="dim")
    return Panel(Group(t, footer), title="validation evidence", border_style="blue")


def show(console: Console, e: TaskEntry, section: str = "all") -> None:
    views = {"summary": [header], "instruction": [instruction], "patch": [patch], "tests": [tests],
             "gates": [gates], "all": [header, instruction, patch, tests, gates]}
    for view in views[section]:
        console.print(view(e))
