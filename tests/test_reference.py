"""Reference-guided pipeline (v2.2): N0 state machine + store, N1 novelty, N2 Harbor wrapper,
N3-N9 research, drafting, building, benchmark and the controller, all with fakes (no web, no LLM)."""
import json
import re
import shutil
import time
from pathlib import Path

import mongomock
import pytest

from factory.config import FactoryConfig
from factory.models import EnvConfig, GateResult, RejectCode, ValidationReport
from factory.reference import drafting
from factory.reference.agents import AgentPool, SubAgent, family
from factory.reference.benchmark import FakeBackend, Worker, dir_checksum, enqueue, summary
from factory.reference.building import Builder, task_id_for
from factory.reference.github import GitHub, parse_repo
from factory.reference.harbor import HarborRun, HarborValidator, format_problems
from factory.reference.miner import Miner, added_names, for_author, for_test_writer
from factory.reference.profiler import Profiler, spot_check
from factory.reference.report import build_report, render_markdown
from factory.reference.run import Controller, RunOptions, run_id_for
from factory.reference.schema import (Capability, JobState, PeerFeature, PeerRepo,
                                      ProposalState as S, Provenance, PullRef, ReferenceBundle, RepoProfile,
                                      TaskProposal, outcome_of)
from factory.reference.scout import Scout, ScoutConfig, compare
from factory.reference.snapshot import ReplayMiss, Snapshot
from factory.reference.states import TRANSITIONS, TransitionError, next_state
from factory.reference.store import ReferenceStore
from factory.reference.text import code_similarity, match_score
from helpers import PY_REPO, git, make_git_repo


@pytest.fixture
def store():
    return ReferenceStore(mongomock.MongoClient().db)


def proposal(pid="p1", state=S.DRAFTED, **kw) -> TaskProposal:
    data = dict(proposal_id=pid, run_id="r1", title="Bulk discount for large cart lines", state=state,
                summary="Lines with 10 or more items get 10 percent off.",
                behaviour_spec="x" * 250, expected_files=["toyshop/pricing.py"],
                test_plan=["10 items get the discount", "9 items do not", "the discount rounds half up"],
                provenance=[Provenance(feature_id="f1", peer_repo="acme/shop", pr_url="https://github.com/acme/shop/pull/7",
                                       licence="MIT")])
    data.update(kw)
    return TaskProposal(**data)


# =========================================================================== N0 · states and store


def test_main_path_and_failure_paths():
    path = ["critics_pass", "auto_approve", "build", "repair", "validated", "enqueue", "lease", "finished"]
    s = S.DRAFTED
    for e in path:
        s = next_state(s, e)
    assert s == S.BENCHMARKED
    assert next_state(S.DRAFTED, "critic_objection") == S.REJECTED
    assert next_state(S.CHECKED, "near_duplicate") == S.MERGED
    assert next_state(S.PENDING_REVIEW, "user_edit") == S.DRAFTED
    assert next_state(S.PENDING_REVIEW, "user_reject") == S.REJECTED
    assert next_state(S.BUILDING, "repairs_exhausted") == S.DROPPED
    assert next_state(S.RUNNING, "infra_retry") == S.QUEUED
    assert next_state(S.RUNNING, "retries_exhausted") == S.RUN_FAILED
    assert next_state(S.BENCHMARKED, "package_edited") == S.QUEUED
    assert next_state(S.VALIDATED, "proposal_edited") == S.DRAFTED
    for bad in [(S.DRAFTED, "auto_approve"), (S.REJECTED, "critics_pass"), (S.DROPPED, "proposal_edited"),
                (S.CHECKED, "build"), (S.MERGED, "enqueue")]:
        with pytest.raises(TransitionError):
            next_state(*bad)
    assert all(isinstance(v, S) for v in TRANSITIONS.values())


def test_store_transitions_are_compare_and_set(store):
    store.add_proposal(proposal())
    p = store.transition("p1", "critics_pass", "ok")
    assert p.state == S.CHECKED and p.history[-1]["event"] == "critics_pass"
    with pytest.raises(TransitionError):
        store.transition("p1", "critics_pass")
    assert store.try_transition("p1", "build") is None
    again = store.add_proposal(proposal())  # replay: the existing proposal stays
    assert again.state == S.CHECKED
    with pytest.raises(TransitionError):
        store.update_proposal("p1", state="VALIDATED")


def test_edit_is_logged_goes_back_to_drafted_and_invalidates_jobs(store, tmp_path):
    store.add_proposal(proposal())
    for e in ("critics_pass", "request_review"):
        store.transition("p1", e)
    store.update_proposal("p1", task_id="t1")
    task = tmp_path / "t1"
    task.mkdir()
    (task / "a").write_text("1")
    enqueue(store, "r1", "t1", task, ["oracle"])
    with pytest.raises(TransitionError):
        store.edit_proposal("p1", {"title": "x"}, "user:me", "")
    with pytest.raises(TransitionError):
        store.edit_proposal("p1", {"state": "APPROVED"}, "user:me", "nope")
    p = store.edit_proposal("p1", {"title": "Bulk discount from 12 items"}, "user:me", "clearer threshold")
    assert p.state == S.DRAFTED and p.edits[0].reason == "clearer threshold" and p.task_id is None
    assert store.list_jobs("r1")[0].state == JobState.INVALIDATED


def _validated(store, pid="p1", task_id="t1"):
    store.add_proposal(proposal(pid))
    for e in ("critics_pass", "auto_approve", "build", "validated"):
        store.transition(pid, e)
    store.update_proposal(pid, task_id=task_id)


def test_queue_lease_heartbeat_resume_and_retry(store, tmp_path):
    _validated(store)
    task = tmp_path / "t1"
    task.mkdir()
    (task / "instruction.md").write_text("# x")
    ids = enqueue(store, "r1", "t1", task, ["oracle", "nop"], proposal_id="p1")
    assert len(ids) == 2 and enqueue(store, "r1", "t1", task, ["oracle", "nop"], proposal_id="p1") == []
    assert store.get_proposal("p1").state == S.QUEUED
    job = store.lease("w1", lease_s=60)
    assert job.state == JobState.RUNNING and job.tries == 1 and store.get_proposal("p1").state == S.RUNNING
    assert store.heartbeat(job.job_id, "w1", 60) and not store.heartbeat(job.job_id, "other", 60)
    # the worker dies: once its lease runs out the next worker resumes the same job
    other = store.lease("w2", lease_s=60, now=time.time() + 1)
    assert other.job_id != job.job_id  # the second job first
    resumed = store.lease("w3", lease_s=60, now=time.time() + 120)
    assert resumed.job_id == job.job_id and resumed.worker == "w3" and resumed.tries == 2


def test_infra_errors_retry_with_backoff_then_run_failed(store, tmp_path):
    _validated(store)
    task = tmp_path / "t1"
    task.mkdir()
    (task / "x").write_text("1")
    from factory.reference.schema import JobBudget
    enqueue(store, "r1", "t1", task, ["crash"], proposal_id="p1", budget=JobBudget(max_retries=1))
    job = store.lease("w")
    assert store.infra_error(job.job_id, "boom", backoff_s=100) == JobState.QUEUED
    assert store.lease("w") is None  # backing off
    job = store.lease("w", now=time.time() + 1000)
    assert store.infra_error(job.job_id, "boom again") == JobState.RUN_FAILED
    assert store.get_proposal("p1").state == S.RUN_FAILED


def test_kill_switch(store):
    assert not store.killed()
    store.set_kill(True)
    assert store.killed()


# =========================================================================== N1 · novelty


def _feature(fid="f1", text="", names=(), cap=None) -> PeerFeature:
    return PeerFeature(id=fid, peer_repo="acme/shop", pr_url="https://github.com/acme/shop/pull/1", summary=text,
                       behaviour_pattern=text, gap_capability=cap, new_names=list(names), snapshot_id="s",
                       reference_bundle=ReferenceBundle(pr=PullRef(number=1, title=text, url="u")))


@pytest.fixture
def toy_profile():
    return RepoProfile(repo="toyshop", language="python", capability_map=[
        Capability(name="Coupon discounts", behaviour="apply a coupon code to the cart total for a percentage discount",
                   evidence=["toyshop/pricing.py:coupon_discount"]),
        Capability(name="Catalog search", behaviour="search products by text and filter by tag",
                   evidence=["toyshop/catalog.py:search"])])


def _harbor_task(root: Path, base="a" * 40, test_text="def test_x():\n    assert 1\n") -> Path:
    t = root / "toyshop-abc-task"
    for d in ("environment", "solution", "tests/files/tests"):
        (t / d).mkdir(parents=True, exist_ok=True)
    (t / "instruction.md").write_text("# Task\n")
    (t / "task.toml").write_text(f'base_commit = "{base}"\n[metadata]\nbase_commit = "{base}"\n')
    (t / "environment" / "Dockerfile").write_text("FROM python:3.12\n")
    (t / "solution" / "solve.sh").write_text("#!/bin/bash\n")
    (t / "tests" / "test.sh").write_text("#!/bin/bash\n")
    (t / "tests" / "fail_to_pass.json").write_text('["tests/test_x.py::test_x"]')
    (t / "tests" / "pass_to_pass.json").write_text("[]")
    (t / "tests" / "files" / "tests" / "test_x.py").write_text(test_text)
    return t


def test_format_checks(tmp_path):
    t = _harbor_task(tmp_path / "ok")
    assert format_problems(t) == []
    bad = _harbor_task(tmp_path / "bad", base="abc123",
                       test_text="import requests\nrequests.get('https://api.example.net')\nos.environ['OPENAI_API_KEY']\n")
    (bad / "provenance.json").write_text("{}")
    (bad / "tests" / "fail_to_pass.json").write_text("[]")
    probs = " | ".join(format_problems(bad))
    for want in ("extra entries", "40-character", "fail_to_pass.json is empty", "reaches the network", "credentials"):
        assert want in probs


