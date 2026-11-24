"""The automated factory: opencode backend, edits, ideation, repair loop, produce command."""
import json
import tomllib
from pathlib import Path

import pytest

from factory.cli import main
from factory.errors import ModelProviderError, PackageError
from factory.generation import ideas as ideas_mod
from factory.generation.author import apply_edits, outline, repo_context
from factory.generation.ideas import Idea, check_idea, select_distinct
from factory.model_client import ModelClient, opencode_text, uses_opencode_cli

TASK = Path(__file__).parent / "fixtures/tasks/python/good_bulk_discount"
HIDDEN = (TASK / "tests/tests/test_bulk_discount.py").read_text()
REQUIREMENTS = (TASK / "requirements.md").read_text()

GOOD_EDITS = [{
    "path": "toyshop/pricing.py",
    "search": '    """Total for one cart line."""\n    return price_cents * qty\n',
    "replace": ('    """Total for one cart line. Lines with 10 or more items get 10 % off."""\n'
                "    amount = price_cents * qty\n"
                "    if qty >= 10:\n"
                "        amount -= (amount * 10 + 50) // 100\n"
                "    return amount\n"),
}]
# A wrong first draft: the discount starts one item too late, so the boundary test fails.
BAD_EDITS = [dict(GOOD_EDITS[0], replace=GOOD_EDITS[0]["replace"].replace("qty >= 10", "qty > 10"))]

IDEAS = {"ideas": [
    {"id": "bulk-discount", "title": "Bulk discount on cart lines", "summary": "Large lines get a discount.",
     "category": "validation rule", "kind": "feature", "files": ["toyshop/pricing.py"],
     "behaviour": ["10 or more items: 10% off"], "new_names": ["BULK_MIN_QTY"]},
    {"id": "bulk-discount-2", "title": "Bulk discount on cart lines again", "summary": "Large lines get a discount.",
     "category": "validation rule", "files": ["toyshop/pricing.py"]},
    {"id": "fix-rounding", "title": "Fix the rounding bug", "summary": "fix a bug", "category": "x",
     "files": ["toyshop/money.py"]},
    {"id": "ghost", "title": "Ghost feature", "summary": "s", "category": "y", "files": ["nope.py"]},
]}


# --------------------------------------------------------------------------- opencode backend


def test_opencode_text_joins_text_parts_and_reports_errors():
    out = "\n".join([
        json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
        json.dumps({"type": "text", "part": {"id": "p1", "type": "text", "text": "{\"a\""}}),
        json.dumps({"type": "text", "part": {"id": "p1", "type": "text", "text": "{\"a\": 1}"}}),
        "not json",
    ])
    assert opencode_text(out) == '{"a": 1}'
    limit = json.dumps({"type": "error", "error": {"type": "provider.free-limit",
                                                   "message": "Free usage limit reached."}})
    with pytest.raises(ModelProviderError, match="Free usage limit"):
        opencode_text(limit)


def test_opencode_models_use_the_cli(monkeypatch):
    monkeypatch.delenv("FACTORY_MODEL_URL", raising=False)
    monkeypatch.delenv("FACTORY_MODEL_BACKEND", raising=False)
    assert uses_opencode_cli("opencode/beam-free")
    assert not uses_opencode_cli("gpt-4o")
    monkeypatch.setenv("FACTORY_MODEL_URL", "https://opencode.ai/zen/v1")
    assert not uses_opencode_cli("opencode/beam-free")
    monkeypatch.setenv("FACTORY_MODEL_BACKEND", "opencode")
    assert uses_opencode_cli("anything/else")


def test_opencode_cli_runs_in_an_empty_folder(monkeypatch, tmp_path):
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\n"
                    "[ -z \"$(ls -A .)\" ] || { echo '{\"type\":\"error\",\"error\":{\"message\":\"cwd not empty\"}}'; exit 0; }\n"
                    "read -r line\n"
                    "printf '{\"type\":\"text\",\"part\":{\"id\":\"x\",\"type\":\"text\",\"text\":\"echo:%s %s\"}}\\n' \"$3\" \"$line\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(fake))
    monkeypatch.delenv("FACTORY_MODEL_URL", raising=False)
    client = ModelClient("opencode/hy4-flash-free")
    assert client.reply([{"role": "user", "content": "hello"}]) == "echo:opencode/hy4-flash-free hello"


