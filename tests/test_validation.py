import json
from pathlib import Path

import pytest

from factory.config import ALL_MUTATIONS, GatesConfig
from factory.models import RejectCode
from factory.validation.drafts import diff_from_overlay, load_draft_dir
from factory.validation.gates import Validator, append_rejection
from factory.validation.mutations import (drop_error_branch, flip_comparison, hardcode_expected, make_mutants,
                                          revert_half, swap_arguments)

TASKS = Path(__file__).parent / "fixtures" / "tasks"
PY_TASKS = sorted(p for p in (TASKS / "python").iterdir() if p.is_dir())
NODE_TASKS = sorted(p for p in (TASKS / "node").iterdir() if p.is_dir()) if (TASKS / "node").exists() else []
FULL = GatesConfig(stability_repeats=1, mutations=ALL_MUTATIONS)


def _expect(task_dir: Path) -> str:
    return json.loads((task_dir / "task.json").read_text())["expect"]


# --------------------------------------------------------------------------- mutation operators

PATCH = """\
diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,6 @@
 def f(a, b):
-    return a
+    if a >= 10:
+        raise ValueError("a >= 10")
+    x = g(a, b)
+    return x
"""


def test_flip_comparison_ignores_strings():
    (m,) = flip_comparison(PATCH, limit=1)
    assert "+    if a > 10:" in m and '"a >= 10"' in m


def test_drop_error_branch():
    (m,) = drop_error_branch(PATCH)
    assert "+        pass" in m and "raise" not in m


def test_swap_arguments():
    (m,) = swap_arguments(PATCH, limit=1)
    assert "g(b, a)" in m


def test_hardcode_expected():
    (m,) = hardcode_expected(PATCH, {"t.py": "assert f(1, 2) == 42"})
    assert "+    return 42" in m


def test_revert_half_recomputes_header():
    (m,) = revert_half(PATCH)
    assert "@@ -1,2 +1,3 @@" in m and "g(a, b)" not in m


def test_make_mutants_dedupes():
    ms = make_mutants(PATCH, {"t.py": "assert f(1, 2) == 42"}, ALL_MUTATIONS)
    names = [m.name.split("#")[0] for m in ms]
    assert set(names) == {"revert_half", "flip_comparison", "swap_arguments", "drop_error_branch",
                          "hardcode_expected"}
    assert len({m.patch for m in ms}) == len(ms)


# --------------------------------------------------------------------------- drafts


def test_draft_patch_computed_from_overlay(py_spec):
    draft = load_draft_dir(TASKS / "python" / "good_bulk_discount", py_spec)
    assert draft.solution_patch.startswith("diff --git a/toyshop/pricing.py b/toyshop/pricing.py")
    assert "+BULK_MIN_QTY = 10" in draft.solution_patch
    assert draft.touched_files == ["toyshop/pricing.py"]
    assert list(draft.test_files) == ["tests/test_bulk_discount.py"]
    assert draft.task_id.startswith(f"toyshop-{py_spec.short_sha}-bulk-discount-on-cart-lines-")


# --------------------------------------------------------------------------- gates (local backend)


@pytest.fixture(scope="module")
def local_validator(local_factory, py_spec, py_env_local, tmp_path_factory):
    return Validator(local_factory, py_spec, py_env_local, FULL, log_dir=tmp_path_factory.mktemp("logs"))


@pytest.mark.parametrize("task_dir", PY_TASKS, ids=lambda p: p.name)
def test_python_task_gets_expected_verdict(task_dir, local_validator, py_spec):
    report = local_validator.validate(load_draft_dir(task_dir, py_spec))
    expect = _expect(task_dir)
    if expect == "valid":
        assert report.verdict == "valid", report.reject_reason
        assert [g.gate for g in report.gates] == ["G0", "G1", "G2", "G3", "G4", "G6", "G5"]
        assert report.fail_to_pass == ["tests/test_bulk_discount.py::test_bulk_discount_boundary",
                                       "tests/test_bulk_discount.py::test_bulk_discount_in_subtotal",
                                       "tests/test_bulk_discount.py::test_bulk_discount_rounds_half_up"]
        assert len(report.pass_to_pass) == 28
        assert report.mutation_score == 1.0 and report.mutants_run >= 3
        assert all(g.exec_log_paths for g in report.gates if g.gate != "G1")
    else:
        assert report.verdict == "rejected"
        assert report.reject_code == RejectCode(expect), report.reject_reason


