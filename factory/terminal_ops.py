"""What the Task Factory terminal can do, as plain functions over one project's workdir.

The terminal plugin calls these through ``factory.terminal_bridge``. Each project
(the git root the terminal runs in) gets one stable workdir *outside* the repo::

    ~/.local/state/task-factory/projects/<repo name>-<hash of its path>/

so ``/setup``, ``/generate-task``, ``/tasks``, ``/run-task`` and ``/results`` all see
the same tasks, runs and solution branches, and the user's repo stays untouched.
Functions print progress (forwarded live to the terminal) and return JSON-able data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from .config import FactoryConfig
from .errors import ConfigError
from .models import RepoSpec
from .state import Workdir

MAX_TOOL_OUTPUT_LINES = 12
MAX_SECTION_CHARS = 40_000


# --------------------------------------------------------------------------- project + workdir


def project_root(cwd: Path | None = None) -> Path:
    """The git top level of ``cwd`` (or ``cwd`` itself outside git)."""
    cwd = (cwd or Path.cwd()).resolve()
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, check=False)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else cwd


def project_workdir(root: Path) -> Workdir:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:10]
    return Workdir(state / "task-factory" / "projects" / f"{root.name}-{key}")


def project_config(root: Path) -> FactoryConfig:
    """The project's ``factory.toml`` if it has one, with the workdir moved out of the repo."""
    toml = root / "factory.toml"
    cfg = FactoryConfig.load(toml if toml.exists() else None)
    cfg.workdir = str(project_workdir(root).root)
    from .ecosystems import configure

    configure(cfg.ecosystems)
    return cfg


def _head(root: Path) -> str | None:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _source(root: Path) -> str:
    # A file:// URL clones the committed HEAD, so uncommitted edits never reach a task.
    return root.as_uri() if (root / ".git").exists() else str(root)


def status(root: Path) -> dict:
    wd = project_workdir(root)
    out: dict = {"project": str(root), "workdir": str(wd.root), "ready": False}
    if wd.repo_spec.exists():
        spec = RepoSpec.load(wd.repo_spec)
        head = _head(root)
        out.update(repo=spec.name, commit=spec.commit_sha, ready=_ready(wd),
                   head_moved=bool(head and head != spec.commit_sha))
    if wd.tasks.exists():
        from .review.store import TaskStore

        store = TaskStore(wd.tasks)
        out["counts"] = store.summary().get("counts", {})
        out["open_assignments"] = len(store.assignments())
    out["runs"] = len(_run_dirs(wd))
    return out


def _ready(wd: Workdir) -> bool:
    """Set up = an environment exists and the last inspect of the pinned commit succeeded."""
    if not (wd.repo_spec.exists() and wd.env_config.exists()):
        return False
    try:
        return json.loads(wd.inspect_result.read_text(encoding="utf-8")).get("status") == "ok"
    except (OSError, ValueError):
        return True  # older workdirs without inspect.json


def setup(root: Path, force: bool = False) -> dict:
    """Pin the project's committed HEAD and inspect it (Docker image + baseline). Idempotent.

    The new commit is pinned and inspected in a staging folder first; only a working setup
    replaces the current one, so a failed rebuild (e.g. no network) leaves the old setup usable.
    """
    import shutil

    from .commands import cmd_inspect
    from .repo.loader import load_repo

    wd = project_workdir(root)
    cfg = project_config(root)
    head = _head(root)
    if _ready(wd) and not force:
        spec = RepoSpec.load(wd.repo_spec)
        if head is None or spec.commit_sha == head:
            print(f"{spec.name}@{spec.short_sha} is already set up")
            return status(root)
        print(f"HEAD moved from {spec.short_sha} to {head[:7]}: pinning the new commit")
    print(f"pinning {root.name} and building its test environment (first time can take a few minutes)")
    stage = Workdir(wd.root / ".setup-staging")
    if stage.root.exists():
        shutil.rmtree(stage.root)
    spec = load_repo(_source(root), stage.root)
    spec.save(stage.repo_spec)
    stage_cfg = cfg.model_copy(update={"workdir": str(stage.root)})
    if cmd_inspect(argparse.Namespace(force=True, dry_run=False), stage_cfg):
        keep = " The previous setup is unchanged." if _ready(wd) else ""
        raise ConfigError(f"could not build a working test environment; details in {stage.inspect_result}.{keep}")
    dest = wd.root / "repos" / spec.name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stage.root / "repos" / spec.name), dest)
    spec.local_path = str(dest.resolve())
    spec.save(wd.repo_spec)
    for name in ("env_config", "inspect_result"):
        shutil.move(str(getattr(stage, name)), getattr(wd, name))
    shutil.rmtree(stage.root, ignore_errors=True)
    print(f"ready: {spec.name}@{spec.short_sha}")
    return status(root)