# --------------------------------------------------------------------------- edits and context


def test_apply_edits(tmp_path):
    (tmp_path / "m.py").write_text("a = 1\nb = 2\nb = 2\n")
    assert apply_edits(tmp_path, [{"path": "m.py", "search": "a = 1\n", "replace": "a = 3\n"}],
                       {"n.py": "x\n"}) == {"m.py": "a = 3\nb = 2\nb = 2\n", "n.py": "x\n"}
    with pytest.raises(PackageError, match="found 2 times"):
        apply_edits(tmp_path, [{"path": "m.py", "search": "b = 2\n", "replace": ""}])
    with pytest.raises(PackageError, match="not found"):
        apply_edits(tmp_path, [{"path": "m.py", "search": "zzz", "replace": ""}])
    with pytest.raises(PackageError, match="does not exist"):
        apply_edits(tmp_path, [{"path": "x.py", "search": "a", "replace": ""}])
    with pytest.raises(PackageError, match="already exists"):
        apply_edits(tmp_path, [], {"m.py": "x"})
    with pytest.raises(PackageError, match="Invalid repository path"):
        apply_edits(tmp_path, [{"path": "../m.py", "search": "a", "replace": ""}])


def test_big_files_get_an_outline(tmp_path):
    big = "def keep(x):\n" + "    pass\n" * 30_000 + "class Later:\n    def method(self, y):\n        pass\n"
    (tmp_path / "big.py").write_text(big)
    (tmp_path / "small.py").write_text("x = 1\n")
    ctx = repo_context(tmp_path, ["big.py", "small.py"], budget=50_000)
    assert ctx["files"] == {"small.py": "x = 1\n"}
    assert "def keep(x):" in ctx["outlines"]["big.py"] and "def method(self, y):" in ctx["outlines"]["big.py"]
    assert "30002" in outline(big)  # line numbers let the model find its place


# --------------------------------------------------------------------------- ideation


def test_ideas_are_checked_and_kept_distinct(py_repo):
    sources = ideas_mod.source_files(py_repo)
    assert "toyshop/pricing.py" in sources and not any("test" in s for s in sources)
    kept, why = [], []
    for raw in IDEAS["ideas"]:
        idea, reason = check_idea(raw, py_repo, "python", sources)
        (kept if idea else why).append(idea or reason)
    assert [i.id for i in kept] == ["bulk-discount", "bulk-discount-2"]
    assert "bug fixes are out of scope" in why and any("no existing source file" in w for w in why)
    assert [i.id for i in select_distinct(kept, 5)] == ["bulk-discount"]  # the near copy is dropped
    clash, reason = check_idea(dict(IDEAS["ideas"][0], new_names=["line_total"]), py_repo, "python", sources)
    assert clash is None and "already exists" in reason


def test_select_distinct_spreads_categories():
    mk = lambda i, words, cat, f: Idea(id=i, title=words, summary="", category=cat, files=[f])
    pool = [mk("a", "colour output", "x", "f1"), mk("b", "prompt timeout", "x", "f2"),
            mk("c", "envvar prefix", "y", "f1"), mk("d", "range clamping", "z", "f3")]
    assert [i.id for i in select_distinct(pool, 3)] == ["a", "d", "c"]  # new category and new file first


# --------------------------------------------------------------------------- produce, end to end


def _scripted(replies):
    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        return replies.pop(0)
    return reply, prompts


