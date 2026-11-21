import json
import subprocess
from pathlib import Path

import pytest

from factory.config import FactoryConfig, GatesConfig
from factory.errors import ConfigError
from factory.harness.agents import get_agent
from factory.harness.protect import is_test_path
from factory.harness.runner import Harness
from factory.packaging.harbor import to_harbor
from factory.packaging.writer import write_package
from factory.validation.drafts import diff_from_overlay, load_draft_dir
from factory.validation.gates import Validator

TASKS = Path(__file__).parent / "fixtures" / "tasks" / "python"
CFG = FactoryConfig(gates=GatesConfig(stability_repeats=1))


@pytest.fixture(scope="module")
def package(local_factory, py_spec, py_env_local, tmp_path_factory):
    draft = load_draft_dir(TASKS / "good_bulk_discount", py_spec)
    report = Validator(local_factory, py_spec, py_env_local, CFG.gates).validate(draft)
    return write_package(draft, report, py_spec, py_env_local, tmp_path_factory.mktemp("tasks"))


@pytest.fixture(scope="module")
def docker_package(docker_factory, py_spec, py_env_docker, tmp_path_factory):
    draft = load_draft_dir(TASKS / "good_bulk_discount", py_spec)
    report = Validator(docker_factory, py_spec, py_env_docker, CFG.gates).validate(draft)
    return write_package(draft, report, py_spec, py_env_docker, tmp_path_factory.mktemp("dtasks"))


def _run(factory, pkg, tmp_path, agent, **kw):
    h = Harness(factory, CFG, tmp_path / "harness")
    try:
        return h.run([pkg], agent, **kw), h
    finally:
        h.close()


def _patch_file(py_spec, tmp_path, overlay: dict[str, str]) -> Path:
    p = tmp_path / "agent.diff"
    p.write_text(diff_from_overlay(py_spec, overlay))
    return p


def test_is_test_path():
    for p in ("tests/test_x.py", "pkg/test_util.py", "a/b_test.go", "src/x.test.ts", "conftest.py",
              "src/test/java/A.java", "test/slug.test.js"):
        assert is_test_path(p), p
    for p in ("toyshop/pricing.py", "lib/stats.js", "contest.py"):
        assert not is_test_path(p), p


def test_unknown_agent_and_command_needs_cmd():
    with pytest.raises(ConfigError, match="unknown agent"):
        get_agent("nope")
    with pytest.raises(ConfigError, match="--cmd"):
        get_agent("command")
    custom = get_agent("mine", {"mine": {"run": "tool {instruction_file} {model_args}", "model_flag": "-m {model}"}})
    assert custom.command(".factory/instruction.md", "x") == "tool .factory/instruction.md -m x"


