"""Grading cannot be gamed, packages are self-contained, and the workflow keeps human decisions."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from factory.cli import main
from factory.config import FactoryConfig, GatesConfig
from factory.errors import ConfigError
from factory.harness.agents import get_agent
from factory.harness.protect import RESTORE_SH, is_protected, patch_paths, protected_in_patch
from factory.harness.runner import Harness
from factory.harness.solutions import SolutionRepos
from factory.models import RejectCode
from factory.packaging.harbor import verifier_script
from factory.packaging.writer import REPO_TAR, write_package
from factory.repo.loader import load_repo
from factory.review.authoring import add_task, package_to_draft
from factory.review.store import TaskStore
from factory.validation.drafts import diff_from_overlay, load_draft_dir
from factory.validation.gates import Validator

TASKS = Path(__file__).parent / "fixtures" / "tasks" / "python"
CFG = FactoryConfig(gates=GatesConfig(stability_repeats=1))

# A root conftest.py that reports every test as passed: the classic way to fake a solution.
CHEAT_CONFTEST = (
    "import pytest\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    rep = outcome.get_result()\n"
    "    rep.outcome = 'passed'\n"
    "    rep.longrepr = None\n"
)
CHEAT_CMD = f"echo {base64.b64encode(CHEAT_CONFTEST.encode()).decode()} | base64 -d > conftest.py"


@pytest.fixture(scope="module")
def package(local_factory, py_spec, py_env_local, tmp_path_factory):
    draft = load_draft_dir(TASKS / "good_bulk_discount", py_spec)
    report = Validator(local_factory, py_spec, py_env_local, CFG.gates).validate(draft)
    return write_package(draft, report, py_spec, py_env_local, tmp_path_factory.mktemp("tasks"))


def _run(factory, pkgs, tmp_path, agent, solutions=None, **kw):
    h = Harness(factory, CFG, tmp_path / "harness", solutions=solutions)
    try:
        return h.run(pkgs, agent, **kw)
    finally:
        h.close()


def _grades(tmp_path):
    return [json.loads(p.read_text()) for p in sorted((tmp_path / "harness").glob("*/*/attempt-*/grade.json"))]


# --------------------------------------------------------------------------- protected files


def test_protected_paths():
    for p in ("tests/test_x.py", "conftest.py", "pkg/conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
              "pyproject.toml", "package.json", "jest.config.js", "vitest.config.mts", ".mocharc.yml",
              "sitecustomize.py", "x.pth", "go.mod", "Cargo.toml", "build.gradle.kts"):
        assert is_protected(p), p
    for p in ("toyshop/pricing.py", "lib/config.js", "README.md", "src/package_json.py"):
        assert not is_protected(p), p


def test_patch_paths_and_protected_in_patch(py_spec):
    diff = diff_from_overlay(py_spec, {"toyshop/new.py": "x = 1\n", "tests/conftest.py": "# changed\n"})
    assert patch_paths(diff) == ["tests/conftest.py", "toyshop/new.py"]
    assert protected_in_patch(diff) == ["tests/conftest.py"]


def test_restore_sh_undoes_every_kind_of_tampering(tmp_path):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "src.py").write_text("v = 1\n")
    (repo / "tests" / "test_a.py").write_text("def test_a(): assert True\n")
    (repo / "tests" / "test_b.py").write_text("def test_b(): assert True\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    def sh(cmd):
        return subprocess.run(["sh", "-c", cmd], cwd=repo, capture_output=True, text=True, check=True).stdout

    sh("git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -qm base && git tag factory-base")
    (repo / "src.py").write_text("v = 2\n")  # the real change: kept
    (repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")  # weakened: restored
    (repo / "tests" / "test_b.py").unlink()  # deleted: restored
    (repo / "conftest.py").write_text(CHEAT_CONFTEST)  # added: removed
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -k nothing\n")  # added: removed
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n[tool.pytest.ini_options]\naddopts = '-x'\n")
    (repo / "my file.py").write_text("ok\n")  # unprotected, with a space: kept
    out = sh(RESTORE_SH).split()
    assert sorted(out) == ["conftest.py", "pyproject.toml", "pytest.ini", "tests/test_a.py", "tests/test_b.py"]
    assert (repo / "src.py").read_text() == "v = 2\n" and (repo / "my file.py").exists()
    assert not (repo / "conftest.py").exists() and not (repo / "pytest.ini").exists()
    assert (repo / "tests" / "test_a.py").read_text() == "def test_a(): assert True\n"
    assert (repo / "tests" / "test_b.py").exists() and "tool.pytest" not in (repo / "pyproject.toml").read_text()


def test_reference_patch_may_not_touch_protected_files(local_factory, py_spec, py_env_local):
    report = Validator(local_factory, py_spec, py_env_local, CFG.gates).validate(
        load_draft_dir(TASKS / "bad_touches_protected", py_spec))
    assert report.reject_code == RejectCode.SOLUTION_TOUCHES_PROTECTED
    assert "tests/conftest.py" in report.reject_reason and [g.gate for g in report.gates] == ["G0", "G1"]


def test_cheating_conftest_does_not_resolve(local_factory, package, tmp_path):
    summary = _run(local_factory, [package], tmp_path, get_agent("command", cmd=CHEAT_CMD))
    (grade,) = _grades(tmp_path)
    assert not grade["resolved"] and grade["category"] == "wrong_behaviour"
    assert grade["tests_tampered"] == ["conftest.py"] and summary.resolved_at_k == 0.0


def test_real_solution_plus_extra_conftest_still_resolves(local_factory, package, py_spec, tmp_path):
    good = (TASKS / "good_bulk_discount" / "after" / "toyshop" / "pricing.py").read_text()
    patch = tmp_path / "good.diff"
    patch.write_text(diff_from_overlay(py_spec, {"toyshop/pricing.py": good}))
    _run(local_factory, [package], tmp_path, get_agent("command", cmd=f"git apply {patch} && {CHEAT_CMD}"))
    (grade,) = _grades(tmp_path)
    assert grade["resolved"] and grade["tests_tampered"] == ["conftest.py"]
    solution = next((tmp_path / "harness").glob("*/*/attempt-1/solution.diff")).read_text()
    assert "conftest.py" not in solution and "toyshop/pricing.py" in solution


def test_harbor_verifier_checks_hidden_tests_ran(py_env_local):
    script = verifier_script(py_env_local, [], ["tests/test_bulk_discount.py"], 3)
    assert "tests/test_bulk_discount.py || fail 'hidden feature tests failed'" in script
    assert 'only $ran of 3 hidden feature tests ran' in script and "<(failure|error|skipped)" in script
    bash = shutil.which("bash")
    if bash:
        assert subprocess.run([bash, "-n", "-c", script]).returncode == 0  # valid bash


# --------------------------------------------------------------------------- agent errors


def test_agent_errors_stop_the_run(local_factory, package, tmp_path):
    agent = get_agent("command", cmd="echo 'Free usage limit reached' >&2; exit 1")
    summary = _run(local_factory, [package, package, package], tmp_path, agent)
    assert len(_grades(tmp_path)) <= 2 and summary.stopped_early and "agent errors in a row" in summary.stopped_early
    assert summary.per_task[0].difficulty == "error"  # not evidence that the task is hard


def test_work_keeps_assignment_open_on_agent_error(package, tmp_path):
    wd = tmp_path / "wd"
    shutil.copytree(package, wd / "tasks" / package.name)
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[sandbox]\nbackend = "local"\n\n[harness.agents.broken]\nrun = "exit 1"\n')
    common = ["--workdir", str(wd), "--config", str(cfg)]
    assert main(["tasks", "assign", package.name, "--agent", "broken", *common]) == 0
    assert main(["harness", "work", *common]) == 0
    store = TaskStore(wd / "tasks")
    assert len(store.assignments()) == 1  # still open: retried by the next 'harness work'
    notes = [e.reason for e in store.events() if e.action == "note"]
    assert any("agent_error" in n and "left open" in n for n in notes)
    assert not any("unsolved" in n for n in notes)


# --------------------------------------------------------------------------- self-contained packages


def test_package_works_without_the_original_repo(local_factory, py_spec, py_env_local, tmp_path):
    src = tmp_path / "gone"
    shutil.copytree(py_spec.local_path, src)
    spec = load_repo(str(src), tmp_path / "wd")
    draft = load_draft_dir(TASKS / "good_bulk_discount", spec)
    report = Validator(local_factory, spec, py_env_local, CFG.gates).validate(draft)
    pkg = write_package(draft, report, spec, py_env_local, tmp_path / "tasks")
    assert (pkg / REPO_TAR).is_file()
    shutil.rmtree(src)
    shutil.rmtree(tmp_path / "wd")  # neither the source nor our clone exists any more
    good = (TASKS / "good_bulk_discount" / "after" / "toyshop" / "pricing.py").read_text()
    patch = tmp_path / "good.diff"
    patch.write_text(diff_from_overlay(py_spec, {"toyshop/pricing.py": good}))
    summary = _run(local_factory, [pkg], tmp_path, get_agent("command", cmd=f"git apply {patch}"),
                   solutions=SolutionRepos(tmp_path / "solutions"))
    (grade,) = _grades(tmp_path)
    assert summary.resolved_at_k == 1.0 and grade["branch"]
    repo = tmp_path / "solutions" / "gone"
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s", grade["branch"]], capture_output=True,
                         text=True, check=True).stdout
    assert "solved by command" in log and "(from repo.tar)" in log


# --------------------------------------------------------------------------- workflow keeps human decisions


@pytest.fixture
def wd_state(py_spec, py_env_local, tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    py_spec.save(wd / "repo_spec.json")
    py_env_local.save(wd / "env_config.json")
    cfg = tmp_path / "factory.toml"
    cfg.write_text('[sandbox]\nbackend = "local"\n\n[gates]\nstability_repeats = 1\n')
    return wd, ["--workdir", str(wd), "--config", str(cfg)]


def test_export_never_overwrites_a_reviewed_package(wd_state):
    wd, common = wd_state
    assert main(["validate", "--tasks", str(TASKS / "good_bulk_discount"), *common]) == 0
    assert main(["export", *common]) == 0
    (pkg,) = [p for p in (wd / "tasks").iterdir() if not p.name.startswith(".")]
    marker = pkg / "instruction.md"
    marker.write_text(marker.read_text() + "\nreviewer edit\n")
    assert main(["export", *common]) == 0
    assert "reviewer edit" in marker.read_text()
    assert main(["export", "--force", *common]) == 0
    assert "reviewer edit" not in marker.read_text()


def test_republish_removes_tasks_no_longer_approved(package, tmp_path):
    store_dir = tmp_path / "store"
    shutil.copytree(package, store_dir / package.name)
    common = ["--store", str(store_dir), "--workdir", str(tmp_path / "wd")]
    out = tmp_path / "pub"
    assert main(["tasks", "approve", package.name, *common]) == 0
    assert main(["tasks", "publish", "--out", str(out), *common]) == 0
    assert (out / package.name).is_dir()
    assert main(["tasks", "delete", package.name, "--reason", "too easy", *common]) == 0
    main(["tasks", "publish", "--out", str(out), "--include-pending", *common])
    assert not (out / package.name).exists()


def test_adding_an_existing_task_again_is_refused(package, py_spec, py_env_local, local_factory, tmp_path):
    store = TaskStore(tmp_path / "store")
    shutil.copytree(package, store.root / package.name)
    draft = package_to_draft(package, tmp_path / "again")  # task.json carries the existing task_id
    with pytest.raises(ConfigError, match="already exists"):
        add_task(draft, py_spec, py_env_local, local_factory, CFG, store)


def test_baseline_cache_depends_on_test_settings(local_factory, py_spec, py_env_local, tmp_path):
    other = py_env_local.model_copy(update={"test_timeout_s": 99})
    for env in (py_env_local, other):
        Validator(local_factory, py_spec, env, CFG.gates, cache_dir=tmp_path).baseline()
    assert len(list((tmp_path / "baseline").glob("*.json"))) == 2


# --------------------------------------------------------------------------- base image digest


def test_pin_image_resolves_a_digest(monkeypatch):
    from factory.config import SandboxConfig
    from factory.sandbox.docker_backend import DockerSandboxFactory

    f = object.__new__(DockerSandboxFactory)
    f.cfg, f.bin = SandboxConfig(), "docker"
    digests = json.dumps(["docker.io/library/python@sha256:" + "a" * 64])
    monkeypatch.setattr(f, "_run", lambda args, **kw: subprocess.CompletedProcess(args, 0, digests + "\n", ""))
    assert f.pin_image("python:3.11-slim") == "python:3.11-slim@sha256:" + "a" * 64
    assert f.pin_image("python:3.11-slim@sha256:" + "b" * 64).endswith("b" * 64)  # already pinned
    f.cfg = SandboxConfig(pin_base_image=False)
    assert f.pin_image("python:3.11-slim") == "python:3.11-slim"


def test_publish_never_removes_folders_the_store_does_not_know(tmp_path, capsys):
    """An empty or different store must not wipe a submission folder (seen: publishing from the wrong store)."""
    out = tmp_path / "submission"
    foreign = out / "someone-elses-task-001"
    foreign.mkdir(parents=True)
    (foreign / "task.toml").write_text('task_id = "someone-elses-task-001"\n')
    empty_store = tmp_path / "empty-store"
    empty_store.mkdir()
    assert main(["tasks", "publish", "--out", str(out), "--store", str(empty_store)]) == 4
    assert foreign.exists()
    assert "does not know" in capsys.readouterr().out


def test_reports_and_packages_record_the_backend(package):
    from factory.review.store import read_meta

    assert read_meta(package).validation.backend == "local"  # the fixture validates with the local backend
    assert json.loads((package / "validation" / "report.json").read_text())["backend"] == "local"
