"""Terminal views for ``factory produce`` and ``factory harbor-check`` (uses ``rich``).

On a terminal: a header panel, a live spinner naming the current step, a colored line per
finished idea, and a results table. Piped or captured output gets the same text without
animation or wrapping, so logs stay greppable.
"""
from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

VERDICT_STYLE = {"valid": "bold green", "rejected": "bold yellow", "error": "bold red"}
VERDICT_MARK = {"valid": "✔", "rejected": "✘", "error": "!"}


def console() -> Console:
    c = Console(highlight=False)
    if not c.is_terminal:  # captured or piped: never wrap long lines
        c = Console(highlight=False, soft_wrap=True, width=200)
    return c


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class ProduceView:
    def __init__(self, c: Console | None = None) -> None:
        self.c = c or console()
        self.status = None
        self.idea_label = ""

    # ------------------------------------------------------------------ lifecycle

    def header(self, repo: str, commit: str, models: list[str], count: int, repairs: int, approve: bool) -> None:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim")
        grid.add_column()
        grid.add_row("repository", f"{repo} @ {commit}")
        grid.add_row("models", " → ".join(models) + ("  (later ones take over on failure)" if len(models) > 1 else ""))
        grid.add_row("goal", f"{count} valid task(s), up to {repairs} repair(s) per idea")
        grid.add_row("new tasks", "approved" if approve else "pending review (factory review)")
        self.c.print(Panel(grid, title="[bold]Task Factory · produce[/]", title_align="left",
                           border_style="cyan", expand=False))
        if self.c.is_terminal:
            self.status = self.c.status("starting", spinner="dots")
            self.status.start()

    def stop(self) -> None:
        if self.status is not None:
            self.status.stop()
            self.status = None

    # ------------------------------------------------------------------ events

    def idea(self, idea, n: int) -> None:
        self.idea_label = f"[{n}] {idea.title}"
        self._print(Text.assemble(("▶ ", "cyan"), (self.idea_label, "bold"), (f"  · {idea.category}", "dim"),
                                  (f"  · {', '.join(idea.files)}", "dim")))

    def step(self, message: str) -> None:
        if self.status is not None:
            label = f"{self.idea_label}: " if self.idea_label else ""
            self.status.update(f"[cyan]{label}[/]{message}")
        else:
            self.c.print(f"  … {message}", style="dim")

    def outcome(self, o) -> None:
        style = VERDICT_STYLE.get(o.verdict, "white")
        line = Text.assemble(("  " + VERDICT_MARK.get(o.verdict, "?") + " ", style), (f"{o.verdict:8}", style),
                             f" attempts={o.attempts}  {_duration(o.seconds)}  ",
                             (o.task_id or "", "green"))
        self._print(line)
        for r in o.reasons:
            self._print(Text(f"      - {r}", style="dim"))
        self.idea_label = ""

    def error(self, message: str) -> None:
        self._print(Text(f"error    {message}", style="bold red"))

    def _print(self, renderable) -> None:
        self.c.print(renderable)

    # ------------------------------------------------------------------ summary

    def summary(self, outcomes, record: dict, record_path) -> None:
        self.stop()
        if outcomes:
            t = Table(box=box.SIMPLE_HEAVY, header_style="bold", title="Results", title_justify="left")
            for col, kw in (("#", {"justify": "right"}), ("idea", {}), ("category", {"style": "dim"}),
                            ("verdict", {}), ("attempts", {"justify": "right"}), ("time", {"justify": "right"}),
                            ("task", {"overflow": "fold"})):
                t.add_column(col, **kw)
            for i, o in enumerate(outcomes, 1):
                t.add_row(str(i), o.idea.title, o.idea.category,
                          Text(o.verdict, style=VERDICT_STYLE.get(o.verdict, "white")),
                          str(o.attempts), _duration(o.seconds), o.task_id or "-")
            self.c.print(t)
        valid = record["valid"]
        style = "bold green" if valid and valid == record.get("goal", valid) else "bold yellow"
        self.c.print(Text.assemble((f"{valid}/{record['tried']} ideas became valid tasks", style),
                                   f" ({record['attempts']} drafts, {_duration(record['seconds'])}); "
                                   f"record: {record_path}"))


def harbor_row(c: Console, r: dict) -> None:
    ok = r["ok"]
    c.print(Text.assemble(("pass " if ok else "FAIL ", "bold green" if ok else "bold red"),
                          f"nop={r['nop']['reward']} oracle={r['oracle']['reward']}  ", (r["task_id"], "bold")))


def harbor_table(c: Console, rows: list[dict], version: str = "") -> None:
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold",
              title=f"Harbor validation matrix{f' (harbor {version})' if version else ''}", title_justify="left")
    t.add_column("task", overflow="fold")
    t.add_column("nop (expect 0.0)", justify="right")
    t.add_column("oracle (expect 1.0)", justify="right")
    t.add_column("time", justify="right")
    t.add_column("result")
    for r in rows:
        def cell(agent):
            a = r[agent]
            return Text(str(a["reward"]), style="green" if a["ok"] else "bold red")
        t.add_row(r["task_id"], cell("nop"), cell("oracle"),
                  _duration(r["nop"]["seconds"] + r["oracle"]["seconds"]),
                  Text("pass", style="bold green") if r["ok"] else Text("FAIL", style="bold red"))
    c.print(t)