def _runner(rewards: dict):
    seq = {k: list(v) if isinstance(v, list) else [v] for k, v in rewards.items()}

    def run(task, agent, out, model=None, timeout_s=0):
        vals = seq[agent]
        r = vals.pop(0) if len(vals) > 1 else vals[0]
        return HarborRun(agent, r, 0, 0.1, str(out), f"harbor run -a {agent}")
    return run


def test_harbor_validator_nop_oracle_stability_and_infra(tmp_path):
    t = _harbor_task(tmp_path)
    ok = HarborValidator(tmp_path / "o1", repeats=2, runner=_runner({"nop": 0.0, "oracle": 1.0})).validate(t)
    assert ok.ok and ok.stable and ok.reason() == "ok"
    leak = HarborValidator(tmp_path / "o2", runner=_runner({"nop": 1.0, "oracle": 1.0})).validate(t)
    assert not leak.ok and "nop reward 1.0" in leak.reason()
    flaky = HarborValidator(tmp_path / "o3", repeats=2, runner=_runner({"nop": 0.0, "oracle": [1.0, 0.5]})).validate(t)
    assert not flaky.ok and not flaky.stable
    retried = HarborValidator(tmp_path / "o4", retries=1, runner=_runner({"nop": [None, 0.0], "oracle": 1.0})).validate(t)
    assert retried.ok
    crash = HarborValidator(tmp_path / "o5", retries=1, runner=_runner({"nop": None, "oracle": 1.0})).validate(t)
    assert not crash.ok and crash.infra_error
    assert [outcome_of(x) for x in (1.0, 0.5, 0.0, None)] == ["solved", "partial", "failed", "infra_error"]


# =========================================================================== fakes for the LLM and GitHub


class FakeLLM:
    """Answers each role's prompt with canned JSON; records every prompt per role."""

    def __init__(self):
        self.prompts: dict[str, list[str]] = {}

    def __call__(self, model):
        def reply(prompt: str) -> str:
            role = self.role(prompt)
            self.prompts.setdefault(role, []).append(prompt)
            return json.dumps(getattr(self, role)(prompt))
        return reply

    @staticmethod
    def role(prompt):
        for key, role in (("You classify candidate peer", "classify"), ("You are the research agent", "researcher"),
                          ("You are an independent critic for a coding-task", "idea_critic"),
                          ("You write the task proposal for an accepted idea", "spec"),
                          ("You are the Profiler", "profiler"),
                          ("You are the Scout", "scout"),
                          ("You are the Miner", "miner"), ("You check whether", "checker"),
                          ("You are a Drafter", "drafter"), ("independent Critic", "critic"),
                          ("You are the Author", "author"), ("You are the Test writer", "test_writer"),
                          ("You review a draft", "draft_review"), ("You review hidden tests", "tests_review")):
            if key in prompt:
                return role
        raise AssertionError(prompt[:200])

    def profiler(self, prompt):
        if "giftshop" in prompt:  # the peer
            return {"purpose": "A gift shop cart.", "capability_map": [
                {"name": "Coupon discounts", "behaviour": "apply a coupon code to the cart total for a percentage discount",
                 "evidence": ["giftshop/pricing.py:coupon_discount"]},
                {"name": "Bulk line discount", "behaviour": "lines with many items get a quantity discount",
                 "evidence": ["giftshop/pricing.py:bulk"]},
                {"name": "Gift wrapping", "behaviour": "wrap an order as a gift for a fixed fee",
                 "evidence": ["giftshop/wrap.py:wrap"]}], "design_traits": ["small pure functions in modules"],
                "boundaries": ["no payments"]}
        return {"purpose": "A toy shop cart.", "capability_map": [
            {"name": "Coupon discounts", "behaviour": "apply a coupon code to the cart total for a percentage discount",
             "evidence": ["toyshop/pricing.py:coupon_discount"]},
            {"name": "Catalog search", "behaviour": "search products by text", "evidence": ["toyshop/catalog.py:search"]},
            {"name": "Ghost", "behaviour": "does not exist", "evidence": ["toyshop/ghost.py:nothing"]}],
            "design_traits": ["small pure functions in modules"], "boundaries": ["no payments"]}

    def scout(self, prompt):
        assert "def " not in prompt  # privacy: only capability descriptions leave the machine
        return {"queries": ["shopping cart discounts"], "alternatives": ["acme/giftshop", "big/gpl-shop"]}

    IDEA = {"title": "Bulk discount for large cart lines", "kind": "feature",
            "summary": "Cart lines with 10 or more items get 10 percent off.",
            "behaviour": "line_total applies 10 % off when qty >= 10", "files": ["toyshop/pricing.py"],
            "where": "toyshop.pricing.line_total", "references": [{"peer": "acme/giftshop", "pr": 7}],
            "why_fits": "pricing rules live in pricing.py",
            "how_to_test": ["10 items get 10 % off", "9 items pay full price", "rounding is half up"]}

    def researcher(self, prompt):
        if "asked for changes" in prompt:
            return {"final": {"idea": dict(self.IDEA, summary="Cart lines with 12 or more items get 10 percent off.")}}
        if "Your steps so far" not in prompt:  # look before deciding
            return {"tool": "repo_search", "args": {"pattern": "def line_total"}, "why": "where pricing lives"}
        dup = dict(self.IDEA, title="Bulk discount for large cart lines (again)")
        return {"final": {"ideas": [self.IDEA, dup]}}

    def idea_critic(self, prompt):
        if "Your steps so far" not in prompt:
            return {"tool": "repo_read", "args": {"path": "toyshop/pricing.py", "start": 1, "end": 40}}
        return {"final": {"verdict": "accept", "already_exists": False, "evidence": ["toyshop/pricing.py:10"],
                          "problems": [], "edge_cases": ["exactly 10 items"]}}

    def spec(self, prompt):
        return {"final": self.drafter(prompt)}

    def brainstorm(self, prompt):
        return {"ideas": [{"capability": "Gift wrapping", "summary": "Orders can be gift wrapped for a fee.",
                           "behaviour_pattern": "a cart can be marked as a gift and pays a wrapping fee",
                           "why": "self-contained pricing rule"}, {"capability": "Not in the gap list"}]}

    def classify(self, prompt):
        return {"repos": {"acme/giftshop": {"role": "alternative", "context": 0.9, "why": "another shop cart library"},
                          "big/gpl-shop": "alternative"}}  # both reply shapes models use

    def miner(self, prompt):
        return {"is_feature": True, "is_bug_fix": False, "summary": "Adds a bulk discount for large lines.",
                "behaviour_pattern": "cart lines with ten or more items get a bulk quantity discount"}

    def checker(self, prompt):
        return {"exists": False}

    def drafter(self, prompt):
        return {"title": "Bulk discount for large cart lines", "kind": "feature",
                "summary": "Cart lines with 10 or more items get 10 percent off their line total.",
                "behaviour_spec": "`line_total(price_cents, qty)` applies 10 % off when qty >= 10. " * 6,
                "expected_files": ["toyshop/pricing.py"], "new_names": ["BULK_MIN_QTY"],
                "test_plan": ["10 items get 10 % off", "9 items pay full price", "rounding is half up"],
                "edge_cases_from_peer": ["zero quantity"], "difficulty_est": "easy", "category": "validation rule"}

    def critic(self, prompt):
        return {"verdict": "pass", "hard": False, "objections": [], "missing_edge_cases": ["exactly 10 items"]}

    def author(self, prompt):
        return {"title": "Bulk discount", "requirements": "# Bulk discount\n\n" + "Lines of 10 or more get 10 % off. " * 12,
                "edits": [{"path": "toyshop/pricing.py",
                           "search": '    """Total for one cart line."""\n    return price_cents * qty\n',
                           "replace": '    """Total for one cart line."""\n    amount = price_cents * qty\n'
                                      '    if qty >= 10:\n        amount -= (amount * 10 + 50) // 100\n    return amount\n'}]}

    def test_writer(self, prompt):
        path = re.search(r"ONE new file at (\S+)\.", prompt).group(1)
        return {"tests": {path: "from toyshop.pricing import line_total\n\ndef test_bulk():\n    assert line_total(100, 10) == 900\n"}}


