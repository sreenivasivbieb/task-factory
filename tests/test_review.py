import json
import shutil
import tomllib
from pathlib import Path

import pytest
from rich.console import Console

from factory.config import FactoryConfig, GatesConfig
from factory.errors import ConfigError
from factory.packaging.harbor import to_harbor
from factory.packaging.writer import write_package
from factory.review import render
from factory.review.authoring import add_task, apply_edit, package_to_draft, scaffold_draft
from factory.review.interactive import review_loop
from factory.review.store import TaskStore
from factory.validation.drafts import load_draft_dir
from factory.validation.gates import Validator

TASKS = Path(__file__).parent / "fixtures" / "tasks" / "python"
CFG = FactoryConfig(gates=GatesConfig(stability_repeats=1))


@pytest.fixture(scope="module")
def good(local_factory, py_spec, py_env_local):
    draft = load_draft_dir(TASKS / "good_bulk_discount", py_spec)
    return draft, Validator(local_factory, py_spec, py_env_local, CFG.gates).validate(draft)


@pytest.fixture
def store(good, py_spec, py_env_local, tmp_path):
    draft, report = good
    root = tmp_path / "tasks"
    for suffix in ("a", "b"):
        d = draft.model_copy(update={"task_id": f"{draft.task_id}-{suffix}"})
        r = report.model_copy(update={"task_id": d.task_id})
        write_package(d, r, py_spec, py_env_local, root)
    return TaskStore(root, reviewer="tester")


def _ids(entries):
    return [e.task_id for e in entries]


def test_status_flow(store):
    a, b = _ids(store.entries())
    assert [e.status for e in store.entries()] == ["pending", "pending"]
    store.approve(a, "looks good")
    with pytest.raises(ConfigError, match="reason"):
        store.delete(b, "  ")
    store.delete(b, "requirements too vague")
    assert not (store.root / b).exists() and (store.trash / b / "task.toml").exists()
    assert _ids(store.entries("approved")) == [a]
    assert _ids(store.entries("deleted")) == [b]
    summary = store.summary()
    assert summary["counts"] == {"pending": 0, "approved": 1, "deleted": 1}
    assert summary["deleted"] == [{"task_id": b, "reason": "requirements too vague", "by": "tester"}]
    store.restore(b)
    assert store.get(b).status == "pending"
    actions = [e.action for e in store.events()]
    assert actions == ["approve", "delete", "restore"]


def test_partial_id_lookup(store):
    a, _ = _ids(store.entries())
    assert store.get(a[:-1] + "a").task_id == a
    with pytest.raises(ConfigError, match="matches 2"):
        store.get("bulk-discount")


def test_render_all_sections(store):
    console = Console(record=True, width=120)
    render.show(console, store.entries()[0], "all")
    text = console.export_text()
    for needle in ("Bulk discount on cart lines", "+BULK_MIN_QTY = 10", "test_bulk_discount_boundary",
                   "G0", "G6", "the only file the solver sees", "pending"):
        assert needle in text, needle
    console = Console(record=True, width=160)
    console.print(render.tasks_table(store.entries()))
    assert "3" in console.export_text()


def test_interactive_review(store):
    answers = iter(["p", "a", "ok", "d", "", "d", "tests too weak", "q"])
    console = Console(record=True, width=120)
    done = review_loop(store, console, lambda _: next(answers))
    assert done == {"approved": 1, "deleted": 1, "skipped": 0, "edited": 0}
    assert "a reason is required" in console.export_text()
    assert [e.status for e in store.entries()] == ["approved", "deleted"]
    assert review_loop(store, Console(record=True), lambda _: "q") == {"approved": 0, "deleted": 0,
                                                                        "skipped": 0, "edited": 0}


def test_scaffold_and_add_own_task(tmp_path, py_spec, py_env_local, local_factory):
    store = TaskStore(tmp_path / "tasks", reviewer="tester")
    draft = scaffold_draft(tmp_path / "drafts" / "bulk", "Bulk discount", tags=["validation rule"])
    assert (draft / "HOW_TO.md").exists()
    with pytest.raises(ConfigError, match="template"):
        add_task(draft, py_spec, py_env_local, local_factory, CFG, store)
    src = TASKS / "good_bulk_discount"
    shutil.copy(src / "requirements.md", draft / "requirements.md")
    shutil.copytree(src / "after", draft / "after", dirs_exist_ok=True)
    shutil.copytree(src / "tests", draft / "tests", dirs_exist_ok=True)
    result = add_task(draft, py_spec, py_env_local, local_factory, CFG, store, author="Asha")
    assert result.report.verdict == "valid" and result.package.exists()
    entry = store.get(result.draft.task_id)
    assert entry.status == "approved" and entry.author == "human (Asha)"
    assert store.summary()["human_authored"] == [entry.task_id]


