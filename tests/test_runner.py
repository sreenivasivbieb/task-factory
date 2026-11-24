from pathlib import Path

import pytest

from factory.models import RunSummary, TestResult
from factory.runner.parsers import parse_cargo_text, parse_go_json, parse_jest_json, parse_junit
from factory.runner.test_runner import build_command, flaky_ids, run_tests


def run_repeated(sb, env, n, select):
    """What gate G5 does: repeat inside one sandbox, then compare."""
    runs = [run_tests(sb, env, select) for _ in range(n)]
    return runs, flaky_ids(runs)

REPORTS = Path(__file__).parent / "fixtures" / "reports"


def _by_id(results):
    return {r.test_id: r.status for r in results}


def test_parse_pytest_junit():
    got = _by_id(parse_junit((REPORTS / "pytest_junit.xml").read_text(), ["/repo"]))
    assert got == {
        "tests/test_store.py::test_add": "passed",
        "tests/test_store.py::TestRemove::test_remove": "failed",
        "tests/test_store.py::test_skip": "skipped",
        "tests/test_other.py::test_param[a-1]": "error",
        "tests/test_other.py::test_ok": "passed",
    }


def test_parse_surefire():
    got = _by_id(parse_junit((REPORTS / "surefire.xml").read_text()))
    assert got == {"com.acme.CalcTest::adds": "passed", "com.acme.CalcTest::divides": "failed"}


def test_parse_jest():
    got = _by_id(parse_jest_json((REPORTS / "jest.json").read_text(), ["/repo"]))
    assert got == {"src/sum.test.js::sum adds": "passed", "src/sum.test.js::sum handles NaN": "failed",
                   "src/sum.test.js::sum later": "skipped", "src/broken.test.js::<suite>": "error"}


def test_parse_go():
    got = _by_id(parse_go_json((REPORTS / "go.json").read_text()))
    assert got == {"example.com/m/calc::TestAdd": "passed", "example.com/m/calc::TestDiv": "failed",
                   "example.com/m/calc::TestLater": "skipped"}


def test_parse_cargo():
    got = _by_id(parse_cargo_text((REPORTS / "cargo.txt").read_text()))
    assert got == {"calc::tests::adds": "passed", "calc::tests::divides": "failed",
                   "calc::tests::later": "skipped"}


def test_duplicate_ids_get_suffix():
    xml = '<testsuite><testcase classname="c" name="t"/><testcase classname="c" name="t"/></testsuite>'
    assert [r.test_id for r in parse_junit(xml)] == ["c::t", "c::t[2]"]


def test_build_command_select(py_env_local):
    assert build_command(py_env_local).endswith("--junitxml=.factory/report.xml")
    cmd = build_command(py_env_local, ["tests/test_a.py::test_x y"])
    assert cmd.endswith("'tests/test_a.py::test_x y'")


def test_flaky_ids():
    a = RunSummary.build([TestResult(test_id="x", status="passed"), TestResult(test_id="y", status="passed")], 0)
    b = RunSummary.build([TestResult(test_id="x", status="failed"), TestResult(test_id="y", status="passed")], 1)
    assert flaky_ids([a, b]) == {"x"}
    assert flaky_ids([a, a]) == set()


# --------------------------------------------------------------------------- real runs


@pytest.fixture
def local_image(local_factory, py_spec, py_env_local):
    return local_factory.build_image(py_spec, py_env_local)


def test_full_suite_python(local_factory, local_image, py_env_local):
    with local_factory.new_sandbox(local_image) as sb:
        s = run_tests(sb, py_env_local)
    assert s.error is None
    assert s.counts["passed"] == 28 and s.counts["failed"] == 0
    assert "tests/test_cart.py::test_add_and_quantity" in s.status_of()


def test_select_runs_only_chosen(local_factory, local_image, py_env_local):
    with local_factory.new_sandbox(local_image) as sb:
        s = run_tests(sb, py_env_local, ["tests/test_money.py", "tests/test_cart.py::test_lines_sorted"])
    ids = set(s.status_of())
    assert len(ids) == 7
    assert all(i.startswith("tests/test_money.py::") or i == "tests/test_cart.py::test_lines_sorted" for i in ids)


def test_crash_before_tests_is_error(local_factory, local_image, py_env_local):
    env = py_env_local.model_copy(update={"test_command": "python -c 'import sys; sys.exit(2)'"})
    with local_factory.new_sandbox(local_image) as sb:
        s = run_tests(sb, env)
    assert s.crashed and s.results == [] and "no parsed tests" in s.error


def test_collection_error_is_not_all_passed(local_factory, local_image, py_env_local):
    with local_factory.new_sandbox(local_image) as sb:
        sb.write_file("tests/test_new.py", "from toyshop.nope import x\n\ndef test_x():\n    assert x\n")
        s = run_tests(sb, py_env_local, ["tests/test_new.py"])
    assert s.raw_exit_code != 0
    assert s.counts["passed"] == 0


def test_flaky_detected(local_factory, local_image, py_env_local):
    env = py_env_local.model_copy(update={"env_vars": {"TOY_FLAKY": "1"}})
    with local_factory.new_sandbox(local_image) as sb:
        runs, flaky = run_repeated(sb, env, n=3, select=["tests/test_flaky.py"])
    assert flaky == {"tests/test_flaky.py::test_sometimes_flaky"}


def test_log_written(local_factory, local_image, py_env_local, tmp_path):
    with local_factory.new_sandbox(local_image) as sb:
        run_tests(sb, py_env_local, ["tests/test_money.py"], log_to=tmp_path / "log.json")
    assert '"command"' in (tmp_path / "log.json").read_text()


@pytest.mark.docker
def test_node_suite_in_docker(docker_factory, node_spec, node_env_docker):
    image = docker_factory.build_image(node_spec, node_env_docker)
    with docker_factory.new_sandbox(image) as sb:
        full = run_tests(sb, node_env_docker)
        assert full.error is None and full.counts["passed"] == 22
        assert "test/slug.test.js::slugify basic" in full.status_of()
        part = run_tests(sb, node_env_docker, ["test/wrap.test.js"])
        assert len(part.results) == 6
    env = node_env_docker.model_copy(update={"env_vars": {"TOY_FLAKY": "1"}})
    with docker_factory.new_sandbox(image) as sb:
        _, flaky = run_repeated(sb, env, n=2, select=["test/flaky.test.js"])
    assert flaky == {"test/flaky.test.js::sometimes flaky"}


@pytest.mark.docker
def test_python_suite_in_docker(docker_factory, py_spec, py_env_docker):
    image = docker_factory.build_image(py_spec, py_env_docker)
    with docker_factory.new_sandbox(image) as sb:
        s = run_tests(sb, py_env_docker)
    assert s.error is None and s.counts["passed"] == 28