def fake_github(peer_repo: Path):
    meta = {"acme/giftshop": {"full_name": "acme/giftshop", "html_url": "https://github.com/acme/giftshop",
                              "license": {"spdx_id": "MIT"}, "language": "Python", "pushed_at": "2026-11-01T00:00:00Z",
                              "stargazers_count": 900, "default_branch": "main", "topics": ["shop"]},
            "big/gpl-shop": {"full_name": "big/gpl-shop", "license": {"spdx_id": "GPL-3.0"}, "language": "Python",
                             "pushed_at": "2026-11-01T00:00:00Z", "stargazers_count": 5000, "default_branch": "main"}}
    pr = {"number": 7, "title": "Add bulk discount for large lines", "body": "Closes #3. Lines with many items get a discount.",
          "html_url": "https://github.com/acme/giftshop/pull/7", "merged_at": "2026-05-01T00:00:00Z",
          "labels": [{"name": "enhancement"}], "merge_commit_sha": "m" * 40, "base": {"sha": "b" * 40}}
    files = [{"filename": "giftshop/pricing.py", "status": "modified",
              "patch": "@@ -1 +1,4 @@\n+def bulk(qty):\n+    return qty >= 10\n"},
             {"filename": "tests/test_pricing.py", "status": "modified", "patch": "@@ -1 +1,2 @@\n+def test_bulk():\n+    assert bulk(10)\n"},
             {"filename": "CHANGES.md", "status": "modified", "patch": "@@ -1 +1 @@\n+- bulk discount\n"}]

    def transport(route):
        if route.startswith("search/repositories"):
            return {"items": [meta["acme/giftshop"], meta["big/gpl-shop"]]}
        if route.startswith("search/issues"):
            from urllib.parse import unquote
            q = unquote(route.split("q=")[1].split("&")[0])
            if "repo:acme/giftshop" in q:  # the follow-up search ("7") finds nothing
                return {"items": [] if q.rstrip().endswith(" 7") else [dict(pr, state="closed")]}
            return {"items": []}
        if route in ("repos/acme/giftshop", "repos/big/gpl-shop"):
            return meta[route[6:]]
        if "/git/trees/" in route:
            return {"tree": [{"type": "blob", "path": p} for p in ("giftshop/pricing.py", "tests/test_pricing.py",
                                                                    ".github/workflows/ci.yml")]}
        if route.startswith("repos/acme/giftshop/pulls?"):
            return []
        if route == "repos/acme/giftshop/pulls/7":
            return pr
        if route.startswith("repos/acme/giftshop/pulls/7/files"):
            return files
        if route.startswith("repos/acme/giftshop/pulls/7/comments"):
            return [{"user": {"login": "maint"}, "body": "What about exactly 10 items?", "path": "giftshop/pricing.py"}]
        if route.startswith("repos/acme/giftshop/issues/7/comments"):
            return [{"user": {"login": "ci[bot]", "type": "Bot"}, "body": "green"}]
        if route == "repos/acme/giftshop/issues/3":
            return {"number": 3, "title": "Bulk discounts please", "body": "Shops need it", "html_url": "i/3"}
        if route.startswith("repos/acme/giftshop/contents/"):
            import base64
            return {"encoding": "base64", "content": base64.b64encode(b"def test_old():\n    pass\n").decode()}
        return None
    return transport


def make_peer_repo(tmp: Path) -> Path:
    peer = tmp / "giftshop"
    (peer / "giftshop").mkdir(parents=True, exist_ok=True)
    (peer / "tests").mkdir(exist_ok=True)
    (peer / "pyproject.toml").write_text('[project]\nname = "giftshop"\ndependencies = ["attrs>=1"]\n')
    (peer / "giftshop" / "pricing.py").write_text("def coupon_discount(x):\n    return x\n\ndef bulk(q):\n    return q\n")
    (peer / "giftshop" / "wrap.py").write_text("def wrap(order):\n    return order\n")
    (peer / "tests" / "test_pricing.py").write_text("def test_bulk():\n    pass\n")
    return peer


# =========================================================================== N3-N5 · research


def test_profiler_cites_files_and_spot_check_catches_bad_citations(tmp_path, py_repo):
    llm = FakeLLM()
    prof = Profiler(SubAgent("profiler", "m", llm("m"))).profile(py_repo, "toyshop", commit="c" * 40)
    assert prof.language == "python" and prof.test_runner == "pytest"
    assert prof.spot_check.checked == 3 and prof.spot_check.passed == 2 and prof.spot_check.failed == ["Ghost"]
    assert [c.verified for c in prof.capability_map] == [True, True, False]
    bad = prof.model_copy(update={"capability_map": [Capability(name="x", behaviour="y", evidence=["toyshop/pricing.py:nope"])]})
    assert spot_check(bad, py_repo).spot_check.failed == ["x"]


def test_scout_cuts_by_licence_and_scores_by_capability(tmp_path, py_repo):
    llm = FakeLLM()
    snap = Snapshot(tmp_path / "snaps", "s1")
    peer_dir = make_peer_repo(tmp_path)
    gh = GitHub(snap, transport=fake_github(peer_dir))
    starter = Profiler(SubAgent("profiler", "m", llm("m"))).profile(py_repo, "toyshop", commit="c" * 40)
    scout = Scout(SubAgent("scout", "m", llm("m"), snap), gh, Profiler(SubAgent("profiler", "m", llm("m"), snap), snap),
                  tmp_path / "clones", ScoutConfig(k=2), cloner=lambda url, dest: (peer_dir, "d" * 40))
    peers, profiles = scout.run(starter)
    by = {p.full_name: p for p in peers}
    assert by["big/gpl-shop"].status == "cut" and "licence GPL-3.0" in by["big/gpl-shop"].reasons[0]
    g = by["acme/giftshop"]
    assert g.status == "selected" and g.similarity.capability_overlap > 0 and g.similarity.total > 0
    assert {c.name for c in g.gap} == {"Bulk line discount", "Gift wrapping"}
    overlap, gap, _ = compare(starter, profiles["acme/giftshop"], ScoutConfig())
    assert round(overlap, 3) == g.similarity.capability_overlap
    # replay: the same snapshot answers offline, no new LLM or GitHub calls
    replay = Snapshot(tmp_path / "snaps", "s1", "replay")
    pool = AgentPool({"scout": ["m"], "profiler": ["m"]}, replay, make_reply=lambda m: (_ for _ in ()).throw(AssertionError))
    scout2 = Scout(pool.one("scout"), GitHub(replay, transport=lambda r: (_ for _ in ()).throw(AssertionError(r))),
                   Profiler(pool.one("profiler"), replay), tmp_path / "clones", ScoutConfig(k=2), offline=True)
    peers2, _ = scout2.run(starter)
    assert [p.model_dump() for p in peers2] == [p.model_dump() for p in peers]
    with pytest.raises(ReplayMiss):
        replay.fetch("github", "never-seen", lambda: 1)


def test_deterministic_checks_block_bad_proposals(py_repo):
    good = proposal(tags=["validation rule", "new_name:BULK_MIN_QTY"])
    assert drafting.deterministic_checks(good, py_repo, "python")[0] == []
    cases = {
        "bug fix": proposal(title="Fix rounding bug in totals"),
        "network": proposal(behaviour_spec="Fetch rates from https://rates.example.net/api " * 6),
        "short": proposal(behaviour_spec="too short"),
        "source": proposal(expected_files=["tests/test_pricing.py"]),
        "exists": proposal(tags=["new_name:coupon_discount"]),
        "plan": proposal(test_plan=["one"]),
        "perf": proposal(title="Lazy-import terminal utilities to reduce CLI startup time"),
    }
    for name, p in cases.items():
        objections, _ = drafting.deterministic_checks(p, py_repo, "python")
        assert objections, name


def test_variation_filter_merges_near_duplicates_and_spreads_categories():
    a = proposal("a", tags=["validation rule"])
    b = proposal("b", tags=["validation rule"], test_plan=a.test_plan[:3])  # same peer feature, same text
    c = proposal("c", title="Receipt footer with the shop name", summary="Receipts end with a footer line.",
                 test_plan=["footer printed", "empty name", "long name wraps"], tags=["output formatting"],
                 provenance=[Provenance(feature_id="f2", peer_repo="other/shop", pr_url="u2")])
    merged = drafting.near_duplicates([a, b, c])
    assert len(merged) == 1 and merged[0][1].proposal_id in {"a", "b"}
    assert drafting.distinct_count([a, b, c]) == 2
    order = drafting.distinct_order([a, c])
    assert {order[0].proposal_id, order[1].proposal_id} == {"a", "c"}


def _report(verdict="valid", code=None, details=""):
    return ValidationReport(task_id="t", verdict=verdict, reject_code=code, reject_reason=details,
                            gates=[GateResult(gate="G3", passed=verdict == "valid", details=details)],
                            mutation_score=0.8)


def _bundle_feature():
    b = ReferenceBundle(pr=PullRef(number=7, title="bulk", url="u"),
                        impl_diff="+++ b/giftshop/pricing.py\n+def bulk(qty):\n+    return qty >= 10\n",
                        tests="+++ b/tests/test_pricing.py\n+def test_peer_secret_name():\n+    assert bulk(10)\n")
    return PeerFeature(id="f1", peer_repo="acme/giftshop", pr_url="u", summary="s", reference_bundle=b, snapshot_id="s",
                       licence="MIT")


def test_builder_role_split_repair_routing_and_similarity(tmp_path, py_spec):
    llm = FakeLLM()
    author, writer = SubAgent("author", "a", llm("a")), SubAgent("test_writer", "t", llm("t"))
    results = [_report("rejected", RejectCode.FEATURE_TESTS_PASS_WITHOUT_PATCH, "tests pass without the feature"),
               _report("rejected", RejectCode.REGRESSION, "an existing test failed"), _report()]
    seen = []

    def validate(draft):
        seen.append(json.loads((draft / "task.json").read_text()))
        return results.pop(0), seen[-1]["task_id"]

    p = proposal()
    res = Builder(author, writer, py_spec, tmp_path / "drafts", validate, repairs=2).build(p, _bundle_feature())
    assert res.verdict == "validated" and res.task_id == task_id_for(py_spec, p)
    assert [a["role"] for a in res.attempts] == ["tests", "author", "-"]
    assert len(llm.prompts["author"]) == 2 and len(llm.prompts["test_writer"]) == 2
    # the author never sees tests (ours or the peer's); the test writer never sees the patch or peer code
    for prompt in llm.prompts["author"]:
        assert "test_peer_secret_name" not in prompt and "def test_bulk" not in prompt
    for prompt in llm.prompts["test_writer"]:
        assert "amount -= (amount * 10 + 50)" not in prompt and "return qty >= 10" not in prompt
    assert "test_peer_secret_name" in llm.prompts["test_writer"][0]
    assert res.similarity is not None and res.similarity.patch < 0.5
    assert seen[-1]["repair_attempts"][0]["strategy"] == "tests_repair"


def test_builder_sends_near_copies_back(tmp_path, py_spec):
    llm = FakeLLM()
    feature = _bundle_feature()
    feature.reference_bundle.impl_diff = ("+++ b/toyshop/pricing.py\n+    amount = price_cents * qty\n"
                                          "+    if qty >= 10:\n+        amount -= (amount * 10 + 50) // 100\n+    return amount\n")
    b = Builder(SubAgent("author", "a", llm("a")), SubAgent("test_writer", "t", llm("t")), py_spec, tmp_path / "d",
                lambda d: (_report(), "x"), repairs=1, max_similarity=0.8)
    res = b.build(proposal(), feature)
    assert res.verdict == "dropped" and res.attempts[0]["code"] == "NEAR_COPY_PATCH" and res.similarity.patch >= 0.8
    assert code_similarity("+a = b + c * d - e / f\n", "+a = b + c * d - e / f\n") == 1.0


# =========================================================================== N8 · benchmark


def test_worker_replay_solves_empty_fails_crash_retries_and_restart_resumes(store, tmp_path):
    _validated(store, "p1", "t1")
    task = tmp_path / "t1"
    task.mkdir()
    (task / "instruction.md").write_text("# t")
    from factory.reference.schema import JobBudget
    enqueue(store, "r1", "t1", task, ["oracle", "nop", "crash"], proposal_id="p1", budget=JobBudget(max_retries=1))
    # a worker that died mid-job: its lease expires, a new worker resumes it
    dead = store.lease("dead-worker", lease_s=0.01)
    time.sleep(0.05)
    w = Worker(store, FakeBackend(), backoff_s=0.01, run_id="r1")
    w.run_until_empty(poll_s=0.02)
    res = {r.agent_id: r for r in store.list_results("r1")}
    assert res["oracle"].outcome == "solved" and res["nop"].outcome == "failed"
    assert res["crash"].outcome == "infra_error"
    jobs = {j.agent_id: j for j in store.list_jobs("r1")}
    assert jobs["crash"].state == JobState.RUN_FAILED and jobs[dead.agent_id].worker != "dead-worker"
    assert store.get_proposal("p1").state == S.BENCHMARKED
    s = summary(store.list_results("r1"), store.list_jobs("r1"))
    assert s["agents"]["oracle"]["solved"] == 1 and s["agents"]["crash"]["infra_error"] == 1
    # a changed package invalidates older results
    (task / "instruction.md").write_text("# changed")
    assert dir_checksum(task) != jobs["oracle"].package_checksum
    enqueue(store, "r1", "t1", task, ["oracle"], proposal_id="p1")
    assert all(r.package_checksum != dir_checksum(task) for r in store.list_results("r1", current_only=False))
    assert store.list_results("r1") == []


def test_worker_stops_on_kill_switch_and_cost_cap(store, tmp_path):
    task = tmp_path / "t"
    task.mkdir()
    (task / "a").write_text("1")
    enqueue(store, "r1", "t", task, ["oracle"])
    store.set_kill(True)
    assert Worker(store, FakeBackend(), run_id="r1").run_one() is None
    store.set_kill(False)
    assert Worker(store, FakeBackend(), run_id="r1", cost_cap_usd=0.0).run_one() is None and store.killed()


# =========================================================================== N9 · controller, replay, report


def _controller(tmp_path, py_spec, store, snapshot, make_reply, transport, validate):
    cfg = FactoryConfig(workdir=str(tmp_path / "wd"))
    cfg.reference.target = 1
    cfg.reference.overgenerate = 2.0
    pool = AgentPool({"drafter": ["m"]}, snapshot, make_reply=make_reply)
    peer_dir = make_peer_repo(tmp_path)
    ctl = Controller(cfg, py_spec, EnvConfig(base_image="python:3.12", test_command="pytest", framework="pytest", report_format="junit"), store, pool, snapshot,
                     GitHub(snapshot, transport=transport), validate, RunOptions(target=1, harbor="off"),
                     tmp_path / "wd", cloner=lambda url, dest: (peer_dir, "d" * 40))
    return ctl


def test_controller_end_to_end_replays_offline_with_the_same_ids(tmp_path, py_spec):
    store = ReferenceStore(mongomock.MongoClient().db)
    llm = FakeLLM()
    valid = []

    def validate(draft):
        valid.append(json.loads((draft / "task.json").read_text())["task_id"])
        return _report(), valid[-1]

    snap = Snapshot(tmp_path / "snaps", "toyshop-s1")
    ctl = _controller(tmp_path, py_spec, store, snap, llm, fake_github(tmp_path), validate)
    out = ctl.run()
    assert out["status"] == "done" and out["valid"] == 1 and ctl.run_id == run_id_for(py_spec, "toyshop-s1")
    props = store.list_proposals(ctl.run_id)
    states = sorted(p.state.value for p in props)
    assert states == ["MERGED", "VALIDATED"]  # two drafts of the same feature: the duplicate merged
    v = next(p for p in props if p.state == S.VALIDATED)
    assert v.approved_by == "policy" and any(r.reviewer == "policy:auto-approve" for r in v.reviews)
    assert "exactly 10 items" in " ".join(v.test_plan)  # a critic's missing edge case joined the plan
    report = build_report(store, ctl.run_id)
    assert report["yield"]["valid"] == 1 and report["declarations"]["models"]
    md = render_markdown(report)
    assert "acme/giftshop" in md and "Declarations" in md
    side = json.loads((tmp_path / "wd" / "reference" / "harbor" / "provenance" / f"{valid[0]}.json").read_text())
    assert side["sources"][0]["licence"] == "MIT" and side["novelty"]["in_repo"] is False

    # replay on a fresh state store: no web, no LLM, same run id, proposal ids and task id
    store2 = ReferenceStore(mongomock.MongoClient().db)
    replay = Snapshot(tmp_path / "snaps", "toyshop-s1", "replay")
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("network or LLM used in replay"))  # noqa: E731
    shutil.rmtree(tmp_path / "wd" / "reference" / "drafts")
    ctl2 = _controller(tmp_path, py_spec, store2, replay, lambda m: boom, boom, validate)
    out2 = ctl2.run()
    assert out2["valid"] == 1 and ctl2.run_id == ctl.run_id
    assert sorted(p.proposal_id for p in store2.list_proposals(ctl2.run_id)) == sorted(p.proposal_id for p in props)
    assert valid[1] == valid[0]


def test_review_mode_waits_for_the_reviewer(tmp_path, py_spec):
    from factory.reference.review import approve, card, reject

    store = ReferenceStore(mongomock.MongoClient().db)
    snap = Snapshot(tmp_path / "snaps", "s")
    ctl = _controller(tmp_path, py_spec, store, snap, FakeLLM(), fake_github(tmp_path), lambda d: (_report(), "t"))
    ctl.opts.review = True
    ctl.run()
    pending = store.list_proposals(ctl.run_id, [S.PENDING_REVIEW])
    assert len(pending) == 1 and not store.list_proposals(ctl.run_id, [S.VALIDATED])
    assert "source: acme/giftshop" in card(pending[0])
    from factory.errors import ConfigError
    with pytest.raises(ConfigError):
        reject(store, pending[0], "me", "")
    approve(store, pending[0], "me", "fine")
    assert store.get_proposal(pending[0].proposal_id).approved_by == "user:me"
    ctl.run()
    assert store.list_proposals(ctl.run_id, [S.VALIDATED])


def test_parse_repo_and_text_helpers():
    assert parse_repo("https://github.com/pallets/click.git") == "pallets/click"
    assert parse_repo("pallets/click") == "pallets/click" and parse_repo("/tmp/a/b/c") is None
    assert match_score("search products by text", "search products by text and tag") > 0.6


def test_quota_fallback_switches_model_and_caches_under_the_role_key(tmp_path):
    from factory.errors import ModelProviderError

    calls = []

    def make(model):
        def reply(prompt):
            calls.append(model)
            if model == "a":
                raise ModelProviderError("Free usage limit reached")
            return '{"ok": true}'
        return reply

    snap = Snapshot(tmp_path, "s")
    pool = AgentPool({"drafter": ["a"]}, snap, make_reply=make, fallback=["a", "b"])
    agent = pool.one("drafter")
    assert agent.ask_json("hi") == {"ok": True} and agent.answered_by == "b"
    assert agent.ask_json("again") == {"ok": True} and calls == ["a", "b", "b"]  # switched for good
    assert pool.declarations()[0]["answered_by"] == "b"
    replay = AgentPool({"drafter": ["a"]}, Snapshot(tmp_path, "s", "replay"), make_reply=make)
    assert replay.one("drafter").ask_json("hi") == {"ok": True}


def test_parallel_threads_follow_a_switch_made_by_another_thread(tmp_path):
    import threading

    from factory.errors import ModelProviderError

    gate = threading.Barrier(2)

    def make(model):
        def reply(prompt):
            if model == "a":
                gate.wait(timeout=5)  # both threads are inside the failing model at once
                raise ModelProviderError("quota")
            return '{"ok": 1}'
        return reply

    agent = AgentPool({"drafter": ["a"]}, make_reply=make, fallback=["a", "b"]).one("drafter")
    from factory.reference.parallel import pmap
    assert pmap(lambda p: agent.ask_json(p), ["x", "y"], 2) == [{"ok": 1}, {"ok": 1}]