def ensure_setup(root: Path) -> FactoryConfig:
    if not _ready(project_workdir(root)):
        setup(root)
    return project_config(root)


# --------------------------------------------------------------------------- tasks


def _title(pkg: Path) -> str:
    try:
        for line in (pkg / "instruction.md").read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return pkg.name


def _store(root: Path):
    from .review.store import TaskStore

    return TaskStore(project_workdir(root).tasks)


def tasks(root: Path, status_filter: str = "all") -> list[dict]:
    store = _store(root)
    if not store.root.exists():
        return []
    last: dict[str, str] = {}
    for a in store.assignments(open_only=False):
        if a.result is not None and a.result.reason:
            last[a.task_id] = f"{a.label}: {a.result.reason}"
    return [{"task_id": e.task_id, "title": _title(e.path), "status": e.status, "author": e.author,
             "session_id": e.meta.generated_by.session_id,
             "kind": str(e.meta.kind.value if hasattr(e.meta.kind, "value") else e.meta.kind),
             "tests": len(e.meta.fail_to_pass), "last_result": last.get(e.task_id)}
            for e in store.entries(status_filter)]


def task_detail(root: Path, task_id: str) -> dict:
    e = _store(root).get(task_id)
    history = "\n".join(f"- {ev.action}" + (f" ({ev.reason})" if ev.reason else "") + f" by {ev.reviewer}"
                        for ev in e.events)
    meta = (f"status: {e.status}\nauthor: {e.author}\nbase commit: {e.meta.commit_sha}\n"
            f"hidden tests: {len(e.meta.fail_to_pass)} fail_to_pass, {len(e.meta.pass_to_pass)} pass_to_pass\n"
            f"package: {e.path}")
    return {"title": f"{e.task_id}", "sections": [
        {"heading": "Task", "text": meta},
        {"heading": "Instruction (what the model sees)", "text": (e.path / "instruction.md").read_text(encoding="utf-8")},
        {"heading": "Review history", "text": history or "(none)"},
    ]}


def approve(root: Path, task_id: str) -> dict:
    e = _store(root).approve(task_id)
    return {"task_id": e.task_id, "status": e.status}


def delete(root: Path, task_id: str, reason: str) -> dict:
    if not reason.strip():
        raise ConfigError("a reason is needed to delete a task")
    e = _store(root).delete(task_id, reason.strip())
    return {"task_id": e.task_id, "status": e.status}


# --------------------------------------------------------------------------- run


def _host_auth_has(model: str, cfg: FactoryConfig) -> bool:
    """True when the agent's stored logins (``/connect`` in the terminal) include the model's provider."""
    from .harness.agents import get_agent

    agent = get_agent(cfg.harness.default_agent, cfg.harness.agents)
    path = Path(os.path.expanduser(agent.host_auth_file or ""))
    try:
        return model.split("/", 1)[0] in json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False


