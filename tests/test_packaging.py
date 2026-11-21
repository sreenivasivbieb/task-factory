import tomllib
from pathlib import Path

import pytest

from factory.cli import main
from factory.config import FactoryConfig, GatesConfig
from factory.errors import PackageError
from factory.models import TaskPackage
from factory.packaging import toml_writer
from factory.packaging.verify import read_package, verify_checksums, verify_package
from factory.packaging.writer import write_package
from factory.validation.drafts import load_draft_dir
from factory.validation.gates import Validator

TASKS = Path(__file__).parent / "fixtures" / "tasks"
CFG = FactoryConfig(gates=GatesConfig(stability_repeats=1))


@pytest.fixture(scope="module")
def validated(local_factory, py_spec, py_env_local, tmp_path_factory):
    v = Validator(local_factory, py_spec, py_env_local, CFG.gates, log_dir=tmp_path_factory.mktemp("logs"))
    good = load_draft_dir(TASKS / "python" / "good_bulk_discount", py_spec)
    bad = load_draft_dir(TASKS / "python" / "bad_regression", py_spec)
    return good, v.validate(good), bad, v.validate(bad)


def test_toml_writer_round_trip():
    data = {"a": 1, "b": "x \"q\"\n", "c": [1, 2, 3, 4], "t": {"x": True, "f": 0.5, "n": None},
            "arr": [{"k": "v"}, {"k": "w"}]}
    back = tomllib.loads(toml_writer.dumps(data))
    assert back == {"a": 1, "b": "x \"q\"\n", "c": [1, 2, 3, 4], "t": {"x": True, "f": 0.5},
                    "arr": [{"k": "v"}, {"k": "w"}]}


def test_write_and_reload(validated, py_spec, py_env_local, tmp_path):
    good, report, _, _ = validated
    pkg = write_package(good, report, py_spec, py_env_local, tmp_path / "tasks")
    assert pkg.name == good.task_id
    for rel in ["instruction.md", "task.toml", "solution/solution.diff", "tests/run_tests.sh",
                "tests/files/tests/test_bulk_discount.py", "environment/Dockerfile", "environment/env.json",
                "validation/report.json", "checksums.txt"]:
        assert (pkg / rel).is_file(), rel
    assert list((pkg / "validation" / "logs").glob("*.json"))
    meta = TaskPackage.model_validate(tomllib.loads((pkg / "task.toml").read_text()))
    assert meta.commit_sha == py_spec.commit_sha and meta.fail_to_pass == report.fail_to_pass
    assert meta.validation.verdict == "valid" and meta.validation.mutation_score == 1.0
    _, draft = read_package(pkg)
    assert draft.solution_patch == good.solution_patch and draft.test_files == good.test_files
    assert verify_checksums(pkg) == []
    assert not list((tmp_path / "tasks").glob(".*"))  # no temp folders left behind


def test_checksums_are_deterministic(validated, py_spec, py_env_local, tmp_path):
    good, report, _, _ = validated
    a = write_package(good, report, py_spec, py_env_local, tmp_path / "a")
    b = write_package(good, report, py_spec, py_env_local, tmp_path / "b")
    assert (a / "checksums.txt").read_text() == (b / "checksums.txt").read_text()


def test_changed_byte_fails_checksum(validated, py_spec, py_env_local, tmp_path, local_factory):
    good, report, _, _ = validated
    pkg = write_package(good, report, py_spec, py_env_local, tmp_path / "tasks")
    f = pkg / "tests" / "files" / "tests" / "test_bulk_discount.py"
    f.write_bytes(f.read_bytes().replace(b"900", b"901", 1))
    assert verify_checksums(pkg) == ["tests/files/tests/test_bulk_discount.py"]
    with pytest.raises(PackageError, match="checksum"):
        verify_package(pkg, local_factory, CFG)


def test_rejected_task_writes_nothing(validated, py_spec, py_env_local, tmp_path):
    _, _, bad, bad_report = validated
    with pytest.raises(PackageError):
        write_package(bad, bad_report, py_spec, py_env_local, tmp_path / "tasks")
    assert not (tmp_path / "tasks").exists()


def test_round_trip_reverify_local(validated, py_spec, py_env_local, tmp_path, local_factory):
    good, report, _, _ = validated
    pkg = write_package(good, report, py_spec, py_env_local, tmp_path / "tasks")
    again = verify_package(pkg, local_factory, CFG)
    assert again.verdict == "valid" and again.fail_to_pass == report.fail_to_pass


def test_run_script_runs_hidden_tests(validated, py_spec, py_env_local, tmp_path, local_factory):
    """The package's own run script, run against the reference solution, passes."""
    import subprocess

    good, report, _, _ = validated
    pkg = write_package(good, report, py_spec, py_env_local, tmp_path / "tasks")
    image = local_factory.build_image(py_spec, py_env_local)
    with local_factory.new_sandbox(image) as sb:
        assert sb.apply_patch((pkg / "solution" / "solution.diff").read_text()).ok
        r = subprocess.run(["sh", str(pkg / "tests" / "run_tests.sh")], env={"REPO_DIR": str(sb.repo),
                           "PATH": __import__("os").environ["PATH"]}, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.docker
def test_cli_validate_export_reverify_docker(py_repo, tmp_path, capsys):
    wd, out = tmp_path / "wd", tmp_path / "tasks"
    assert main(["load", "--repo", str(py_repo), "--workdir", str(wd)]) == 0
    assert main(["inspect", "--workdir", str(wd)]) == 0
    assert main(["validate", "--tasks", str(TASKS / "python"), "--workdir", str(wd)]) == 0
    assert main(["export", "--out", str(out), "--workdir", str(wd)]) == 0
    (pkg,) = list(out.iterdir())
    assert main(["validate", "--tasks", str(out), "--workdir", str(wd)]) == 0
    assert "1/1 valid" in capsys.readouterr().out
