"""Records of the reference-guided pipeline (v2.2 section 11).

Every record is a pydantic model; the MongoDB store keeps them as documents (``store.py``) and the
evidence snapshot keeps the research ones as JSON files. ``kind`` uses the spelling the rest of the
factory uses (``behavior_change``).
"""
from __future__ import annotations

import hashlib
import json
import time
from enum import Enum
from typing import Literal

from pydantic import Field

from ..models import Model

ProposalKind = Literal["feature", "behavior_change"]


def stable_id(*parts: str, n: int = 12) -> str:
    """A short id that depends only on its inputs (same repo, commit and snapshot: same ids)."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:n]


def checksum(data) -> str:
    """Checksum of a JSON-able value; a changed proposal or package invalidates older results."""
    text = data if isinstance(data, str) else json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# =========================================================================== P1 · repo understanding


class Capability(Model):
    """One user-facing feature written as a behaviour, with the files that show it."""

    name: str
    behaviour: str
    evidence: list[str] = Field(default_factory=list)  # "path" or "path:symbol"
    verified: bool | None = None  # spot check: True ok, False failed, None not sampled


class Ecosystem(Model):
    deps: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class SpotCheck(Model):
    checked: int = 0
    passed: int = 0
    failed: list[str] = Field(default_factory=list)  # capability names whose evidence did not hold


class RepoProfile(Model):
    repo: str  # owner/name or a local name
    url: str | None = None
    commit: str | None = None
    purpose: str = ""
    language: str | None = None
    test_runner: str | None = None
    capability_map: list[Capability] = Field(default_factory=list)
    design_traits: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    ecosystem: Ecosystem = Field(default_factory=Ecosystem)
    spot_check: SpotCheck = Field(default_factory=SpotCheck)
    model: str | None = None


# =========================================================================== P2 · peers


class PeerSimilarity(Model):
    capability_overlap: float = 0.0
    design_fit: float = 0.0
    maturity: float = 0.0
    ecosystem_fit: float = 0.0
    total: float = 0.0


PeerStatus = Literal["candidate", "cut", "shortlisted", "selected", "not_selected"]


class PeerRepo(Model):
    full_name: str  # owner/name
    url: str
    licence: str | None = None
    language: str | None = None
    last_commit: str | None = None  # ISO date of the last push
    has_tests: bool = False
    stars: int = 0
    archived: bool = False
    fork: bool = False
    default_branch: str | None = None
    description: str = ""  # GitHub description + topics (cheap relevance before deep profiling)
    found_by: list[str] = Field(default_factory=list)  # the recall routes that found it
    similarity: PeerSimilarity = Field(default_factory=PeerSimilarity)
    reasons: list[str] = Field(default_factory=list)
    gap: list[Capability] = Field(default_factory=list)  # the peer has it, the starter repo lacks it
    status: PeerStatus = "candidate"
    manual: bool = False  # chosen by a manual override in factory.toml
    role: str | None = None  # the scout's classification: alternative | relative | dependent | unrelated
    role_reason: str = ""
    context: float | None = None  # how close its purpose and domain are to the starter's (0-1, from README + description)


# =========================================================================== P3 · reference bundles


class PullRef(Model):
    number: int
    title: str
    url: str
    body: str = ""
    merged_at: str | None = None
    labels: list[str] = Field(default_factory=list)
    merge_commit: str | None = None
    base_commit: str | None = None


class ReferenceBundle(Model):
    """The whole story of one peer change. Lives in the snapshot, never in a task folder."""

    pr: PullRef
    issue: dict | None = None  # {"number", "title", "body", "url"}
    discussion: list[str] = Field(default_factory=list)  # review comments, trimmed
    impl_diff: str = ""  # source changes only
    tests: str = ""  # the PR's test changes
    neighbour_tests: dict[str, str] = Field(default_factory=dict)  # peer test file -> trimmed text
    followup_fixes: list[dict] = Field(default_factory=list)  # {"number", "title", "url", "diff"}
    docs: str = ""  # docs and changelog changes
    truncated: list[str] = Field(default_factory=list)  # parts cut to the size limits


class Novelty(Model):
    in_repo: bool = False
    in_upstream: bool = False
    evidence: list[str] = Field(default_factory=list)
    checked_by: list[str] = Field(default_factory=list)  # "capability_map", "symbols", "upstream_refs", ...

    @property
    def novel(self) -> bool:
        return not (self.in_repo or self.in_upstream)


class PeerFeature(Model):
    id: str
    peer_repo: str
    pr_url: str
    issue_url: str | None = None
    summary: str
    behaviour_pattern: str = ""  # the behaviour in one line, written for the starter repo's domain
    gap_capability: str | None = None  # the capability from the gap list this PR implements
    new_names: list[str] = Field(default_factory=list)  # public names the peer change adds
    reference_bundle: ReferenceBundle
    licence: str | None = None
    language: str | None = None
    snapshot_id: str
    relevance: float = 0.0
    novelty: Novelty | None = None
    status: Literal["candidate", "rejected", "used"] = "candidate"
    source: Literal["pr", "capability", "doc"] = "pr"  # a peer PR, a gap capability, or a peer doc (README, design)


# =========================================================================== P5-P7 · proposals


class ProposalState(str, Enum):
    DRAFTED = "DRAFTED"
    CHECKED = "CHECKED"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    BUILDING = "BUILDING"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    BENCHMARKED = "BENCHMARKED"
    REJECTED = "REJECTED"
    MERGED = "MERGED"
    DROPPED = "DROPPED"
    RUN_FAILED = "RUN_FAILED"


class Provenance(Model):
    feature_id: str
    peer_repo: str
    pr_url: str
    issue_url: str | None = None
    licence: str | None = None
    attribution: str = ""


Verdict = Literal["pass", "objection", "approve", "reject", "edit"]


class Review(Model):
    proposal_id: str
    reviewer: str  # "critic:<model>#<n>", "check:<name>", "user:<name>" or "policy:auto-approve"
    verdict: Verdict
    objections: list[str] = Field(default_factory=list)
    hard: bool = False  # a hard objection rejects the proposal
    reason: str = ""
    edge_cases: list[str] = Field(default_factory=list)  # critics: edge cases missing from the test plan
    timestamp: float = Field(default_factory=time.time)


class Edit(Model):
    by: str
    reason: str
    fields: list[str] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)


class Similarity(Model):
    """Our patch and tests compared with the peer's diff and tests (0 = unrelated, 1 = copy)."""

    patch: float = 0.0
    tests: float = 0.0
    flagged: bool = False