def run(root: Path, task_ids: list[str], model: str) -> dict:
    """Assign the tasks to ``model`` and solve them now in Docker; returns the new run's summary."""
    from .commands import cmd_harness_work

    if not task_ids:
        raise ConfigError("choose at least one task")
    if "/" not in model:
        raise ConfigError("the model must look like provider/model")
    cfg = project_config(root)
    agent = cfg.harness.default_agent
    wd = Workdir(cfg.workdir)
    store = _store(root)
    for task_id in task_ids:
        store.assign(task_id, agent, model, note="terminal /run-task")
    before = {p.name for p in _run_dirs(wd)}
    use_auth = _host_auth_has(model, cfg)
    print(f"solving {len(task_ids)} task(s) with {model}"
          + (" using your terminal login" if use_auth else ""))
    args = argparse.Namespace(store=None, k=None, timeout=None, use_host_auth=use_auth, no_branch=False,
                              only={(t, agent, model) for t in task_ids})
    cmd_harness_work(args, cfg)
    new = [p for p in _run_dirs(wd) if p.name not in before]
    return {"run_id": new[0].name if new else None}


# --------------------------------------------------------------------------- results


def _run_dirs(wd: Workdir) -> list[Path]:
    if not wd.harness.exists():
        return []
    return sorted((p for p in wd.harness.iterdir() if p.is_dir() and (p / "results.jsonl").exists()),
                  key=lambda p: p.name, reverse=True)


def _results(run_dir: Path) -> list[dict]:
    out = []
    for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def runs(root: Path) -> list[dict]:
    out = []
    for p in _run_dirs(project_workdir(root)):
        rs = _results(p)
        summary = {}
        if (p / "summary.json").exists():
            summary = json.loads((p / "summary.json").read_text(encoding="utf-8"))
        first = rs[0] if rs else {}
        out.append({"run_id": p.name, "agent": summary.get("agent", first.get("agent")),
                    "model": summary.get("model", first.get("model")), "attempts": len(rs),
                    "resolved": sum(bool(r.get("resolved")) for r in rs),
                    "finished": bool(summary), "stopped_early": summary.get("stopped_early")})
    return out


def attempts(root: Path, run_id: str) -> list[dict]:
    p = _run_dir(root, run_id)
    return [{"task_id": r["task_id"], "attempt": r["attempt"], "category": r["category"],
             "resolved": r["resolved"], "agent_duration_s": r.get("agent_duration_s"), "branch": r.get("branch")}
            for r in _results(p)]


def _run_dir(root: Path, run_id: str) -> Path:
    if not run_id or "/" in run_id or run_id.startswith("."):
        raise ConfigError(f"bad run id {run_id!r}")
    p = project_workdir(root).harness / run_id
    if not (p / "results.jsonl").exists():
        raise ConfigError(f"no run {run_id}")
    return p