def test_deterministic_checks_ignore_negations_and_adjectives(py_repo):
    ok = proposal(summary="The Error: label uses a darker red instead of the fixed bright red on light terminals.",
                  behaviour_spec="Tests fake the loader; no dependency or network access is needed. " * 5)
    assert drafting.deterministic_checks(ok, py_repo, "python")[0] == []
    for p in (proposal(title="Fix the crash in coupon parsing"), proposal(summary="This bug fix restores totals."),
              proposal(behaviour_spec="Download rates from a remote server before pricing. " * 6)):
        assert drafting.deterministic_checks(p, py_repo, "python")[0]


def test_adapted_names_count_only_when_defined():
    src = "def patch_all():\n    pass\n# we patch things here\nclass Theme:\n    x = 1\nMAX_WIDTH = 80\nopts = ['--color']\n"
    assert not drafting.defines(src, "patch") and drafting.defines(src, "Theme")
    assert drafting.defines(src, "MAX_WIDTH") and drafting.defines(src, "--color") and not drafting.defines(src, "x == 1")


def test_bedrock_backend_calls_converse_with_the_api_key(monkeypatch):
    import io

    import factory.model_client as mc

    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, request, timeout=None):
            seen["url"], seen["auth"] = request.full_url, request.headers.get("Authorization")
            seen["body"] = json.loads(request.data)
            return Resp(json.dumps({"output": {"message": {"content": [{"text": '{"ok": 1}'}]}},
                                    "usage": {"inputTokens": 3, "outputTokens": 2}}).encode())

    monkeypatch.setattr(mc, "build_opener", lambda *a: Opener())
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-key")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    client = mc.ModelClient("bedrock/qwen.qwen3-coder-next")
    assert client.reply([{"role": "user", "content": "hi"}]) == '{"ok": 1}'
    assert seen["url"] == "https://bedrock-runtime.us-west-2.amazonaws.com/model/qwen.qwen3-coder-next/converse"
    assert seen["auth"] == "Bearer test-key" and seen["body"]["messages"][0]["content"][0]["text"] == "hi"
    assert client._bedrock.usage == {"input_tokens": 3, "output_tokens": 2, "calls": 1}
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    from factory.errors import ConfigError
    with pytest.raises(ConfigError):
        client.reply([{"role": "user", "content": "hi"}])


def test_latency_is_recorded_per_stage_and_per_call(tmp_path, py_spec):
    store = ReferenceStore(mongomock.MongoClient().db)
    ctl = _controller(tmp_path, py_spec, store, Snapshot(tmp_path / "s", "x"), FakeLLM(), fake_github(tmp_path),
                      lambda d: (_report(), json.loads((d / "task.json").read_text())["task_id"]))
    ctl.run()
    run = store.get_run(ctl.run_id)
    assert {"P1 profile", "P2 scout", "P5 research", "P6 critic + spec", "P8 build"} <= set(run["timings"])
    assert {c["role"] for c in run["llm_calls"]} >= {"profiler", "scout", "researcher", "critic", "drafter", "author",
                                                     "test_writer"}
    r = build_report(store, ctl.run_id)
    assert r["latency"]["llm"]["profiler"]["calls"] >= 1 and "Latency" in render_markdown(r)
    t = r["tasks"][0]
    assert t["build_s"] is not None and "author_s" in t["attempt_timings"][-1] and "gates_s" in t["attempt_timings"][-1]


def test_dependents_named_by_the_model_lose_to_real_alternatives(toy_profile):
    black = PeerRepo(full_name="psf/black", url="u", description="The uncompromising Python code formatter",
                     found_by=["named alternative"], role="dependent", role_reason="a formatter built with click")
    typer = PeerRepo(full_name="fastapi/typer", url="u", description="Typer, build great CLIs", found_by=["topic: click"],
                     role="alternative")
    assert Scout.relevance(toy_profile, typer) > ScoutConfig().min_relevance > Scout.relevance(toy_profile, black)


def test_edit_hints_point_to_the_right_file_and_real_lines(py_repo):
    from factory.reference.building import edit_hints

    wrong_file = {"edits": [{"path": "toyshop/money.py", "search": "def line_total(price_cents: int, qty: int) -> int:\n",
                             "replace": "x"}]}
    assert "it is in toyshop/pricing.py" in edit_hints(py_repo, wrong_file)
    close = {"edits": [{"path": "toyshop/pricing.py", "search": "def line_total(price_cents, qty):\n", "replace": "x"}]}
    h = edit_hints(py_repo, close)
    assert "closest real lines" in h and "def line_total(price_cents: int, qty: int) -> int:" in h
    ok = {"edits": [{"path": "toyshop/pricing.py", "search": "def line_total(", "replace": "x"}]}
    assert edit_hints(py_repo, ok) == ""


def test_model_json_slips_are_repaired():
    from factory.generation.author import _json_object

    assert _json_object('{\n  "a": "use @group(name="x") here",\n  "b": [1, 2,],\n}') == {"a": 'use @group(name="x") here', "b": [1, 2]}
    assert _json_object('{"a": "an enum\\-like class"}') == {"a": "an enum\\-like class"}
    assert _json_object('```json\n{"tests": {"t.py": "x"}\n```') == {"tests": {"t.py": "x"}}
    assert _json_object('{"ideas": [{"a": "b"') == {"ideas": [{"a": "b"}]}
    assert _json_object("no json here") is None


def test_boundaries_are_stated_only_when_the_quote_is_in_the_docs(py_repo):
    from factory.reference.profiler import verify_boundaries

    (py_repo / "README.md").write_text("# Toyshop\n\nToyshop does **not** handle payments or shipping.\n")
    out = verify_boundaries([
        {"text": "no payments", "source": "README.md", "quote": "Toyshop does not handle payments or shipping."},
        {"text": "no plugins", "source": "README.md", "quote": "Toyshop has no plugin system at all."},
        {"text": "no async", "source": "", "quote": ""}, "plain string boundary"], py_repo)
    assert out[0].startswith("[stated] no payments") and out[1] == "[inferred] no plugins"
    assert out[2] == "[inferred] no async" and out[3] == "[inferred] plain string boundary"


# =========================================================================== research agents (tools + critic)


def _toolbox(tmp_path, py_repo, toy_profile):
    from factory.reference.research import Toolbox

    snap = Snapshot(tmp_path / "snaps", "t")
    gh = GitHub(snap, transport=fake_github(tmp_path))
    peer = PeerRepo(full_name="acme/giftshop", url="u", licence="MIT", language="Python", default_branch="main",
                    gap=[Capability(name="Gift wrapping", behaviour="wrap an order", evidence=["giftshop/pricing.py:bulk"])])
    return Toolbox(py_repo, toy_profile, gh, [peer], Miner(None, gh, "t"))


def test_toolbox_reads_the_repo_and_records_peer_references(tmp_path, py_repo, toy_profile):
    tools = _toolbox(tmp_path, py_repo, toy_profile)
    assert "toyshop/pricing.py:10: def line_total" in tools.call("repo_search", {"pattern": "def line_total"})
    assert "10: def line_total" in tools.call("repo_read", {"path": "toyshop/pricing.py", "start": 5, "end": 12})
    assert "did you mean toyshop/pricing.py" in tools.call("repo_read", {"path": "src/pricing.py"})
    assert "error" in tools.call("repo_read", {"path": "../../etc/passwd"})
    assert "Gift wrapping" in tools.call("peers", {})
    out = tools.call("peer_pr", {"peer": "acme/giftshop", "number": 7})
    assert "Add bulk discount" in out and "acme/giftshop#7" in tools.features
    assert "Gift wrapping" in tools.call("peer_capability", {"peer": "acme/giftshop", "name": "Gift wrapping"})
    assert "unknown peer" in tools.call("peer_pr", {"peer": "nobody/x", "number": 1})
    assert "unknown tool" in tools.call("rm_rf", {})


def test_action_loop_uses_tools_recovers_and_forces_a_final(tmp_path, py_repo, toy_profile):
    from factory.reference.research import run_agent

    tools = _toolbox(tmp_path, py_repo, toy_profile)
    seen = []
    replies = iter(["not json at all", '{"tool": "shell", "args": {}}',
                    '{"tool": "repo_search", "args": {"pattern": "Cart"}} <|tool_call_end|> '
                    '{"tool": "repo_tree", "args": {}}',  # two calls in one reply: both run
                    '{"final": {"done": true}}'])

    def reply(prompt):
        seen.append(prompt)
        return next(replies)

    run = run_agent(SubAgent("researcher", "m", reply), "Do research.", "find things", tools,
                    ["repo_search", "repo_tree"], '{"done": true}', max_steps=5)
    assert run.final == {"done": True} and [s["tool"] for s in run.steps] == ["repo_search", "repo_tree"]
    assert "had no JSON object" in seen[1] and "not available here" in seen[2]
    assert "LAST TURN" not in seen[3] and len(seen) == 4


def test_critic_fix_leads_to_one_refinement_then_acceptance(tmp_path, py_spec):
    class Fixer(FakeLLM):
        def idea_critic(self, prompt):
            if "Your steps so far" not in prompt:
                return {"tool": "repo_search", "args": {"pattern": "def line_total"}}
            if "12 or more" in prompt:
                return {"final": {"verdict": "accept", "evidence": ["toyshop/pricing.py:10"]}}
            return {"final": {"verdict": "fix", "problems": ["the threshold collides with coupons"], "fix": "use 12"}}

    store = ReferenceStore(mongomock.MongoClient().db)
    ctl = _controller(tmp_path, py_spec, store, Snapshot(tmp_path / "s", "x"), Fixer(), fake_github(tmp_path),
                      lambda d: (_report(), json.loads((d / "task.json").read_text())["task_id"]))
    ctl.run()
    v = [p for p in store.list_proposals(ctl.run_id) if p.state == S.VALIDATED]
    assert v and v[0].edits[0].by.startswith("drafter:") and "12 or more" in v[0].idea["summary"]
    assert [r.verdict for r in v[0].reviews if r.reviewer.startswith("critic:")] == ["objection", "pass"]
    assert build_report(store, ctl.run_id)["manual_edits"] == []