def test_add_rejected_draft_writes_nothing(tmp_path, py_spec, py_env_local, local_factory):
    store = TaskStore(tmp_path / "tasks", reviewer="tester")
    draft = tmp_path / "weak"
    shutil.copytree(TASKS / "bad_weak_tests", draft)
    result = add_task(draft, py_spec, py_env_local, local_factory, CFG, store)
    assert result.package is None and result.report.reject_code.value == "NEG_CONTROL_PASSED"
    assert store.entries() == []


def test_edit_is_revalidated_and_disclosed(store, tmp_path, local_factory):
    entry = store.entries()[0]
    store.approve(entry.task_id)
    draft = package_to_draft(entry.path, tmp_path / "edit")
    req = draft / "requirements.md"
    req.write_text(req.read_text() + "\n- Quantities are whole numbers.\n")
    with pytest.raises(ConfigError, match="reason"):
        apply_edit(store, entry.task_id, draft, "", local_factory, CFG)
    result = apply_edit(store, entry.task_id, draft, "clarified quantities", local_factory, CFG)
    assert result.package is not None
    meta = tomllib.loads((entry.path / "task.toml").read_text())
    assert meta["manual_edit"] is True and meta["manual_edit_reason"] == "clarified quantities"
    assert "whole numbers" in (entry.path / "instruction.md").read_text()
    assert store.get(entry.task_id).status == "pending"  # edited tasks need a fresh look


def test_bad_edit_keeps_package(store, tmp_path, local_factory):
    entry = store.entries()[0]
    before = (entry.path / "checksums.txt").read_text()
    draft = package_to_draft(entry.path, tmp_path / "edit")
    (draft / "tests" / "tests" / "test_bulk_discount.py").write_text(
        "from toyshop.pricing import line_total\n\ndef test_x():\n    assert line_total(100, 20) == 1800\n")
    result = apply_edit(store, entry.task_id, draft, "simplify tests", local_factory, CFG)
    assert result.package is None
    assert (entry.path / "checksums.txt").read_text() == before


def test_harbor_export(store, tmp_path):
    entry = store.entries()[0]
    out = to_harbor(entry.path, tmp_path / "harbor")
    for rel in ("instruction.md", "task.toml", "environment/Dockerfile", "environment/repo.tar",
                "solution/solve.sh", "solution/solution.diff", "tests/test.sh",
                "tests/files/tests/test_bulk_discount.py"):
        assert (out / rel).exists(), rel
    meta = tomllib.loads((out / "task.toml").read_text())
    assert meta["metadata"]["base_commit_hash"] == entry.meta.commit_sha
    assert meta["metadata"]["display_title"] == "Bulk discount on cart lines"
    test_sh = (out / "tests" / "test.sh").read_text()
    assert "/logs/verifier/reward.txt" in test_sh and "cp -R /tests/files/. ./" in test_sh
    assert "git apply --whitespace=nowarn /solution/solution.diff" in (out / "solution" / "solve.sh").read_text()


def test_cli_publish(store, tmp_path):
    from factory.cli import main

    a, b = _ids(store.entries())
    assert main(["tasks", "publish", "--out", str(tmp_path / "pub"), "--store", str(store.root),
                 "--workdir", str(tmp_path / "wd")]) == 4  # nothing approved yet
    assert main(["tasks", "approve", a, "--store", str(store.root), "--workdir", str(tmp_path / "wd")]) == 0
    assert main(["tasks", "delete", b, "--reason", "duplicate", "--store", str(store.root),
                 "--workdir", str(tmp_path / "wd")]) == 0
    assert main(["tasks", "publish", "--out", str(tmp_path / "pub"), "--store", str(store.root),
                 "--workdir", str(tmp_path / "wd")]) == 0
    assert sorted(p.name for p in (tmp_path / "pub").iterdir()) == sorted([a, "review_summary.json"])
    summary = json.loads((tmp_path / "pub" / "review_summary.json").read_text())
    assert summary["deleted"][0]["reason"] == "duplicate"