def _clip(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def _tool_input(tool: str, data: dict) -> str:
    for key in ("command", "filePath", "path", "pattern", "url", "query"):
        if isinstance(data.get(key), str):
            return data[key]
    return json.dumps(data, ensure_ascii=False)[:200]


def transcript(agent_log: dict) -> str:
    """The agent's session as readable text: what it said and every tool it ran, with output."""
    lines: list[str] = []
    steps = tokens_in = tokens_out = 0
    for raw in (agent_log.get("stdout") or "").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            if raw.strip():
                lines.append(raw)
            continue
        part = ev.get("part") or {}
        kind = ev.get("type")
        if kind == "text" and part.get("text", "").strip():
            lines += ["", "● " + part["text"].strip(), ""]
        elif kind == "tool_use":
            state = part.get("state") or {}
            tool = part.get("tool", "tool")
            mark = "✓" if state.get("status") == "completed" else "✗"
            lines.append(f"{mark} {tool}: {_tool_input(tool, state.get('input') or {})}")
            output = str(state.get("output") or state.get("error") or "").rstrip()
            out_lines = output.splitlines()
            for line in out_lines[:MAX_TOOL_OUTPUT_LINES]:
                lines.append("    " + line)
            if len(out_lines) > MAX_TOOL_OUTPUT_LINES:
                lines.append(f"    … {len(out_lines) - MAX_TOOL_OUTPUT_LINES} more lines")
        elif kind == "step_finish":
            steps += 1
            t = part.get("tokens") or {}
            tokens_in += int(t.get("input") or 0)
            tokens_out += int(t.get("output") or 0)
        elif kind == "error":
            lines.append("ERROR " + json.dumps(ev.get("error") or part, ensure_ascii=False)[:500])
    head = f"{steps} step(s), {tokens_in} input / {tokens_out} output tokens"
    stderr = (agent_log.get("stderr") or "").strip()
    if stderr:
        lines += ["", "stderr:", *stderr.splitlines()[-20:]]
    return _clip(head + "\n" + "\n".join(lines).strip())


def _test_output(grade_file: Path) -> str:
    if not grade_file.exists():
        return "(not run)"
    data = json.loads(grade_file.read_text(encoding="utf-8"))
    exe = data.get("exec", data)
    text = ((exe.get("stdout") or "") + "\n" + (exe.get("stderr") or "")).strip()
    return _clip(text or "(no output)", 12_000)


def attempt_detail(root: Path, run_id: str, task_id: str, attempt: int) -> dict:
    p = _run_dir(root, run_id)
    r = next((x for x in _results(p) if x["task_id"] == task_id and int(x["attempt"]) == int(attempt)), None)
    if r is None:
        raise ConfigError(f"no attempt {attempt} of {task_id} in run {run_id}")
    adir = p / task_id / f"attempt-{attempt}"
    verdict = "RESOLVED" if r["resolved"] else r["category"].upper().replace("_", " ")
    summary = [
        f"verdict: {verdict}",
        f"model: {r.get('agent')} · {r.get('model')}",
        f"hidden tests: {r['f2p_passed']}/{r['f2p_total']} new tests pass; "
        + (f"{len(r['p2p_failed'])} existing test(s) broke" if r["p2p_failed"] else "no existing test broke"),
        f"agent: {r['agent_duration_s']}s, exit {r['agent_exit_code']}" + (" (timed out)" if r["agent_timed_out"] else ""),
    ]
    if r.get("tests_tampered"):
        summary.append("test/config files the agent changed (undone before grading): " + ", ".join(r["tests_tampered"]))
    if r["p2p_failed"]:
        summary.append("broken existing tests: " + ", ".join(r["p2p_failed"][:10]))
    if r.get("branch"):
        summary.append(f"branch: {r['branch']} @ {str(r.get('commit'))[:12]}")
        try:
            repo_name = _store(root).get(task_id).meta.repo_name
            summary.append(f"  git -C {project_workdir(root).root / 'solutions' / repo_name} show {r['branch']}")
        except Exception:  # noqa: BLE001 - the command line is a convenience, the branch name is enough
            pass
    sections = [{"heading": "Summary", "text": "\n".join(summary)}]
    log_file = adir / "agent.log.json"
    if log_file.exists():
        sections.append({"heading": "Agent session", "text": transcript(json.loads(log_file.read_text(encoding="utf-8")))})
    diff = (adir / "solution.diff")
    sections.append({"heading": "Graded change (solution.diff)",
                     "text": _clip(diff.read_text(encoding="utf-8")) if diff.exists() and diff.stat().st_size else "(no change)"})
    sections.append({"heading": "Hidden tests output", "text": _test_output(adir / "grade-feature.json")})
    if r["p2p_failed"]:
        sections.append({"heading": "Full test suite output", "text": _test_output(adir / "grade-full.json")})
    return {"title": f"{task_id} · attempt {attempt} · {verdict}", "sections": sections}


# --------------------------------------------------------------------------- generations (per session)


def _generations_file(wd: Workdir) -> Path:
    return wd.root / "generations.jsonl"


def log_generation(wd: Workdir, entry: dict) -> None:
    """One line per /generate-task attempt, valid or not, with the session it came from."""
    import time

    wd.root.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": round(time.time(), 3), **entry}, ensure_ascii=False)
    with _generations_file(wd).open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    from .records import generation_doc, records

    if records.enabled:
        records.put("generations", generation_doc(wd.root, line))


def generations(root: Path, session_id: str | None = None) -> list[dict]:
    path = _generations_file(project_workdir(root))
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if session_id is None or entry.get("session_id") == session_id:
            out.append(entry)
    return out


def last_rejection(wd: Workdir) -> dict:
    """The reject code and reason of the newest validation report (the draft just checked)."""
    reports = sorted(wd.runs.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if wd.runs.exists() else []
    for report in reports[:1]:
        data = json.loads(report.read_text(encoding="utf-8"))
        return {"reject_code": data.get("reject_code"), "reason": data.get("reject_reason"),
                "report": str(report)}
    return {}


# --------------------------------------------------------------------------- performance


def _tokens(adir: Path) -> int | None:
    log = adir / "agent.log.json"
    try:
        stdout = json.loads(log.read_text(encoding="utf-8")).get("stdout") or ""
    except (OSError, ValueError):
        return None
    total, seen = 0, False
    for raw in stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") == "step_finish":
            t = (ev.get("part") or {}).get("tokens") or {}
            total += int(t.get("input") or 0) + int(t.get("output") or 0)
            seen = True
    return total if seen else None


SANITY_AGENTS = {"oracle", "noop"}  # check the harness itself; never counted as AI performance


def _run_started(run_id: str) -> float | None:
    import time

    try:
        return time.mktime(time.strptime(run_id[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


def _attempt_rows(root: Path) -> list[dict]:
    """Every graded AI attempt, oldest run first, with its run id and token count.

    Left out: the sanity agents, and attempts on a task made before its last manual edit
    (they graded a different version of the task).
    """
    from .harness.runner import INFRA_CATEGORIES

    edited: dict[str, float] = {}
    store = _store(root)
    if store.root.exists():
        for e in store.entries():
            times = [ev.ts for ev in e.events if ev.action == "edit"]
            if times:
                edited[e.task_id] = max(times)
    rows = []
    for run_dir in reversed(_run_dirs(project_workdir(root))):
        started = _run_started(run_dir.name)
        for r in _results(run_dir):
            if r.get("agent") in SANITY_AGENTS:
                continue
            if started is not None and started < edited.get(r["task_id"], 0):
                continue
            rows.append({**r, "run_id": run_dir.name, "infra": r.get("category") in INFRA_CATEGORIES,
                         "tokens_total": _tokens(run_dir / r["task_id"] / f"attempt-{r['attempt']}")})
    return rows


def _difficulty(rows: list[dict]) -> str | None:
    """The design doc's tag: solved by all attempts, some, or none (agent errors do not count)."""
    graded = [r for r in rows if not r["infra"]]
    if not graded:
        return None
    solved = sum(bool(r["resolved"]) for r in graded)
    return "all" if solved == len(graded) else ("some" if solved else "none")


def _model_stats(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r.get("model") or r.get("agent") or "?", []).append(r)
    out = []
    for model, rs in by.items():
        graded = [r for r in rs if not r["infra"]]
        times = [r["agent_duration_s"] for r in graded if r.get("agent_duration_s") is not None]
        toks = [r["tokens_total"] for r in graded if r.get("tokens_total") is not None]
        out.append({"model": model, "attempts": len(graded), "resolved": sum(bool(r["resolved"]) for r in graded),
                    "agent_errors": len(rs) - len(graded), "tasks": len({r["task_id"] for r in rs}),
                    "mean_time_s": round(sum(times) / len(times), 1) if times else None,
                    "mean_tokens": round(sum(toks) / len(toks)) if toks else None})
    return sorted(out, key=lambda m: m["model"])


def overview(root: Path, session_id: str | None = None) -> dict:
    """Everything the sidebar shows: project, tasks left, AI performance, per model, this session, next step."""
    st = status(root)
    entries = tasks(root)
    live = [t for t in entries if t["status"] != "deleted"]
    rows = _attempt_rows(root)
    by_task: dict[str, list[dict]] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)
    graded = [r for r in rows if not r["infra"]]
    difficulty = {"all": 0, "some": 0, "none": 0}
    never_solved = []
    for t in live:
        d = _difficulty(by_task.get(t["task_id"], []))
        if d:
            difficulty[d] += 1
        if d == "none":
            never_solved.append(t["task_id"])
    failures: dict[str, int] = {}
    for r in rows:
        if not r["resolved"]:
            failures[r["category"]] = failures.get(r["category"], 0) + 1
    not_run = [t["task_id"] for t in live if not any(not r["infra"] for r in by_task.get(t["task_id"], []))]
    counts = st.get("counts", {"pending": 0, "approved": 0, "deleted": 0})
    session = generations(root, session_id) if session_id else []
    titles = {t["task_id"]: t["title"] for t in entries}
    if not st.get("ready"):
        nxt = "/setup to prepare this project"
    elif st.get("head_moved"):
        nxt = "HEAD moved: /setup to pin the new commit"
    elif not live:
        nxt = "/generate-task to make the first task"
    elif counts.get("pending"):
        nxt = f"{counts['pending']} task(s) wait for review: /tasks"
    elif not_run:
        nxt = f"{len(not_run)} task(s) never run: /run-task"
    elif never_solved:
        nxt = f"{len(never_solved)} task(s) no model solved: check they are solvable (/benchmark)"
    else:
        nxt = "/benchmark to compare models, or /generate-task for more tasks"
    return {
        "project": {k: st.get(k) for k in ("repo", "commit", "ready", "head_moved")},
        "tasks": {**counts, "total": len(entries), "not_run": len(not_run), "never_solved": len(never_solved),
                  "open_assignments": st.get("open_assignments", 0)},
        "performance": {"attempts": len(graded), "resolved": sum(bool(r["resolved"]) for r in graded),
                        "agent_errors": len(rows) - len(graded), "difficulty": difficulty, "failures": failures},
        "models": _model_stats(rows),
        "session": {"made": len(session), "valid": sum(g.get("verdict") == "valid" for g in session),
                    "items": [{**{k: g.get(k) for k in ("request", "verdict", "task_id", "reject_code", "reason",
                                                         "model")},
                               "title": titles.get(g.get("task_id"))} for g in session[-8:]]},
        "next": nxt,
    }


def benchmark(root: Path) -> dict:
    """Tasks x models: per cell the attempts and how many resolved; the doc's difficulty per task; totals."""
    entries = {t["task_id"]: t for t in tasks(root)}
    rows = _attempt_rows(root)
    models = sorted({r.get("model") or r.get("agent") or "?" for r in rows})
    task_ids = [t for t in entries if entries[t]["status"] != "deleted"]
    task_ids += sorted({r["task_id"] for r in rows} - set(task_ids))
    matrix = []
    for task_id in task_ids:
        mine = [r for r in rows if r["task_id"] == task_id]
        cells = {}
        for model in models:
            rs = [r for r in mine if (r.get("model") or r.get("agent") or "?") == model]
            if not rs:
                continue
            graded = [r for r in rs if not r["infra"]]
            last = rs[-1]
            cells[model] = {"attempts": len(graded), "resolved": sum(bool(r["resolved"]) for r in graded),
                            "agent_errors": len(rs) - len(graded), "category": last["category"],
                            "run_id": last["run_id"], "attempt": last["attempt"]}
        t = entries.get(task_id, {})
        matrix.append({"task_id": task_id, "title": t.get("title", task_id), "status": t.get("status", "?"),
                       "difficulty": _difficulty(mine), "cells": cells})
    failures: dict[str, int] = {}
    for r in rows:
        if not r["resolved"]:
            failures[r["category"]] = failures.get(r["category"], 0) + 1
    return {"models": _model_stats(rows), "matrix": matrix, "failures": failures}