def test_produce_repairs_a_rejected_draft(py_repo, tmp_path, monkeypatch, capsys):
    workdir = tmp_path / "workdir"
    options = ["--workdir", str(workdir), "--backend", "local"]
    assert main(["load", "--repo", str(py_repo), *options]) == 0
    assert main(["inspect", *options]) == 0

    replies = [
        json.dumps(IDEAS),
        json.dumps({"title": "Bulk discount on cart lines", "requirements": REQUIREMENTS, "edits": BAD_EDITS}),
        json.dumps({"tests": {"tests/test_bulk_discount.py": HIDDEN}}),
        # repair: the model gets the gate verdict and returns a corrected draft
        json.dumps({"title": "Bulk discount on cart lines", "requirements": REQUIREMENTS, "edits": GOOD_EDITS,
                    "tests": {"tests/test_bulk_discount.py": HIDDEN}}),
    ]
    reply, prompts = _scripted(replies)
    monkeypatch.setattr(ModelClient, "reply", lambda self, messages: reply(messages[-1]["content"]))
    code = main(["produce", "--count", "1", "--model", "test/model", "--harbor", str(tmp_path / "harbor"),
                 *options])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "1/1 ideas became valid tasks" in out
    assert "attempt 1: FEATURE_TESTS_FAIL_WITH_PATCH" in out
    assert "FEATURE_TESTS_FAIL_WITH_PATCH" in prompts[3] and "Test output" in prompts[3]
    assert REQUIREMENTS in prompts[2] and GOOD_EDITS[0]["replace"] not in prompts[2]  # tests never see the solution

    (task,) = (workdir / "tasks").glob("toyshop-*")
    meta = tomllib.loads((task / "task.toml").read_text())
    assert meta["generated_by"]["model"] == "test/model" and meta["idea_id"] == "bulk-discount"
    assert meta["generated_by"]["repair_attempts"][0]["reject_code"] == "FEATURE_TESTS_FAIL_WITH_PATCH"
    record = json.loads(next((workdir / "produce").glob("*.json")).read_text())
    assert record["valid"] == 1 and record["outcomes"][0]["attempts"] == 2

    (harbor,) = (tmp_path / "harbor").iterdir()
    toml = tomllib.loads((harbor / "task.toml").read_text())
    assert toml["base_commit"] == meta["commit_sha"] and len(toml["base_commit"]) == 40
    assert toml["verifier"]["network_mode"] == "no-network"
    assert json.loads((harbor / "tests/fail_to_pass.json").read_text()) == meta["fail_to_pass"]
    assert json.loads((harbor / "tests/pass_to_pass.json").read_text()) == meta["pass_to_pass"]

    # a second run does not rebuild the idea that already has a task
    reply2, _ = _scripted([json.dumps({"ideas": []})])
    monkeypatch.setattr(ModelClient, "reply", lambda self, messages: reply2(messages[-1]["content"]))
    assert main(["produce", "--count", "1", "--model", "test/model", *options]) == 4


def test_produce_stops_when_the_provider_fails(py_repo, tmp_path, monkeypatch, capsys):
    workdir = tmp_path / "workdir"
    options = ["--workdir", str(workdir), "--backend", "local"]
    assert main(["load", "--repo", str(py_repo), *options]) == 0
    assert main(["inspect", *options]) == 0
    calls = []

    def reply(self, messages):
        calls.append(self.model)
        if self.model == "first/model":
            raise ModelProviderError("opencode: Free usage limit reached.")
        if len(calls) == 2:
            return json.dumps(IDEAS)
        raise ModelProviderError("opencode: Free usage limit reached.")

    monkeypatch.setattr(ModelClient, "reply", reply)
    code = main(["produce", "--count", "2", "--model", "first/model", "--model", "second/model", *options])
    out = capsys.readouterr().out
    assert code == 4
    assert calls[:2] == ["first/model", "second/model"]  # the second model took over
    assert "error: opencode: Free usage limit reached." in out
    assert len(calls) == 3  # it stopped instead of trying every idea against an exhausted quota


def test_detect_license(tmp_path):
    from factory.packaging.harbor import detect_license

    (tmp_path / "LICENSE.txt").write_text(
        "Copyright 2014 Pallets\n\nRedistribution and use in source and binary forms, with or without "
        "modification, are permitted...\n3. Neither the name of the copyright holder nor ...")
    assert detect_license(tmp_path) == ("BSD-3-Clause", "LICENSE.txt")


