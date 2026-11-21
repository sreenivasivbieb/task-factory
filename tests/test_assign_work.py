import json
import subprocess
from pathlib import Path

import pytest

from factory.cli import main
from factory.config import FactoryConfig, GatesConfig
from factory.errors import ConfigError
from factory.harness.agents import get_agent
from factory.harness.runner import host_auth_payload
from factory.harness.solutions import branch_name
from factory.packaging.writer import write_package
from factory.review.store import TaskStore
from factory.validation.drafts import diff_from_overlay, load_draft_dir
from factory.validation.gates import Validator

TASKS = Path(__file__).parent / "fixtures" / "tasks" / "python"
CFG = FactoryConfig(gates=GatesConfig(stability_repeats=1))


@pytest.fixture
def workdir(local_factory, py_spec, py_env_local, tmp_path):
    draft = load_draft_dir(TASKS / "good_bulk_discount", py_spec)
    report = Validator(local_factory, py_spec, py_env_local, CFG.gates).validate(draft)
    wd = tmp_path / "wd"
    write_package(draft, report, py_spec, py_env_local, wd / "tasks")
    return wd


def _config(tmp_path, py_spec, overlay: dict[str, str]) -> Path:
    patch = tmp_path / "agent.diff"
    patch.write_text(diff_from_overlay(py_spec, overlay))
    cfg = tmp_path / "factory.toml"
    cfg.write_text(f'[sandbox]\nbackend = "local"\n\n[harness.agents.patcher]\nrun = "git apply {patch}"\n')
    return cfg


def test_branch_name():
    assert branch_name("opencode", "nvidia/qwen/qwen3-coder-480b", "toy-1") == "ai/opencode-qwen3-coder-480b/toy-1"
    assert branch_name("oracle", None, "toy-1") == "ai/oracle/toy-1"


def test_assignment_lifecycle(workdir):
    store = TaskStore(workdir / "tasks", reviewer="tester")
    (entry,) = store.entries()
    a = store.assign(entry.task_id, "opencode", "nvidia/x")
    assert store.assign(entry.task_id, "opencode", "nvidia/x").ts == a.ts  # no duplicate
    store.assign(entry.task_id, "opencode", "tokenrouter/y")
    assert [x.label for x in store.assignments()] == ["opencode:nvidia/x", "opencode:tokenrouter/y"]
    store.record_result(entry.task_id, "opencode", "nvidia/x", "resolved 1/1", branch="ai/b")
    assert [x.label for x in store.assignments()] == ["opencode:tokenrouter/y"]
    assert store.unassign(entry.task_id) == 1 and store.assignments() == []
    done = store.assignments(open_only=False)
    assert done[0].result.branch == "ai/b" and done[1].result.action == "unassign"
    assert store.get(entry.task_id).status == "pending"  # assignments never change review status
    store.delete(entry.task_id, "dup")
    with pytest.raises(ConfigError, match="deleted"):
        store.assign(entry.task_id, "opencode", "nvidia/x")


def test_host_auth_only_copies_that_provider(tmp_path, monkeypatch):
    home = tmp_path / "home"
    auth = home / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"nvidia": {"type": "api", "key": "n-1"}, "tokenrouter": {"type": "api", "key": "t-2"}}))
    monkeypatch.setenv("HOME", str(home))
    agent = get_agent("opencode")
    assert json.loads(host_auth_payload(agent, "nvidia/qwen/qwen3-coder")) == {"nvidia": {"type": "api", "key": "n-1"}}
    with pytest.raises(ConfigError, match="stored: nvidia, tokenrouter"):
        host_auth_payload(agent, "amazon-bedrock/qwen.qwen3-coder-480b-a35b-v1:0")
    with pytest.raises(ConfigError, match="--model"):
        host_auth_payload(agent, None)


def test_work_resolved_creates_branch(workdir, py_spec, tmp_path, capsys):
    good = (TASKS / "good_bulk_discount" / "after" / "toyshop" / "pricing.py").read_text()
    cfg = _config(tmp_path, py_spec, {"toyshop/pricing.py": good})
    common = ["--workdir", str(workdir), "--config", str(cfg)]
    store = TaskStore(workdir / "tasks")
    (entry,) = store.entries()
    assert main(["tasks", "assign", entry.task_id, "--agent", "patcher", "--model", "demo/model", *common]) == 0
    assert main(["harness", "work", *common]) == 0
    out = capsys.readouterr().out
    assert "new branch" in out and "resolved" in out
    (done,) = store.assignments(open_only=False)
    assert done.result.reason == "resolved 1/1"
    branch = done.result.branch
    assert branch == f"ai/patcher-model/{entry.task_id}"
    repo = workdir / "solutions" / "toyshop"
    log = subprocess.run(["git", "log", "-1", "--format=%B", branch], cwd=repo, capture_output=True, text=True).stdout
    assert "solved by patcher (demo/model)" in log and "3/3 fail_to_pass passed" in log
    diff = subprocess.run(["git", "diff", entry.meta.commit_sha, branch, "--stat"], cwd=repo,
                          capture_output=True, text=True).stdout
    assert "toyshop/pricing.py" in diff and "test_bulk_discount" not in diff  # hidden tests are not committed
    assert store.assignments() == []
    assert main(["harness", "work", *common]) == 0  # nothing left to do


def test_work_failed_records_and_flags(workdir, py_spec, tmp_path):
    wrong = (TASKS / "good_bulk_discount" / "mutant" / "toyshop" / "pricing.py").read_text()
    cfg = _config(tmp_path, py_spec, {"toyshop/pricing.py": wrong})
    common = ["--workdir", str(workdir), "--config", str(cfg)]
    store = TaskStore(workdir / "tasks")
    (entry,) = store.entries()
    assert main(["tasks", "assign", entry.task_id, "--agent", "patcher", *common]) == 0
    assert main(["harness", "work", *common]) == 0
    (done,) = store.assignments(open_only=False)
    assert done.result.reason == "resolved 0/1 (wrong_behaviour)" and done.result.branch is None
    assert any(e.action == "note" and "unsolved by patcher" in e.reason for e in store.events())
    assert not (workdir / "solutions").exists()


def test_assign_unknown_agent_fails_early(workdir):
    store = TaskStore(workdir / "tasks")
    (entry,) = store.entries()
    assert main(["tasks", "assign", entry.task_id, "--agent", "nope", "--workdir", str(workdir)]) == 2