def test_ideas_that_already_exist_are_rejected_with_evidence(tmp_path, py_spec):
    class Exists(FakeLLM):
        def idea_critic(self, prompt):
            return {"final": {"verdict": "accept", "already_exists": True,
                              "evidence": ["toyshop/pricing.py:19 coupon_discount already does it"]}}

    store = ReferenceStore(mongomock.MongoClient().db)
    ctl = _controller(tmp_path, py_spec, store, Snapshot(tmp_path / "s", "x"), Exists(), fake_github(tmp_path),
                      lambda d: (_report(), "t"))
    ctl.opts.target = 1
    out = ctl.run()
    rejected = store.list_proposals(ctl.run_id, [S.REJECTED])
    assert out["status"] == "short" and rejected
    assert rejected[0].novelty.in_repo and "coupon_discount" in rejected[0].novelty.evidence[0]
    assert build_report(store, ctl.run_id)["novelty"]["rejected"] >= 1


def test_a_quota_stop_keeps_the_idea_and_a_resume_judges_it(tmp_path, py_spec):
    from factory.errors import ModelProviderError

    llm = FakeLLM()
    state = {"quota": True}

    def make(model):
        inner = llm(model)

        def reply(prompt):
            if state["quota"] and "independent critic" in prompt:
                raise ModelProviderError("Free usage limit reached")
            return inner(prompt)
        return reply

    store = ReferenceStore(mongomock.MongoClient().db)
    snap = Snapshot(tmp_path / "s", "x")
    validate = lambda d: (_report(), json.loads((d / "task.json").read_text())["task_id"])  # noqa: E731
    ctl = _controller(tmp_path, py_spec, store, snap, make, fake_github(tmp_path), validate)
    with pytest.raises(ModelProviderError):
        ctl.run()
    drafted = store.list_proposals(ctl.run_id, [S.DRAFTED])
    assert drafted and all(p.idea and not p.reviews for p in drafted)
    state["quota"] = False
    ctl2 = _controller(tmp_path, py_spec, store, snap, make, fake_github(tmp_path), validate)
    ctl2.run()
    assert store.list_proposals(ctl2.run_id, [S.VALIDATED])


def test_peer_pr_bundle_role_views(tmp_path):
    gh = GitHub(Snapshot(tmp_path / "snaps", "s1"), transport=fake_github(tmp_path))
    peer = PeerRepo(full_name="acme/giftshop", url="u", licence="MIT", language="Python")
    b = Miner(None, gh, "s1").bundle(peer, 7)
    assert b.issue["number"] == 3 and b.discussion == ["maint (giftshop/pricing.py): What about exactly 10 items?"]
    assert "def bulk" in b.impl_diff and "test_bulk" in b.tests and "bulk discount" in b.docs
    assert "tests/test_pricing.py" in b.neighbour_tests
    assert "test_bulk" not in json.dumps(for_author(b))
    assert "def bulk(qty)" not in json.dumps(for_test_writer(b)) and "test_bulk" in json.dumps(for_test_writer(b))
    assert added_names("+def visible():\n+def _hidden():\n+class Thing:\n") == ["visible", "Thing"]


def test_critics_from_another_family_come_first():
    llm = FakeLLM()
    pool = AgentPool({"drafter": ["opencode/deepseek-v4", "qwen/qwen3"], "critic": ["deepseek/x", "anthropic/claude-opus-5-5"]},
                     make_reply=llm)
    assert pool.one("critic").model == "anthropic/claude-opus-5-5" and pool.cross_family_critic()
    assert family("opencode/deepseek-v4.1-flash-free") == "deepseek" and family("bedrock/qwen.qwen3-coder-next") == "qwen"


def test_capability_bundles_carry_the_peer_code(tmp_path):
    from factory.reference.gapideas import CapabilityBundles

    gh = GitHub(Snapshot(tmp_path, "s"), transport=fake_github(tmp_path))
    peer = PeerRepo(full_name="acme/giftshop", url="u", licence="MIT", default_branch="main")
    b = CapabilityBundles(gh).bundle(peer, Capability(name="Gift wrapping", behaviour="wrap",
                                                      evidence=["giftshop/pricing.py:bulk"]))
    assert b.pr.url == "https://github.com/acme/giftshop/blob/main/giftshop/pricing.py"
    assert "def test_old" in b.impl_diff and "tests/test_pricing.py" in b.neighbour_tests
    assert CapabilityBundles(gh).bundle(peer, Capability(name="x", behaviour="y", evidence=[])) is None


def test_an_agent_that_keeps_exploring_gets_a_closing_turn(tmp_path, py_repo, toy_profile):
    from factory.reference.research import run_agent

    tools = _toolbox(tmp_path, py_repo, toy_profile)
    seen = []

    def reply(prompt):
        seen.append(prompt)
        if "No tools are available now" in prompt:
            return '{"ideas": ["from what it learned"]}'
        return '{"tool": "repo_tree", "args": {}}'  # ignores even "LAST TURN"

    run = run_agent(SubAgent("researcher", "m", reply), "Research.", "t", tools, ["repo_tree"], "{}", max_steps=4)
    assert run.final == {"ideas": ["from what it learned"]} and len(run.steps) == 4
    assert "LAST TURN" in seen[3] and "Tools:" not in seen[4]


def test_critic_gets_where_names_are_defined_and_works_today_rejects(tmp_path, py_repo, toy_profile):
    from factory.reference.research import IdeaCritic, names_in_code

    idea = {"title": "Expose `coupon_discount` on carts", "summary": "Add a `line_total` hook and shell_complete",
            "behaviour": "uses `Cart.add(` too"}
    hits = names_in_code(py_repo, idea, ["toyshop/pricing.py", "toyshop/cart.py"])
    assert any(h.startswith("coupon_discount: toyshop/pricing.py") for h in hits)
    assert any(h.startswith("line_total: toyshop/pricing.py") for h in hits) and any(h.startswith("add: ") for h in hits) is False
    tools = _toolbox(tmp_path, py_repo, toy_profile)
    seen = []

    def reply(prompt):
        seen.append(prompt)
        return json.dumps({"final": {"example": "pricing.coupon_discount(100, 'X')", "works_today": True,
                                     "verdict": "accept"}})

    verdict, _ = IdeaCritic(SubAgent("critic", "m", reply), tools).judge(toy_profile, idea)
    assert verdict["verdict"] == "reject" and verdict["already_exists"] and "works today" in verdict["evidence"][0]
    assert "names_already_defined_in_code" in seen[0] and "coupon_discount: toyshop/pricing.py" in seen[0]


def test_a_final_answer_without_the_wrapper_is_accepted(tmp_path, py_repo, toy_profile):
    from factory.reference.research import names_in_code, run_agent

    tools = _toolbox(tmp_path, py_repo, toy_profile)
    run = run_agent(SubAgent("critic", "m", lambda p: '{"verdict": "reject", "works_today": true}'), "Judge.", "t",
                    tools, ["repo_read"], "{}", max_steps=3)
    assert run.final == {"verdict": "reject", "works_today": True} and run.steps == []
    (py_repo / "toyshop" / "params.py").write_text("class Param:\n    def __init__(\n        self,\n"
                                                    "        callback: object | None = None,\n    ): pass\n")
    hits = names_in_code(py_repo, {"title": "Argument value transformation callback"}, ["toyshop/params.py"])
    assert hits and hits[0].startswith("callback: toyshop/params.py:4")


def test_running_the_example_overrules_a_wrong_reading_of_the_code(tmp_path, py_repo, toy_profile):
    from factory.reference.research import IdeaCritic

    tools = _toolbox(tmp_path, py_repo, toy_profile)
    calls = []

    def reply(prompt):
        calls.append(prompt)
        if "was executed in a sandbox" in prompt:
            return json.dumps({"final": {"works_today": True, "verdict": "reject", "already_exists": True,
                                         "evidence": ["it ran"]}})
        return json.dumps({"final": {"example": "```python\nimport toyshop\nprint('ok')\n```", "works_today": False,
                                     "verdict": "accept"}})

    ran = []
    runner = lambda code: (ran.append(code) or True, "ok")  # noqa: E731
    verdict, steps = IdeaCritic(SubAgent("critic", "m", reply), tools, runner=runner).judge(toy_profile, {"title": "x"})
    assert ran == ["import toyshop\nprint('ok')"] and verdict["verdict"] == "reject" and verdict["already_exists"]
    assert any("example ran in the sandbox" in e for e in verdict["evidence"]) and any(s["tool"] == "run_example" for s in steps)
    # an agreeing result needs no second opinion
    calls.clear()
    agree = lambda code: (False, "TypeError: unexpected keyword argument")  # noqa: E731
    verdict, _ = IdeaCritic(SubAgent("critic", "m", reply), tools, runner=agree).judge(toy_profile, {"title": "y"})
    assert verdict["verdict"] == "accept" and len(calls) == 1 and "failed in the sandbox" in verdict["evidence"][0]


