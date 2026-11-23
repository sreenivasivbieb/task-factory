"""The Task Factory terminal's commands (/setup, /tasks, /run-task, /results) without the TUI."""
import io
import json
from pathlib import Path

import pytest

from factory import terminal_bridge, terminal_ops
from factory.cli import main
from factory.errors import ConfigError

GOOD = Path(__file__).parent / "fixtures/tasks/python/good_bulk_discount"


@pytest.fixture
def project(py_repo, tmp_path, monkeypatch):
    real = terminal_ops.project_config

    def local(root):
        cfg = real(root)
        cfg.sandbox.backend = "local"
        return cfg

    monkeypatch.setattr(terminal_ops, "project_config", local)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(py_repo)
    return py_repo


def _add_good_task(root) -> str:
    wd = terminal_ops.project_workdir(root).root
    assert main(["tasks", "add", str(GOOD), "--pending", "--workdir", str(wd), "--backend", "local"]) == 0
    (task,) = terminal_ops.tasks(root)
    return task["task_id"]


def test_workdir_is_stable_and_outside_the_repo(project, tmp_path):
    wd = terminal_ops.project_workdir(project).root
    assert wd == terminal_ops.project_workdir(project).root
    assert wd.is_relative_to(tmp_path / "state" / "task-factory" / "projects") and wd.name.startswith("toyshop-")
    assert terminal_ops.project_root(project / "toyshop") == project


def test_setup_is_idempotent_and_repins_when_head_moves(project, capsys):
    import subprocess

    first = terminal_ops.setup(project)
    assert first["ready"] and not first["head_moved"]
    terminal_ops.setup(project)
    assert "already set up" in capsys.readouterr().out
    (project / "NEW.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "more"], cwd=project, check=True)
    assert terminal_ops.status(project)["head_moved"]
    again = terminal_ops.setup(project)
    assert "HEAD moved" in capsys.readouterr().out and again["commit"] != first["commit"]


def test_tasks_review_from_the_terminal(project):
    terminal_ops.setup(project)
    task_id = _add_good_task(project)
    detail = terminal_ops.task_detail(project, task_id)
    assert [s["heading"] for s in detail["sections"]][:2] == ["Task", "Instruction (what the model sees)"]
    assert terminal_ops.approve(project, task_id)["status"] == "approved"
    with pytest.raises(ConfigError, match="reason"):
        terminal_ops.delete(project, task_id, "  ")
    assert terminal_ops.delete(project, task_id, "duplicate")["status"] == "deleted"
    assert terminal_ops.status(project)["counts"]["deleted"] == 1


def test_run_assigns_and_runs_only_the_chosen_tasks(project, monkeypatch):
    terminal_ops.setup(project)
    task_id = _add_good_task(project)
    store = terminal_ops._store(project)
    store.assign(task_id, "other-agent", "x/y")  # an unrelated open assignment must not run
    seen = {}

    def fake_work(args, cfg):
        seen["only"], seen["auth"] = args.only, args.use_host_auth
        return 0

    monkeypatch.setattr("factory.commands.cmd_harness_work", fake_work)
    monkeypatch.setenv("HOME", str(project.parent / "home"))  # no terminal logins
    terminal_ops.run(project, [task_id], "opencode/free-model")
    assert seen == {"only": {(task_id, "task-factory", "opencode/free-model")}, "auth": False}
    with pytest.raises(ConfigError, match="provider/model"):
        terminal_ops.run(project, [task_id], "no-provider")


def test_harness_work_only_filter(project, monkeypatch):
    from factory.commands import cmd_harness_work

    terminal_ops.setup(project)
    task_id = _add_good_task(project)
    terminal_ops._store(project).assign(task_id, "noop", None)
    import argparse

    cfg = terminal_ops.project_config(project)
    args = argparse.Namespace(store=None, k=None, timeout=None, use_host_auth=False, no_branch=True,
                              only={(task_id, "oracle", None)})
    out = io.StringIO()
    from contextlib import redirect_stdout

    with redirect_stdout(out):
        cmd_harness_work(args, cfg)
    assert "no open assignments" in out.getvalue()  # the noop assignment was filtered out


def test_host_auth_used_only_when_the_provider_is_logged_in(tmp_path, monkeypatch):
    auth = tmp_path / ".local/share/task-factory/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"nvidia": {"type": "api", "key": "k"}}))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert terminal_ops._host_auth_has("nvidia/qwen/x")
    assert not terminal_ops._host_auth_has("opencode/free")


AGENT_LOG = {
    "stdout": "\n".join(json.dumps(e) for e in [
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"text": "I will read pricing.py"}},
        {"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed",
                                                                "input": {"filePath": "/repo/toyshop/pricing.py"},
                                                                "output": "\n".join(f"line {i}" for i in range(30))}}},
        {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "error", "input": {"command": "pytest"},
                                                                "error": "exit 1"}}},
        {"type": "step_finish", "part": {"tokens": {"input": 100, "output": 7}}},
    ]),
    "stderr": "",
}


