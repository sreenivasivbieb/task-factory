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

AGENT = "task-factory"
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
        out.update(repo=spec.name, commit=spec.commit_sha, ready=wd.env_config.exists(),
                   head_moved=bool(head and head != spec.commit_sha))
    if wd.tasks.exists():
        from .review.store import TaskStore

        store = TaskStore(wd.tasks)
        out["counts"] = store.summary().get("counts", {})
        out["open_assignments"] = len(store.assignments())
    out["runs"] = len(_run_dirs(wd))
    return out


def setup(root: Path, force: bool = False) -> dict:
    """Pin the project's committed HEAD and inspect it (Docker image + baseline). Idempotent."""
    from .commands import cmd_inspect
    from .repo.loader import load_repo

    wd = project_workdir(root)
    cfg = project_config(root)
    head = _head(root)
    if wd.repo_spec.exists() and wd.env_config.exists() and not force:
        spec = RepoSpec.load(wd.repo_spec)
        if head is None or spec.commit_sha == head:
            print(f"{spec.name}@{spec.short_sha} is already set up")
            return status(root)
        print(f"HEAD moved from {spec.short_sha} to {head[:7]}: pinning the new commit")
    print(f"pinning {root.name} and building its test environment (first time can take a few minutes)")
    spec = load_repo(_source(root), wd.root)
    spec.save(wd.repo_spec)
    if cmd_inspect(argparse.Namespace(force=True, dry_run=False), cfg):
        raise ConfigError(f"could not build a working test environment; details in {wd.inspect_result}")
    print(f"ready: {spec.name}@{spec.short_sha}")
    return status(root)


def ensure_setup(root: Path) -> FactoryConfig:
    wd = project_workdir(root)
    if not (wd.repo_spec.exists() and wd.env_config.exists()):
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


def _host_auth_has(model: str) -> bool:
    """True when the terminal's stored logins (``/connect``) include the model's provider."""
    from .harness.agents import get_agent

    agent = get_agent(AGENT)
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
    wd = Workdir(cfg.workdir)
    store = _store(root)
    for task_id in task_ids:
        store.assign(task_id, AGENT, model, note="terminal /run-task")
    before = {p.name for p in _run_dirs(wd)}
    use_auth = _host_auth_has(model)
    print(f"solving {len(task_ids)} task(s) with {model}"
          + (" using your terminal login" if use_auth else ""))
    args = argparse.Namespace(store=None, k=None, timeout=None, use_host_auth=use_auth, no_branch=False,
                              only={(t, AGENT, model) for t in task_ids})
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
