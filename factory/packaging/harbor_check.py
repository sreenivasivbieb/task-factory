"""The hackathon's validation matrix, run with the real Harbor CLI on exported tasks.

For every task folder: ``harbor run -p <task> -a nop -k 1 -o <out> --no-delete -y`` must give
reward 0.0 and ``-a oracle`` must give 1.0. The rewards, timings and log folders are written
to ``<out>/summary.json`` and ``<out>/summary.md`` as validation evidence.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from ..reference.harbor import harbor_command, read_reward

EXPECTED = {"nop": 0.0, "oracle": 1.0}


def task_dirs(root: Path) -> list[Path]:
    root = Path(root)
    if (root / "task.toml").exists():
        return [root]
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "task.toml").exists())


def check_task(task: Path, out: Path, harbor: list[str], timeout_s: int = 3600) -> dict:
    task_id = task.name
    row: dict = {"task_id": task_id}
    for agent, expected in EXPECTED.items():
        dest = out / agent / task_id
        cmd = [*harbor, "run", "-p", str(task), "-a", agent, "-k", "1", "-o", str(dest), "--no-delete", "-y"]
        start = time.monotonic()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            exit_code = r.returncode
        except subprocess.TimeoutExpired:
            exit_code = None
        reward = read_reward(dest)
        row[agent] = {"reward": reward, "expected": expected, "ok": reward == expected, "exit_code": exit_code,
                      "seconds": round(time.monotonic() - start, 1), "logs": str(dest),
                      "command": " ".join(cmd)}
    row["ok"] = all(row[a]["ok"] for a in EXPECTED)
    return row


def run_matrix(root: Path, out: Path, on_row=None) -> list[dict]:
    harbor = harbor_command()
    version = subprocess.run([*harbor, "--version"], capture_output=True, text=True).stdout.strip()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for task in task_dirs(root):
        row = check_task(task, out, harbor)
        rows.append(row)
        if on_row:
            on_row(row)
    summary = {"harbor_version": version, "tasks": len(rows), "passed": sum(r["ok"] for r in rows), "rows": rows}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Harbor validation matrix (harbor {version})", "",
             "| Task | nop reward (expect 0.0) | oracle reward (expect 1.0) | Result |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['task_id']}` | {r['nop']['reward']} | {r['oracle']['reward']} | "
                     f"{'pass' if r['ok'] else 'FAIL'} |")
    lines += ["", f"{summary['passed']}/{summary['tasks']} tasks pass both checks."]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows
