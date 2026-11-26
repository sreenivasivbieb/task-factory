"""Evidence report and pipeline config: pure file processing, no Docker."""

import json
import shutil
from pathlib import Path

import pytest

from factory import evidence
from factory.pipeline import PipelineConfig

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = sorted(p for p in (ROOT / "submission").iterdir() if (p / "task.toml").exists())


def _attempt(task, agent, resolved, category, model=None, attempt=1, f2p=(3, 3), tampered=()):
    return {"task_id": task, "agent": agent, "model": model, "attempt": attempt, "resolved": resolved,
            "category": category, "f2p_passed": f2p[0], "f2p_total": f2p[1], "p2p_failed": [],
            "tests_tampered": list(tampered), "agent_duration_s": 1.0, "cost_usd": 0.01 if model else None}


@pytest.fixture
def bundle(tmp_path):
    tasks = tmp_path / "tasks"
    for p in PACKAGES:
        shutil.copytree(p, tasks / p.name)
    ids = [p.name for p in PACKAGES]
    runs = tmp_path / "harness"
    rows = {
        "r1-oracle": [_attempt(t, "oracle", True, "resolved") for t in ids],
        "r2-noop": [_attempt(t, "noop", False, "no_change", f2p=(0, 3)) for t in ids],
        "r3-model": [_attempt(ids[0], "task-factory", True, "resolved", "m/a"),
                     _attempt(ids[1], "task-factory", False, "wrong_behaviour", "m/a", f2p=(1, 3),
                              tampered=["tests/test_x.py"]),
                     _attempt(ids[1], "task-factory", False, "agent_error", "m/b")],
    }
    for name, rs in rows.items():
        (runs / name).mkdir(parents=True)
        (runs / name / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs))
    selftest = tmp_path / "selftest.json"
    selftest.write_text(json.dumps([{"fixture": "bad_x", "expected": "FLAKY", "got": "FLAKY", "ok": True}]))
    return tmp_path, ids


def test_build_writes_every_artifact(bundle):
    tmp, ids = bundle
    out = tmp / "out"
    s = evidence.build([tmp / "tasks"], [tmp / "harness"], out, selftests=[tmp / "selftest.json"])
    for f in ["REPORT.md", "evidence/validation_matrix.csv", "evidence/summary.json", "diversity/stats.json",
              "benchmark/leaderboard.csv", "benchmark/per_task.csv", "benchmark/failure_modes.json"]:
        assert (out / f).exists(), f
    assert s["tasks"] == len(ids)
    assert s["oracle_resolved"] == 1.0 and s["noop_resolved"] == 0.0
    assert s["selftest_pass"] == s["selftest_total"] == 1
    assert s["models_benchmarked"] == 2  # m/a and m/b (the latter only had an infrastructure error)
    report = (out / "REPORT.md").read_text(encoding="utf-8")
    assert "## 1. Validity" in report and "## 3. Benchmark" in report


def test_leaderboard_excludes_agent_errors_and_counts_tampering(bundle):
    tmp, ids = bundle
    board = {b["model"]: b for b in evidence.leaderboard(evidence.load_results([tmp / "harness"]))}
    a = board["task-factory:m/a"]
    assert (a["tasks"], a["resolved_at_k"], a["pass_at_1"]) == (2, 0.5, 0.5)
    assert a["mean_hidden_tests_passed"] == round((1 + 1 / 3) / 2, 3)
    assert a["tampered_with_tests"] == 1
    assert board["task-factory:m/b"]["tasks"] == 0 and board["task-factory:m/b"]["agent_errors"] == 1
    assert board["oracle"]["sanity"] and board["oracle"]["resolved_at_k"] == 1.0


def test_failure_modes_split_partial_from_none(bundle):
    tmp, _ = bundle
    modes = evidence.failure_modes(evidence.load_results([tmp / "harness"]))
    assert modes == {"task-factory:m/a": {"wrong_behaviour: partial": 1,
                                          "edited the tests (restored before grading)": 1}}


def test_tiers_and_near_duplicates():
    assert evidence.tier_of(1, 10, 3) == "S"
    assert evidence.tier_of(2, 10, 3) == "M"
    assert evidence.tier_of(1, 200, 3) == "L"
    rows = [evidence.read_task(p) for p in PACKAGES]
    rows[1].instruction = rows[0].instruction
    div = evidence.diversity(rows)
    assert div["instruction_similarity"]["near_duplicates"][0]["jaccard"] == 1.0


def test_pipeline_configs_parse():
    for name in ("hackathon.toml", "dev-local.toml"):
        pc = PipelineConfig.load(ROOT / "configs" / name)
        assert pc.repos and all(r.drafts for r in pc.repos)
    with pytest.raises(ValueError):
        PipelineConfig.model_validate({"repos": [], "typo": 1})