def test_rejection_names_tests(local_validator, py_spec):
    report = local_validator.validate(load_draft_dir(TASKS / "python" / "bad_passes_without_patch", py_spec))
    assert "tests/test_bulk_discount.py::test_small_line_unchanged" in report.reject_reason
    assert "test_bulk_line" not in report.reject_reason


def test_flaky_lists_tests(local_validator, py_spec):
    report = local_validator.validate(load_draft_dir(TASKS / "python" / "bad_flaky", py_spec))
    assert report.flaky_tests == ["tests/test_bulk_discount.py::test_bulk_discount_stateful"]


def test_hand_written_mutant_is_caught(local_validator, py_spec):
    """The off-by-one mutant looks right but the good task's tests reject it."""
    good = load_draft_dir(TASKS / "python" / "good_bulk_discount", py_spec)
    mutant_src = (TASKS / "python" / "good_bulk_discount" / "mutant" / "toyshop" / "pricing.py").read_text()
    mutant = good.model_copy(update={"task_id": good.task_id + "-mutant",
                                     "solution_patch": diff_from_overlay(py_spec, {"toyshop/pricing.py": mutant_src})})
    report = local_validator.validate(mutant)
    assert report.reject_code == RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH
    assert "test_bulk_discount_boundary" in report.reject_reason


def test_baseline_fail(local_factory, py_spec, py_env_local):
    env = py_env_local.model_copy(update={"test_command": "python -m pytest --junitxml={report} no_such_dir"})
    report = Validator(local_factory, py_spec, env, FULL).validate(
        load_draft_dir(TASKS / "python" / "good_bulk_discount", py_spec))
    assert report.reject_code == RejectCode.BASELINE_FAIL


def test_same_task_twice_same_report(local_factory, py_spec, py_env_local):
    draft = load_draft_dir(TASKS / "python" / "good_bulk_discount", py_spec)
    a = Validator(local_factory, py_spec, py_env_local, FULL).validate(draft)
    b = Validator(local_factory, py_spec, py_env_local, FULL).validate(draft)
    strip = {"gates": {"__all__": {"duration_s", "exec_log_paths"}}}
    assert a.model_dump(exclude=strip) == b.model_dump(exclude=strip)


def test_append_rejection(local_validator, py_spec, tmp_path):
    report = local_validator.validate(load_draft_dir(TASKS / "python" / "bad_regression", py_spec))
    append_rejection(tmp_path / "rejections.jsonl", report)
    entry = json.loads((tmp_path / "rejections.jsonl").read_text())
    assert entry["reject_code"] == "REGRESSION" and "test_format_cents" in entry["reject_reason"]


def test_baseline_cached(local_factory, py_spec, py_env_local, tmp_path):
    v = Validator(local_factory, py_spec, py_env_local, FULL, cache_dir=tmp_path)
    v.baseline()
    assert list((tmp_path / "baseline").glob("*.json"))


# --------------------------------------------------------------------------- gates (Docker)


@pytest.fixture(scope="module")
def docker_py_validator(docker_factory, py_spec, py_env_docker):
    return Validator(docker_factory, py_spec, py_env_docker, FULL)


@pytest.fixture(scope="module")
def docker_node_validator(docker_factory, node_spec, node_env_docker):
    return Validator(docker_factory, node_spec, node_env_docker, FULL)


@pytest.mark.docker
@pytest.mark.parametrize("task_dir", PY_TASKS, ids=lambda p: p.name)
def test_python_task_in_docker(task_dir, docker_py_validator, py_spec):
    report = docker_py_validator.validate(load_draft_dir(task_dir, py_spec))
    expect = _expect(task_dir)
    assert (report.verdict if expect == "valid" else report.reject_code.value) == expect, report.reject_reason


@pytest.mark.docker
@pytest.mark.parametrize("task_dir", NODE_TASKS, ids=lambda p: p.name)
def test_node_task_in_docker(task_dir, docker_node_validator, node_spec):
    report = docker_node_validator.validate(load_draft_dir(task_dir, node_spec))
    expect = _expect(task_dir)
    assert (report.verdict if expect == "valid" else report.reject_code.value) == expect, report.reject_reason