def test_secret_required(local_factory, package, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = get_agent("claude-code")
    with pytest.raises(ConfigError):
        _run(local_factory, package, tmp_path, agent)


def test_oracle_resolves_noop_does_not(local_factory, package, tmp_path):
    summary, _ = _run(local_factory, package, tmp_path, get_agent("oracle"))
    assert summary.resolved_at_k == 1.0 and summary.per_task[0].difficulty == "easy"
    summary, _ = _run(local_factory, package, tmp_path, get_agent("noop"), k=2)
    assert summary.resolved_at_k == 0.0 and summary.categories == {"no_change": 2}
    assert summary.per_task[0].difficulty == "unsolved"


def test_command_agent_wrong_behaviour(local_factory, package, py_spec, tmp_path):
    src = (TASKS / "good_bulk_discount" / "mutant" / "toyshop" / "pricing.py").read_text()
    patch = _patch_file(py_spec, tmp_path, {"toyshop/pricing.py": src})
    agent = get_agent("command", cmd=f"git apply {patch} && test -s {{instruction_file}}")
    summary, _ = _run(local_factory, package, tmp_path, agent)
    (result_file,) = list((tmp_path / "harness").glob("*/*/attempt-1/grade.json"))
    result = json.loads(result_file.read_text())
    assert result["category"] == "wrong_behaviour" and result["f2p_passed"] == 1 and not result["resolved"]


def test_agent_cannot_weaken_existing_tests(local_factory, package, py_spec, tmp_path):
    """An agent that breaks money formatting and edits the money test to hide it is still caught."""
    good = (TASKS / "good_bulk_discount" / "after" / "toyshop" / "pricing.py").read_text()
    money = (TASKS / "bad_regression" / "after" / "toyshop" / "money.py").read_text()
    tests = (Path(py_spec.local_path) / "tests" / "test_money.py").read_text().replace(
        'assert format_cents(1250) == "$12.50"', "pass")
    patch = _patch_file(py_spec, tmp_path, {"toyshop/pricing.py": good, "toyshop/money.py": money,
                                            "tests/test_money.py": tests})
    summary, _ = _run(local_factory, package, tmp_path, get_agent("command", cmd=f"git apply {patch}"))
    (result_file,) = list((tmp_path / "harness").glob("*/*/attempt-1/grade.json"))
    result = json.loads(result_file.read_text())
    assert result["tests_tampered"] == ["tests/test_money.py"]
    assert result["category"] == "regression" and "tests/test_money.py::test_format_cents" in result["p2p_failed"]


def test_real_agents_refused_on_local_backend(local_factory, package, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    with pytest.raises(ConfigError, match="Docker"):
        _run(local_factory, package, tmp_path, get_agent("claude-code"))


@pytest.mark.docker
def test_docker_oracle_and_no_leak(docker_factory, docker_package, tmp_path):
    summary, _ = _run(docker_factory, docker_package, tmp_path, get_agent("oracle"))
    assert summary.resolved_at_k == 1.0
    probe = get_agent("command", cmd="ls tests; test ! -e tests/test_bulk_discount.py && test ! -e solution")
    summary, _ = _run(docker_factory, docker_package, tmp_path, probe)
    (log_file,) = list((tmp_path / "harness").glob("*command/*/attempt-1/agent.log.json"))
    assert json.loads(log_file.read_text())["exit_code"] == 0  # hidden files are not in the agent sandbox


@pytest.mark.docker
def test_docker_agent_has_network_grader_does_not(docker_factory, docker_package, tmp_path):
    # a network interface besides loopback exists only when the sandbox has network
    agent = get_agent("command", cmd="ls /sys/class/net | grep -qv '^lo$'")
    _run(docker_factory, docker_package, tmp_path, agent)
    (log_file,) = list((tmp_path / "harness").glob("*/*/attempt-1/agent.log.json"))
    assert json.loads(log_file.read_text())["exit_code"] == 0


@pytest.mark.docker
@pytest.mark.parametrize("mode,reward", [("solve", "1"), ("nothing", "0"), ("cheat", "0"), ("solve+cheat", "1")])
def test_harbor_task_runs_like_harbor(docker_package, tmp_path, mode, reward):
    """Build the exported Harbor task and run solve.sh + test.sh with Harbor's mounts (/solution, /tests)."""
    task = to_harbor(docker_package, tmp_path / "harbor")
    tag = f"factory-harbor-test:{task.name[-20:]}".lower()
    subprocess.run(["docker", "build", "-q", "-t", tag, str(task / "environment")], check=True,
                   capture_output=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    from test_hardening import CHEAT_CMD

    script = ("bash /solution/solve.sh && " if "solve" in mode else "") + \
             (f"cd /repo && {CHEAT_CMD} && " if "cheat" in mode else "") + "bash /tests/test.sh"
    r = subprocess.run(["docker", "run", "--rm", "--network", "none",
                        "-v", f"{task / 'solution'}:/solution:ro", "-v", f"{task / 'tests'}:/tests:ro",
                        "-v", f"{logs}:/logs/verifier", tag, "bash", "-c", script],
                       capture_output=True, text=True, timeout=300)
    assert (logs / "reward.txt").read_text().strip() == reward, r.stdout + r.stderr


def test_agent_error_category(local_factory, package, tmp_path):
    summary, _ = _run(local_factory, package, tmp_path, get_agent("command", cmd="echo 'model not found' >&2; exit 1"))
    assert summary.categories == {"agent_error": 1}
