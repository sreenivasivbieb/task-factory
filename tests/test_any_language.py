"""The pipeline on a language it has no code for: Bash, described only in factory.toml.

tests/fixtures/tasks/bash/factory.toml is the whole language definition (markers, image,
test command, test paths, mutation rules). This runs the full pipeline on it in Docker:
load, inspect (baseline), validate with every mutant, export, oracle/noop, and a cheating
agent that rewrites the test helper, graded with no Bash config at all (the package carries
its protected paths).
"""

import json
import shutil

import pytest

from factory.cli import main
from factory.ecosystems import configure
from helpers import FIXTURES, git

BASH_REPO = FIXTURES / "toy_bash_repo"
BASH_TASKS = FIXTURES / "tasks" / "bash"
STRICT = ('\n[gates]\nstability_repeats = 2\nmutations = ["empty", "revert_half", "flip_comparison", '
          '"swap_arguments", "drop_error_branch", "hardcode_expected"]\n')


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    configure({})  # later tests must not see the Bash ecosystem


@pytest.mark.docker
@pytest.mark.slow
def test_bash_repo_runs_the_whole_pipeline_from_config_alone(tmp_path):
    repo = tmp_path / "bashrepo"
    shutil.copytree(BASH_REPO, repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    config = tmp_path / "factory.toml"
    config.write_text((BASH_TASKS / "factory.toml").read_text() + STRICT)
    wd = tmp_path / "wd"
    opts = ["--workdir", str(wd), "--config", str(config)]

    assert main(["load", "--repo", str(repo), *opts]) == 0
    assert json.loads((wd / "repo_spec.json").read_text())["language"] == "bash"
    assert main(["inspect", *opts]) == 0
    env = json.loads((wd / "env_config.json").read_text())
    assert env["framework"] == "bash" and env["base_image"].startswith("bash:5.2@sha256:")
    assert "(^|/)t/" in env["protected_paths"]

    assert main(["validate", "--tasks", str(BASH_TASKS / "slugify"), *opts]) == 0
    (report,) = wd.glob("runs/*/report.json")
    gates = {g["gate"]: g for g in json.loads(report.read_text())["gates"]}
    assert all(g["passed"] for g in gates.values())
    assert "drop_error_branch#1: caught" in gates["G6"]["details"]  # the Bash rule from factory.toml
    assert "hardcode_expected#1: caught" in gates["G6"]["details"]  # printf, not return, in Bash

    assert main(["export", *opts]) == 0
    assert main(["harness", "run", "--agent", "oracle", *opts]) == 0
    assert main(["harness", "run", "--agent", "noop", *opts]) == 0
    results = [json.loads(line) for f in sorted(wd.glob("harness/*/results.jsonl")) for line in f.read_text().splitlines()]
    assert [(r["agent"], r["resolved"]) for r in results] == [("oracle", True), ("noop", False)]

    # A cheat that makes every assertion pass, graded with no Bash ecosystem configured.
    configure({})
    cheat = "printf 'assert_eq() { :; }\\n' > t/assert.sh"
    assert main(["harness", "run", "--agent", "command", "--cmd", cheat, "--no-branch", "--workdir", str(wd)]) == 0
    (grade,) = [json.loads(p.read_text()) for p in wd.glob("harness/*-command/*/attempt-1/grade.json")]
    assert not grade["resolved"] and grade["tests_tampered"] == ["t/assert.sh"]