def test_ideas_already_in_a_newer_upstream_version_are_dropped(py_repo):
    from helpers import git

    base = git(py_repo, "rev-parse", "HEAD")
    git(py_repo, "checkout", "-q", "-B", "main")
    (py_repo / "toyshop" / "pricing.py").write_text(
        (py_repo / "toyshop" / "pricing.py").read_text() + "\nBULK_MIN_QTY = 10\n")
    git(py_repo, "commit", "-qam", "newer upstream release")
    git(py_repo, "checkout", "-q", "--detach", base)
    refs = ideas_mod.newest_refs(py_repo, base)
    assert len(refs) == 1 and refs[0] != base
    sources = ideas_mod.source_files(py_repo)
    idea, why = check_idea(IDEAS["ideas"][0], py_repo, "python", sources, py_repo, refs)
    assert idea is None and "newer upstream version" in why
    assert check_idea(dict(IDEAS["ideas"][0], new_names=["BOGO_SKUS"]), py_repo, "python", sources,
                      py_repo, refs)[0] is not None


def test_produce_writes_its_record_when_ideation_fails(py_repo, tmp_path, monkeypatch, capsys):
    workdir = tmp_path / "workdir"
    options = ["--workdir", str(workdir), "--backend", "local"]
    assert main(["load", "--repo", str(py_repo), *options]) == 0
    assert main(["inspect", *options]) == 0

    def reply(self, messages):
        raise ModelProviderError("opencode: Free usage limit reached.")

    monkeypatch.setattr(ModelClient, "reply", reply)
    assert main(["produce", "--count", "5", "--model", "opencode/beam-free", *options]) == 4
    assert "error    ideation: opencode: Free usage limit reached." in capsys.readouterr().out
    record = json.loads(next((workdir / "produce").glob("*.json")).read_text())
    assert record["valid"] == 0 and record["errors"] == ["ideation: opencode: Free usage limit reached."]


def test_harbor_check_reports_the_matrix(tmp_path, monkeypatch, capsys):
    for name in ("good", "bad"):
        (tmp_path / "tasks" / name).mkdir(parents=True)
        (tmp_path / "tasks" / name / "task.toml").write_text('schema_version = "1.4"\n')
    fake = tmp_path / "harbor"
    # writes reward 0 for nop, 1 for oracle, except the "bad" task's oracle gets 0
    fake.write_text('#!/bin/sh\n[ "$1" = "--version" ] && { echo 0.25.0; exit 0; }\n'
                    'task=$(basename "$3"); agent=$5; out=$9\n'
                    'mkdir -p "$out/job/trial/verifier"\n'
                    'r=0; [ "$agent" = oracle ] && [ "$task" = good ] && r=1.0\n'
                    'echo $r > "$out/job/trial/verifier/reward.txt"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("HARBOR_BIN", str(fake))
    assert main(["harbor-check", str(tmp_path / "tasks"), "--out", str(tmp_path / "out")]) == 4
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["harbor_version"] == "0.25.0" and summary["passed"] == 1
    assert [r["ok"] for r in summary["rows"]] == [False, True]  # bad, good
    assert "| `good` | 0.0 | 1.0 | pass |" in (tmp_path / "out" / "summary.md").read_text()


def test_requirements_must_not_leak_tests():
    from factory.generation.pipeline import check_requirements

    tests = {"tests/test_bulk_discount.py": "def test_bulk_discount_boundary():\n    pass\n"}
    check_requirements(REQUIREMENTS, tests)
    with pytest.raises(PackageError, match="too short"):
        check_requirements("# Title\n\nAdd a discount.", tests)
    with pytest.raises(PackageError, match="hidden test file"):
        check_requirements(REQUIREMENTS + "\nSee test_bulk_discount.py.", tests)
    with pytest.raises(PackageError, match="hidden test 'test_bulk_discount_boundary'"):
        check_requirements(REQUIREMENTS + "\nMake test_bulk_discount_boundary pass.", tests)
