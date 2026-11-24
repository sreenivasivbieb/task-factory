"""Records in MongoDB next to the files (factory/records.py): off without a URI, idempotent sync,
live writes from the review store, and a dead server never stops the pipeline."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from factory import records as records_mod
from factory.records import Records, sync_workdir, workdir_key

TOML = """schema_version = 1
task_id = "{tid}"
repo = "file:///x"
repo_name = "x"
commit_sha = "{sha}"
test_command = "pytest {{select}}"
kind = "feature"
idea_id = "i"
fail_to_pass = ["t::a"]
pass_to_pass = ["t::b", "t::c"]

[environment]
base_image = "python:3.11-slim"
test_command = "pytest {{select}}"
framework = "pytest"
report_format = "junit"

[validation]
verdict = "valid"
"""


def _workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "wd"
    pkg = wd / "tasks" / "x-0000000-one-001"
    pkg.mkdir(parents=True)
    (pkg / "task.toml").write_text(TOML.format(tid=pkg.name, sha="0" * 40), encoding="utf-8")
    (wd / "tasks" / ".review").mkdir()
    (wd / "tasks" / ".review" / "log.jsonl").write_text(
        json.dumps({"task_id": pkg.name, "action": "approve", "reviewer": "me"}) + "\n", encoding="utf-8")
    run = wd / "harness" / "20261124-120000-oracle"
    run.mkdir(parents=True)
    (run / "results.jsonl").write_text(json.dumps({"task_id": pkg.name, "attempt": 1, "resolved": True}) + "\n"
                                       + "{half-written\n", encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"agent": "oracle", "resolve_rate": 1.0}), encoding="utf-8")
    (wd / "session-runs.jsonl").write_text(json.dumps({"session_id": "s", "run_id": run.name}) + "\n", encoding="utf-8")
    (wd / "prs").mkdir()
    (wd / "prs" / "7.json").write_text(json.dumps({"pr": 7, "status": "task"}), encoding="utf-8")
    return wd


def test_records_are_off_without_a_uri(tmp_path):
    rec = Records("")
    assert not rec.enabled
    assert rec.put("tasks", {"_id": "a"}) is False
    assert sync_workdir(_workdir(tmp_path), rec) == {k: 0 for k in sync_workdir(_workdir(tmp_path / "2"), rec)}
    with pytest.raises(ValueError):
        rec.put("nope", {"_id": "a"})


def test_a_dead_server_switches_records_off_and_never_raises():
    rec = Records("mongodb://127.0.0.1:1/task_factory")
    start = time.monotonic()
    assert rec.put("tasks", {"_id": "a"}) is False and not rec.enabled
    assert rec.put("tasks", {"_id": "b"}) is False  # no second wait
    assert time.monotonic() - start < 15


@pytest.fixture(scope="module")
def mongo_uri():
    r = subprocess.run(["docker", "run", "-d", "--rm", "-p", "127.0.0.1::27017", "mongo:8.0"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot start mongo:8.0: {r.stderr.strip()[:200]}")
    cid = r.stdout.strip()
    try:
        port = subprocess.run(["docker", "port", cid, "27017"], capture_output=True, text=True).stdout.split(":")[-1].strip()
        uri = f"mongodb://127.0.0.1:{port}/tf_test"
        for _ in range(60):
            if Records(uri).put("runs", {"_id": "ping"}):
                break
            time.sleep(1)
        yield uri
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


@pytest.mark.docker
def test_sync_writes_every_record_once(tmp_path, mongo_uri):
    wd = _workdir(tmp_path)
    rec = Records(mongo_uri)
    first = sync_workdir(wd, rec)
    assert first == {"tasks": 1, "review_events": 1, "attempts": 1, "runs": 1, "session_runs": 1, "swe_prs": 1}
    sync_workdir(wd, rec)  # again: same _ids, no duplicates
    db = rec.db()
    task = db.tasks.find_one({"_id": f"{workdir_key(wd / 'tasks')}:x-0000000-one-001"})
    assert task["status"] == "approved" and task["fail_to_pass"] == 1 and task["source"] == "gated"
    assert db.attempts.count_documents({"workdir": str(wd)}) == 1
    assert db.review_events.count_documents({"store": str(wd / "tasks")}) == 1


@pytest.mark.docker
def test_review_decisions_are_written_live(tmp_path, mongo_uri, monkeypatch):
    from factory.review.store import TaskStore

    wd = _workdir(tmp_path)
    rec = Records(mongo_uri)
    monkeypatch.setattr(records_mod, "records", rec)
    sync_workdir(wd, rec)
    TaskStore(wd / "tasks", reviewer="me").delete("x-0000000-one-001", "too easy")
    db = rec.db()
    assert db.review_events.count_documents({"store": str(wd / "tasks"), "action": "delete"}) == 1
    assert db.tasks.find_one({"task_id": "x-0000000-one-001", "store": str(wd / "tasks")})["status"] == "deleted"