def test_example_prelude_imports_the_package_and_broken_examples_prove_nothing(tmp_path, py_repo, toy_profile):
    from factory.reference.research import IdeaCritic, top_packages

    assert top_packages(py_repo) == ["toyshop"]
    tools = _toolbox(tmp_path, py_repo, toy_profile)
    reply = lambda p: json.dumps({"final": {"example": "x = undefined_name", "works_today": False,  # noqa: E731
                                             "verdict": "accept"}})
    broken = lambda code: (False, "NameError: name 'undefined_name' is not defined")  # noqa: E731
    verdict, _ = IdeaCritic(SubAgent("critic", "m", reply), tools, runner=broken).judge(toy_profile, {"title": "z"})
    assert verdict["verdict"] == "accept" and "inconclusive" in verdict["evidence"][-1]


def test_peers_from_the_same_organisation_or_built_on_the_starter_are_cut(tmp_path, py_repo):
    llm = FakeLLM()
    snap = Snapshot(tmp_path / "snaps", "s9")
    peer_dir = make_peer_repo(tmp_path)
    # the peer now depends on the starter: a derivative, not a similar project
    (peer_dir / "pyproject.toml").write_text('[project]\nname = "giftshop"\ndependencies = ["toyshop>=1"]\n')
    gh = GitHub(snap, transport=fake_github(peer_dir))
    starter = Profiler(SubAgent("profiler", "m", llm("m"))).profile(py_repo, "toyshop", commit="c" * 40)
    scout = Scout(SubAgent("scout", "m", llm("m"), snap), gh, Profiler(SubAgent("profiler", "m", llm("m"), snap), snap),
                  tmp_path / "clones", ScoutConfig(k=2), cloner=lambda url, dest: (peer_dir, "d" * 40))
    peers, _ = scout.run(starter)
    g = next(p for p in peers if p.full_name == "acme/giftshop")
    assert g.status == "cut" and any("built on toyshop" in r for r in g.reasons)
    starter2 = starter.model_copy(update={"url": "https://github.com/acme/toyshop"})
    cut = scout.cheap_cut(starter2, [PeerRepo(full_name="acme/giftshop", url="u", licence="MIT", stars=900)])
    assert cut[0].status == "cut" and "same organisation" in cut[0].reasons[-1]



def test_peer_docs_are_listed_read_and_kept_as_references(tmp_path, py_repo, toy_profile):
    tools = _toolbox(tmp_path, py_repo, toy_profile)
    assert "no documentation files" in tools.call("peer_docs", {"peer": "acme/giftshop"})  # the fake tree has none
    out = tools.call("peer_read", {"peer": "acme/giftshop", "path": "README.md"})
    assert "https://github.com/acme/giftshop/blob/main/README.md" in out
    f = tools.features["acme/giftshop:README.md"]
    assert f.source == "doc" and f.reference_bundle.docs


def test_web_tools_only_with_a_provider_and_saved_for_replay(tmp_path, py_repo, toy_profile, monkeypatch):
    from factory.reference import websearch
    from factory.reference.research import RESEARCH_TOOLS

    tools = _toolbox(tmp_path, py_repo, toy_profile)
    assert "web_search" not in tools.available(RESEARCH_TOOLS)  # no key: not offered
    for k in websearch.PROVIDERS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    monkeypatch.setattr(websearch, "_search_live", lambda q, n=8: [{"title": "Ask: min count", "url": "https://x.io/a",
                                                                     "snippet": "users want -vv minimum"}])
    snap = Snapshot(tmp_path / "w", "s")
    tools.web = websearch.WebSearch(snap)
    assert "web_search" in tools.available(RESEARCH_TOOLS)
    assert "https://x.io/a | Ask: min count" in tools.call("web_search", {"query": "count option minimum"})
    monkeypatch.setattr(websearch, "_search_live", lambda q, n=8: 1 / 0)
    replay = websearch.WebSearch(Snapshot(tmp_path / "w", "s", "replay"))
    assert replay.search("count option minimum")[0]["url"] == "https://x.io/a"
    assert "only public https" in websearch.WebSearch(snap).read("http://127.0.0.1/admin")
    assert websearch.page_text("<p>Hi<script>x()</script></p><p>there</p>") == "Hi\nthere"


def test_context_similarity_ranks_peers_before_stars(toy_profile):
    close = PeerRepo(full_name="a/close", url="u", stars=300, role="alternative", context=0.9)
    famous = PeerRepo(full_name="b/famous", url="u", stars=90_000, role="relative", context=0.4)
    dep = PeerRepo(full_name="c/dep", url="u", stars=50_000, role="dependent", context=0.9)
    ranked = sorted([famous, dep, close], key=lambda p: (-Scout.relevance(toy_profile, p), -p.stars))
    assert [p.full_name for p in ranked] == ["a/close", "b/famous", "c/dep"]
    assert Scout.relevance(toy_profile, dep) < ScoutConfig().min_relevance


def test_author_feedback_keeps_failing_behaviours_but_no_test_code():
    from factory.reference.building import _author_feedback

    fb = ("Verdict: FEATURE_TESTS_FAIL_WITH_PATCH: feature tests fail\nTest output (end):\n"
          "    def test_alias_shown_in_group_help(runner):\n        result = runner.invoke(cli, ['--help'])\n"
          "E       AssertionError: 'ls' not in help output\n"
          "FAILED tests/test_aliases.py::test_alias_shown_in_group_help - AssertionError\n")
    out = _author_feedback(fb, "FEATURE_TESTS_FAIL_WITH_PATCH")
    assert "'ls' not in help output" in out and "FAILED tests/test_aliases.py::test_alias_shown_in_group_help" in out
    assert "runner.invoke" not in out and "def test_alias" not in out


# =========================================================================== repair routing + parallel research

OPTION_GROUP_FAIL = """Verdict: FEATURE_TESTS_FAIL_WITH_PATCH: feature tests fail with the reference patch: t
________________ TestGroups.test_basic ________________
    def test_basic(runner):
>       @click.option_group("Network")
tests/test_groups.py:12:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
    def __getattr__(name):
        SECRET_PATCH_LINE = 1
>       raise AttributeError(name)
E       AttributeError: option_group
src/click/__init__.py:120: AttributeError
FAILED tests/test_groups.py::TestGroups::test_basic - AttributeError: option_group
"""


def test_a_missing_name_goes_to_whoever_broke_the_instruction():
    from factory.reference.building import missing_names, route

    code = RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH.value
    assert missing_names(OPTION_GROUP_FAIL) == [("option_group", "click.option_group", "path")]
    # an attribute the instruction promises on an object is the change's to provide, whatever `self.x` says
    member = "E       AttributeError: 'Choice' object has no attribute 'match_prefix'\n"
    owner, _ = route(code, member, 3, "# P\n`click.Choice(match_prefix=True)` stores `self.match_prefix`.", [])
    assert owner == "author"
    assert route(code, member, 3, "# P\nChoices match prefixes.", [])[0] == "tests"
    # the instruction promises click.option_group: the change must provide it
    owner, why = route(code, OPTION_GROUP_FAIL, 0, "# G\nUse `@click.option_group(name)`.", [])
    assert owner == "author" and "click.option_group" in why
    # the instruction says where it lives: the tests must import it from there
    owner, why = route(code, OPTION_GROUP_FAIL, 0, "# G\n`from click.decorators import option_group`", [])
    assert owner == "tests" and "from click.decorators import option_group" in why
    # the instruction names it but never says how to import it: the author states the path
    owner, _ = route(code, OPTION_GROUP_FAIL, 0, "# G\nAdd an option_group decorator.", [])
    assert owner == "author"
    # the tests invented the name
    owner, why = route(code, OPTION_GROUP_FAIL, 0, "# G\nOptions get a group_name.", [])
    assert owner == "tests" and "never mentions" in why


def test_a_plain_failure_goes_to_the_test_writer_first_then_the_author_if_it_persists():
    from factory.reference.building import route

    code = RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH.value
    out = "E   assert 1 == 0\nFAILED tests/t.py::test_a - assert 1 == 0\n"
    assert route(code, out, 0, "# X", [])[0] == "tests"  # first failure: the gate's reason to the test writer
    history = [{"code": code, "role": "tests", "failing": ["tests/t.py::test_a"]}]
    assert route(code, out, 1, "# X", history)[0] == "author"  # same tests still fail after the tests repair
    other = "FAILED tests/t.py::test_b - assert 2 == 3\n"
    assert route(code, other, 1, "# X", history)[0] == "author"  # alternates otherwise
    assert route(code, "ERROR tests/t.py - fixture 'runner' not found", 2, "# X",
                 history + [{"code": code, "role": "author"}])[0] == "tests"


def test_tests_feedback_hides_library_frames_but_keeps_errors(tmp_path):
    from factory.reference.building import tests_feedback

    log = tmp_path / "G2.json"
    log.write_text(json.dumps({"exec": {"stdout": OPTION_GROUP_FAIL.split("\n", 1)[1], "stderr": ""}}))
    rep = ValidationReport(task_id="t", verdict="rejected", reject_code=RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH,
                           reject_reason="feature tests fail", gates=[GateResult(gate="G2", passed=False,
                                                                                 exec_log_paths=[str(log)])])
    out = tests_feedback(rep, ["tests/test_groups.py"])
    assert '@click.option_group("Network")' in out and "AttributeError: option_group" in out
    assert "SECRET_PATCH_LINE" not in out and "inside the library" in out


def test_builder_sends_the_first_test_failure_to_the_test_writer_with_the_reason(tmp_path, py_spec):
    llm = FakeLLM()
    author, writer = SubAgent("author", "a", llm("a")), SubAgent("test_writer", "t", llm("t"))
    results = [_report("rejected", RejectCode.FEATURE_TESTS_FAIL_WITH_PATCH,
                       "feature tests fail with the reference patch: tests/test_x.py::test_bulk"), _report()]
    res = Builder(author, writer, py_spec, tmp_path / "drafts", lambda d: (results.pop(0), "id"),
                  repairs=2).build(proposal(), _bundle_feature())
    assert res.verdict == "validated" and [a["role"] for a in res.attempts] == ["tests", "-"]
    assert len(llm.prompts["author"]) == 1 and len(llm.prompts["test_writer"]) == 2
    repair = llm.prompts["test_writer"][1]
    assert "FEATURE_TESTS_FAIL_WITH_PATCH" in repair and "Diagnosis:" in repair and "def test_bulk" in repair