def test_transcript_is_readable():
    text = terminal_ops.transcript(AGENT_LOG)
    assert text.startswith("1 step(s), 100 input / 7 output tokens")
    assert "● I will read pricing.py" in text
    assert "✓ read: /repo/toyshop/pricing.py" in text and "    line 11" in text and "line 12\n" not in text
    assert "18 more lines" in text and "✗ bash: pytest" in text and "exit 1" in text


def _fake_run(root, run_id="20261123-120000-task-factory"):
    run = terminal_ops.project_workdir(root).harness / run_id
    adir = run / "toy-001" / "attempt-1"
    adir.mkdir(parents=True)
    result = {"task_id": "toy-001", "agent": "task-factory", "model": "opencode/m", "attempt": 1, "resolved": False,
              "category": "wrong_behaviour", "f2p_passed": 1, "f2p_total": 3, "p2p_failed": [],
              "tests_tampered": ["tests/conftest.py"], "agent_exit_code": 0, "agent_timed_out": False,
              "agent_duration_s": 12.5, "branch": None}
    (run / "results.jsonl").write_text(json.dumps(result) + "\n")
    (adir / "agent.log.json").write_text(json.dumps(AGENT_LOG))
    (adir / "solution.diff").write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    (adir / "grade-feature.json").write_text(json.dumps({"exec": {"stdout": "1 passed, 2 failed", "stderr": ""}}))
    return run_id


def test_results_pages(project):
    run_id = _fake_run(project)
    (r,) = terminal_ops.runs(project)
    assert r == {"run_id": run_id, "agent": "task-factory", "model": "opencode/m", "attempts": 1, "resolved": 0,
                 "finished": False, "stopped_early": None}
    (a,) = terminal_ops.attempts(project, run_id)
    assert a["category"] == "wrong_behaviour"
    page = terminal_ops.attempt_detail(project, run_id, "toy-001", 1)
    assert page["title"] == "toy-001 · attempt 1 · WRONG BEHAVIOUR"
    sections = {s["heading"]: s["text"] for s in page["sections"]}
    assert "1/3 new tests pass" in sections["Summary"] and "tests/conftest.py" in sections["Summary"]
    assert "● I will read pricing.py" in sections["Agent session"]
    assert "+b" in sections["Graded change (solution.diff)"]
    assert "2 failed" in sections["Hidden tests output"]
    with pytest.raises(ConfigError):
        terminal_ops.attempts(project, "../etc")


def test_bridge_streams_progress_and_returns_data(project, monkeypatch, capsys):
    _fake_run(project)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"op": "runs"}) + "\n"))
    terminal_bridge.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "result" and events[-1]["ok"] and events[-1]["data"][0]["attempts"] == 1

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"op": "setup"}) + "\n"))
    terminal_bridge.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(e["type"] == "progress" and "pinning" in e["text"] for e in events)
    assert events[-1]["ok"] and events[-1]["data"]["ready"]

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"op": "nope"}) + "\n"))
    terminal_bridge.main()
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert not last["ok"] and "unknown request" in last["text"]
