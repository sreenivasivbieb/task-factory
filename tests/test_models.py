import pytest
from pydantic import ValidationError

from factory.models import (RepoSpec, RunSummary, TaskDraft, TestResult, ValidationReport,
                            make_task_id, slugify)

SHA = "a" * 40


def test_repo_spec_requires_full_sha():
    with pytest.raises(ValidationError):
        RepoSpec(name="x", source=".", commit_sha="abc123", local_path=".")
    spec = RepoSpec(name="x", source=".", commit_sha=SHA, local_path=".")
    assert spec.short_sha == "aaaaaaa"


def test_round_trip(tmp_path):
    draft = TaskDraft(task_id="t", idea_id="i", kind="feature", requirements_md="r",
                      solution_patch="diff", test_files={"tests/test_x.py": "x"})
    path = draft.save(tmp_path / "d.json")
    assert TaskDraft.load(path) == draft
    assert '"schema_version": 1' in path.read_text()


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ValidationReport(task_id="t", verdict="valid", bogus=1)


def test_run_summary_counts():
    s = RunSummary.build([TestResult(test_id="a", status="passed"),
                          TestResult(test_id="b", status="failed")], 1)
    assert s.counts == {"passed": 1, "failed": 1, "skipped": 0, "error": 0}
    assert s.ids_with("failed") == {"b"}


def test_task_id_deterministic():
    tid = make_task_id("Toy Shop", SHA, "Bulk discount: 10% off!", 7)
    assert tid == "toy-shop-aaaaaaa-bulk-discount-10-off-007"
    assert tid == make_task_id("Toy Shop", SHA, "Bulk discount: 10% off!", 7)
    assert slugify("!!!") == "task"