class TaskProposal(Model):
    proposal_id: str
    run_id: str
    title: str
    kind: ProposalKind = "feature"
    summary: str = ""
    behaviour_spec: str = ""  # the behaviour adapted to the starter repo (Markdown)
    provenance: list[Provenance] = Field(default_factory=list)
    novelty: Novelty = Field(default_factory=Novelty)
    expected_files: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    edge_cases_from_peer: list[str] = Field(default_factory=list)
    difficulty_est: Literal["easy", "medium", "hard"] = "medium"
    tags: list[str] = Field(default_factory=list)
    state: ProposalState = ProposalState.DRAFTED
    reviews: list[Review] = Field(default_factory=list)
    edits: list[Edit] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)  # {"from", "to", "event", "reason", "at"}
    drafter: str | None = None
    approved_by: str | None = None  # "policy" or "user:<name>"
    merged_into: str | None = None
    task_id: str | None = None
    package_checksum: str | None = None
    build_attempts: list[dict] = Field(default_factory=list)
    similarity: Similarity | None = None
    validation: dict | None = None  # gates + Harbor rewards summary
    checksum: str | None = None
    idea: dict | None = None  # the research agent's idea as proposed (kept so a resumed run can judge it)

    def content_checksum(self) -> str:
        return checksum({"title": self.title, "kind": self.kind, "summary": self.summary,
                         "behaviour_spec": self.behaviour_spec, "expected_files": self.expected_files,
                         "test_plan": self.test_plan})


# =========================================================================== P10 · benchmark


class JobState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    RUN_FAILED = "RUN_FAILED"
    INVALIDATED = "INVALIDATED"  # the package changed after the job was made


class JobBudget(Model):
    timeout_s: int = 3600
    max_retries: int = 3
    cost_cap_usd: float | None = None


class BenchmarkJob(Model):
    job_id: str
    run_id: str
    task_id: str
    proposal_id: str | None = None
    task_dir: str  # the Harbor task folder
    package_dir: str | None = None  # the task store package (harness backend)
    package_checksum: str
    agent_id: str  # "<agent>" or "<agent>:<model>"
    attempt: int = 1
    state: JobState = JobState.QUEUED
    budget: JobBudget = Field(default_factory=JobBudget)
    tries: int = 0
    worker: str | None = None
    lease_until: float | None = None
    not_before: float = 0.0
    error: str | None = None
    created: float = Field(default_factory=time.time)


Outcome = Literal["solved", "partial", "failed", "infra_error"]


class BenchmarkResult(Model):
    job_id: str
    run_id: str
    task_id: str
    agent_id: str
    attempt: int
    package_checksum: str
    reward: float | None = None
    outcome: Outcome
    feature_tests_passed: int | None = None
    feature_tests_total: int | None = None
    cost: float | None = None
    wall_time_s: float = 0.0
    logs: str | None = None
    finished: float = Field(default_factory=time.time)


def outcome_of(reward: float | None) -> Outcome:
    """The guide's reward rule read back: 1.0 solved, 0.0 failed, between partial, null a crash."""
    if reward is None:
        return "infra_error"
    if reward >= 1.0:
        return "solved"
    if reward <= 0.0:
        return "failed"
    return "partial"