def test_a_test_file_that_does_not_compile_goes_back_to_the_test_writer(tmp_path, py_spec):
    llm = FakeLLM()
    calls = []
    real = llm.test_writer

    def broken_once(prompt):
        calls.append(prompt)
        data = real(prompt)
        if len(calls) == 1:
            data["tests"] = {k: "def test_x(:\n" for k in data["tests"]}
        return data

    llm.test_writer = broken_once
    res = Builder(SubAgent("author", "a", llm("a")), SubAgent("test_writer", "t", llm("t")), py_spec, tmp_path / "d",
                  lambda d: (_report(), "id"), repairs=1).build(proposal(), _bundle_feature())
    assert res.verdict == "validated" and res.attempts[0]["code"] == "DRAFT_ERROR"
    assert res.attempts[0]["role"] == "tests" and "does not compile" in calls[1]


def test_research_lanes_run_together_and_merge_in_lane_order(tmp_path, py_repo, toy_profile):
    import threading

    from factory.reference.research import Researcher, research_lanes

    class Tools:
        web = None

    assert research_lanes(Tools(), 3) == ["peers", "starter"]  # no search provider: no web lane
    Tools.web = object()
    assert research_lanes(Tools(), 3) == ["peers", "starter", "web"] and research_lanes(Tools(), 1) == ["all"]

    barrier = threading.Barrier(3, timeout=5)  # every lane must be running at the same moment

    class Lanes(Researcher):
        def ideas(self, profile, n, avoid, round_=1, focus="", lane="all"):
            barrier.wait()
            return [{"title": f"{lane} idea"}, {"title": "Shared Idea"}, {"title": "old one"}][: n + 1], [{"tool": lane}]

    ideas, steps = Lanes(None, None).ideas_parallel(toy_profile, 4, ["Old one"], lanes=["peers", "starter", "web"],
                                                    workers=3)
    assert [i["title"] for i in ideas] == ["peers idea", "Shared Idea", "starter idea", "web idea"]
    assert [s["tool"] for s in steps] == ["peers", "starter", "web"]


def test_a_test_file_put_elsewhere_lands_at_the_new_test_path(tmp_path, py_spec):
    llm = FakeLLM()
    llm.test_writer = lambda prompt: {"tests": {"tests/test_pricing.py": "def test_x():\n    assert 1\n"}}
    seen = []

    def validate(draft):
        seen.append(sorted(str(p.relative_to(draft / "tests")) for p in (draft / "tests").rglob("*.py")))
        return _report(), "id"

    res = Builder(SubAgent("author", "a", llm("a")), SubAgent("test_writer", "t", llm("t")), py_spec, tmp_path / "d",
                  validate, repairs=0).build(proposal(), _bundle_feature())
    assert res.verdict == "validated" and seen[0] and "tests/test_pricing.py" not in seen[0]


def test_a_panel_of_models_drafts_cross_reviews_picks_the_best_and_revises_it(tmp_path, py_spec):
    """Three author models and three test-writer models: each drafts, the others review, the best is
    revised with its reviews, and ONE package goes to the gates."""
    base = FakeLLM()
    seen: dict[str, list[str]] = {}

    def make(model):
        def reply(prompt):
            role = base.role(prompt)
            seen.setdefault(f"{role}:{model}", []).append(prompt)
            if role == "author":
                data = base.author(prompt)
                if model == "b":  # b's draft is the one the reviewers like
                    data["requirements"] += "\nImport it as `from toyshop.pricing import line_total`."
                return json.dumps(data)
            if role == "test_writer":
                data = base.test_writer(prompt)
                if model == "c":
                    data["tests"] = {k: v + "\n\ndef test_nine():\n    assert line_total(100, 9) == 900\n"
                                     for k, v in data["tests"].items()}
                return json.dumps(data)
            if role == "draft_review":
                good = "from toyshop.pricing import line_total" in prompt.split("Draft:", 1)[1]
                return json.dumps({"score": 9 if good else 4, "verdict": "fix",
                                   "problems": ["state the 10 percent rounding"], "fixes": ["say half up"]})
            if role == "tests_review":
                good = "test_nine" in prompt
                return json.dumps({"score": 8 if good else 5, "verdict": "ok" if good else "fix", "problems": []})
            return json.dumps(getattr(base, role)(prompt))
        return reply

    authors = [SubAgent("author", m, make(m)) for m in ("a", "b", "c")]
    writers = [SubAgent("test_writer", m, make(m)) for m in ("a", "b", "c")]
    gated = []

    def validate(draft):
        gated.append({"req": (draft / "requirements.md").read_text(),
                      "tests": "".join(p.read_text() for p in (draft / "tests").rglob("*.py"))})
        return _report(), "id"

    res = Builder(authors[0], writers[0], py_spec, tmp_path / "d", validate, repairs=1, authors=authors,
                  test_writers=writers, workers=3).build(proposal(), _bundle_feature())
    assert res.verdict == "validated" and len(gated) == 1  # one package reached the gates
    author_panel, tests_panel = res.panel
    assert author_panel["chosen"] == "b" and author_panel["revised"] is True
    assert tests_panel["chosen"] == "c" and "revised" not in tests_panel  # its reviewers said ok
    assert [d["score"] for d in author_panel["drafts"]] == [4.0, 9.0, 4.0]
    assert "from toyshop.pricing import line_total" in gated[0]["req"] and "test_nine" in gated[0]["tests"]
    # every draft is reviewed by the two OTHER models only
    assert len(seen["draft_review:a"]) == 2 and len(seen["tests_review:b"]) == 2
    revision = seen["author:b"][-1]
    assert "reviewed by other models" in revision and "say half up" in revision
    # independence: draft reviewers see no tests; test reviewers never see the reference change
    assert not any("def test_" in pr for k, v in seen.items() if k.startswith("draft_review") for pr in v)
    assert not any("amount -= (amount * 10" in pr for k, v in seen.items() if k.startswith("tests_review") for pr in v)
    assert res.attempts[0]["models"] == {"author": "b", "tests": "c"} and len(res.attempts[0]["panel"]) == 2


def test_a_panel_survives_a_model_that_fails_and_one_whose_edits_do_not_apply(tmp_path, py_spec):
    from factory.errors import ModelProviderError

    base = FakeLLM()

    def make(model):
        def reply(prompt):
            role = base.role(prompt)
            if model == "quota":
                raise ModelProviderError("quota")
            data = getattr(base, role)(prompt) if role not in {"draft_review", "tests_review"} else \
                {"score": 7, "verdict": "ok"}
            if role == "author" and model == "bad":
                data["edits"][0]["search"] = "not in the file\n"
            return json.dumps(data)
        return reply

    authors = [SubAgent("author", m, make(m)) for m in ("quota", "bad", "good")]
    res = Builder(authors[0], SubAgent("test_writer", "t", make("t")), py_spec, tmp_path / "d",
                  lambda d: (_report(), "id"), repairs=0, authors=authors, workers=3).build(proposal(), _bundle_feature())
    assert res.verdict == "validated" and res.panel[0]["chosen"] == "good"
    errors = {d["model"]: d["error"] for d in res.panel[0]["drafts"]}
    assert errors["quota"].startswith("model:") and "search text" in errors["bad"] and errors["good"] == ""


def test_an_edit_on_lines_an_earlier_edit_changed_says_which_edit_to_merge(py_repo):
    from factory.errors import PackageError
    from factory.generation.author import apply_edits

    text = (py_repo / "toyshop/pricing.py").read_text()
    line = next(ln for ln in text.splitlines() if ln.strip())
    edits = [{"path": "toyshop/pricing.py", "search": line, "replace": line + "  # changed"},
             {"path": "toyshop/pricing.py", "search": line + "\n", "replace": line + "\n"}]
    with pytest.raises(PackageError, match=r"ARE in the original file, but edit 1 already changed them"):
        apply_edits(py_repo, edits)


def test_a_model_that_repeats_the_same_failure_hands_the_repair_to_the_next(tmp_path, py_spec):
    base = FakeLLM()
    used = []

    def make(model):
        def reply(prompt):
            role = base.role(prompt)
            data = getattr(base, role)(prompt) if role not in {"draft_review", "tests_review"} else \
                {"score": 9 if model == "next" else 5, "verdict": "ok"}  # next reviews stuck's draft: 9
            if role == "author":
                used.append(model)
                if model == "stuck" and len(used) > 2:  # its repairs keep the same broken edit
                    data["edits"][0]["search"] = "never there\n"
            return json.dumps(data)
        return reply

    authors = [SubAgent("author", m, make(m)) for m in ("stuck", "next")]
    results = [_report("rejected", RejectCode.REGRESSION, "an existing test failed"), _report()]
    res = Builder(authors[0], SubAgent("test_writer", "t", make("t")), py_spec, tmp_path / "d",
                  lambda d: (results.pop(0), "id"), repairs=3, authors=authors).build(proposal(), _bundle_feature())
    assert res.verdict == "validated"
    assert [a["code"] for a in res.attempts] == ["REGRESSION", "DRAFT_ERROR", "DRAFT_ERROR", "VALID"]
    assert res.attempts[-1]["models"]["author"] == "next"
