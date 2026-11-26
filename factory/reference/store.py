"""MongoDB state for the reference pipeline: runs, research records, proposals, benchmark queue.

MongoDB is the only state store (user choice 2026-11-26). Collections (prefix ``ref_``)::

    ref_runs       one document per factory run (repo, commit, snapshot, policy, models, counts)
    ref_profiles   RepoProfile of the starter repo and of each deep-profiled peer
    ref_peers      PeerRepo candidates with their score breakdown and status
    ref_features   PeerFeature candidates (reference bundles) with their novelty verdict
    ref_proposals  TaskProposal, moved through the state machine (states.py) atomically
    ref_jobs       BenchmarkJob queue: workers lease a job with a deadline and send heartbeats
    ref_results    BenchmarkResult, one per job
    ref_control    the kill switch

Every transition is a compare-and-set on the current state, so two workers (or a worker and the
review CLI) cannot both move the same proposal. A job whose lease ran out (worker killed) is leased
again by the next worker: a restart resumes the queue.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from typing import Iterable

from ..errors import EnvironmentProblem
from .schema import (BenchmarkJob, BenchmarkResult, Edit, JobState, PeerFeature, PeerRepo, ProposalState,
                     RepoProfile, Review, TaskProposal)
from .states import TransitionError, next_state

log = logging.getLogger("factory.reference.store")

ENV = "TASK_FACTORY_MONGO_URI"
DEFAULT_DB = "task_factory"


def _doc(model, key: str) -> dict:
    data = model.model_dump(mode="json")
    data["_id"] = key
    return data


def _strip(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def connect(uri: str | None = None):
    """The database named in ``uri`` (or $TASK_FACTORY_MONGO_URI); fails early when it is unreachable."""
    uri = uri or os.environ.get(ENV, "")
    if not uri:
        raise EnvironmentProblem(
            "the reference pipeline keeps its state in MongoDB: start it (docker compose up -d mongo) "
            f"and set {ENV}=mongodb://127.0.0.1:27017/{DEFAULT_DB}")
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, appname="task-factory")
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise EnvironmentProblem(f"MongoDB at {ENV} is not reachable: {exc}") from exc
    return client.get_default_database(default=DEFAULT_DB)


class ReferenceStore:
    def __init__(self, db) -> None:
        self.db = db
        self.runs = db["ref_runs"]
        self.profiles = db["ref_profiles"]
        self.peers = db["ref_peers"]
        self.features = db["ref_features"]
        self.proposals = db["ref_proposals"]
        self.jobs = db["ref_jobs"]
        self.results = db["ref_results"]
        self.control = db["ref_control"]
        try:
            self.proposals.create_index([("run_id", 1), ("state", 1)])
            self.jobs.create_index([("state", 1), ("not_before", 1), ("created", 1)])
            self.jobs.create_index([("task_id", 1), ("package_checksum", 1)])
            self.results.create_index([("run_id", 1)])
        except Exception as exc:  # noqa: BLE001 - indexes speed things up, they are not required
            log.debug("index creation skipped: %s", exc)

    # ------------------------------------------------------------------ runs

    def save_run(self, run_id: str, data: dict) -> None:
        self.runs.update_one({"_id": run_id}, {"$set": {**data, "run_id": run_id, "updated": time.time()}},
                             upsert=True)

    def get_run(self, run_id: str) -> dict | None:
        return _strip(self.runs.find_one({"_id": run_id}))

    def latest_run(self) -> dict | None:
        docs = list(self.runs.find().sort("updated", -1).limit(1))
        return _strip(docs[0]) if docs else None

    # ------------------------------------------------------------------ research records

    def save_profile(self, run_id: str, role: str, profile: RepoProfile) -> None:
        key = f"{run_id}:{role}:{profile.repo}"
        self.profiles.replace_one({"_id": key}, {**_doc(profile, key), "run_id": run_id, "role": role},
                                  upsert=True)

    def profile(self, run_id: str, repo: str, role: str = "starter") -> RepoProfile | None:
        doc = _strip(self.profiles.find_one({"_id": f"{run_id}:{role}:{repo}"}))
        if doc is None:
            return None
        doc.pop("run_id", None)
        doc.pop("role", None)
        return RepoProfile.model_validate(doc)

    def save_peers(self, run_id: str, peers: Iterable[PeerRepo]) -> None:
        for p in peers:
            key = f"{run_id}:{p.full_name}"
            self.peers.replace_one({"_id": key}, {**_doc(p, key), "run_id": run_id}, upsert=True)

    def list_peers(self, run_id: str) -> list[PeerRepo]:
        out = []
        for d in self.peers.find({"run_id": run_id}):
            d = _strip(d)
            d.pop("run_id", None)
            out.append(PeerRepo.model_validate(d))
        return sorted(out, key=lambda p: -p.similarity.total)

    def save_features(self, run_id: str, features: Iterable[PeerFeature]) -> None:
        for f in features:
            key = f"{run_id}:{f.id}"
            self.features.replace_one({"_id": key}, {**_doc(f, key), "run_id": run_id}, upsert=True)

    def list_features(self, run_id: str, status: str | None = None) -> list[PeerFeature]:
        q = {"run_id": run_id}
        if status:
            q["status"] = status
        out = []
        for d in self.features.find(q):
            d = _strip(d)
            d.pop("run_id", None)
            out.append(PeerFeature.model_validate(d))
        return sorted(out, key=lambda f: (-f.relevance, f.id))

    # ------------------------------------------------------------------ proposals

    def add_proposal(self, p: TaskProposal) -> TaskProposal:
        """Insert a new proposal; an existing one with the same id is kept (replay is idempotent)."""
        existing = self.get_proposal(p.proposal_id)
        if existing is not None:
            return existing
        p = p.model_copy(update={"checksum": p.content_checksum()})
        self.proposals.insert_one(_doc(p, p.proposal_id))
        return p

    def get_proposal(self, proposal_id: str) -> TaskProposal | None:
        doc = _strip(self.proposals.find_one({"_id": proposal_id}))
        return TaskProposal.model_validate(doc) if doc else None

    def find_proposal(self, prefix: str, run_id: str | None = None) -> TaskProposal:
        q: dict = {"_id": {"$regex": "^" + "".join(c for c in prefix if c.isalnum() or c in "-_")}}
        if run_id:
            q["run_id"] = run_id
        docs = list(self.proposals.find(q).limit(2))
        if len(docs) != 1:
            raise TransitionError(f"{'no' if not docs else 'more than one'} proposal matches {prefix!r}")
        return TaskProposal.model_validate(_strip(docs[0]))

    def list_proposals(self, run_id: str | None = None, states: Iterable[ProposalState | str] | None = None
                       ) -> list[TaskProposal]:
        q: dict = {}
        if run_id:
            q["run_id"] = run_id
        if states is not None:
            q["state"] = {"$in": [ProposalState(s).value for s in states]}
        return [TaskProposal.model_validate(_strip(d)) for d in self.proposals.find(q).sort("_id", 1)]

    def transition(self, proposal_id: str, event: str, reason: str = "", by: str = "controller",
                   **fields) -> TaskProposal:
        """Move a proposal on ``event`` (compare-and-set on its current state); extra fields are set too."""
        for _ in range(8):
            current = self.get_proposal(proposal_id)
            if current is None:
                raise TransitionError(f"unknown proposal {proposal_id}")
            to = next_state(current.state, event)
            entry = {"from": current.state.value, "to": to.value, "event": event, "reason": reason, "by": by,
                     "at": time.time()}
            updates = {"state": to.value, **{k: _json(v) for k, v in fields.items()}}
            doc = self.proposals.find_one_and_update(
                {"_id": proposal_id, "state": current.state.value},
                {"$set": updates, "$push": {"history": entry}})
            if doc is not None:
                log.info("proposal %s: %s -[%s]-> %s %s", proposal_id, current.state.value, event, to.value, reason)
                return self.get_proposal(proposal_id)
        raise TransitionError(f"proposal {proposal_id} kept changing; {event} not applied")

    def try_transition(self, proposal_id: str, event: str, reason: str = "", **fields) -> TaskProposal | None:
        try:
            return self.transition(proposal_id, event, reason, **fields)
        except TransitionError:
            return None

    def update_proposal(self, proposal_id: str, **fields) -> None:
        """Set fields that are not state (build attempts, similarity, validation, task id)."""
        if "state" in fields:
            raise TransitionError("state changes go through transition()")
        self.proposals.update_one({"_id": proposal_id}, {"$set": {k: _json(v) for k, v in fields.items()}})

    def add_review(self, review: Review) -> None:
        self.proposals.update_one({"_id": review.proposal_id}, {"$push": {"reviews": review.model_dump(mode="json")}})

    def edit_proposal(self, proposal_id: str, changes: dict, by: str, reason: str) -> TaskProposal:
        """A manual edit: logged, back to DRAFTED (critics check again), older results invalidated."""
        if not reason.strip():
            raise TransitionError("a reason is required for a manual edit")
        allowed = {"title", "kind", "summary", "behaviour_spec", "expected_files", "test_plan", "tags",
                   "difficulty_est"}
        bad = sorted(set(changes) - allowed)
        if bad:
            raise TransitionError(f"these fields cannot be edited: {', '.join(bad)}")
        current = self.get_proposal(proposal_id)
        if current is None:
            raise TransitionError(f"unknown proposal {proposal_id}")
        merged = current.model_copy(update=changes)
        TaskProposal.model_validate(merged.model_dump())  # the edit must still be a valid proposal
        edit = Edit(by=by, reason=reason.strip(), fields=sorted(changes))
        event = "user_edit" if current.state == ProposalState.PENDING_REVIEW else "proposal_edited"
        self.proposals.update_one({"_id": proposal_id}, {"$push": {"edits": edit.model_dump(mode="json")}})
        p = self.transition(proposal_id, event, reason.strip(), by=by, **changes,
                            checksum=merged.content_checksum(), package_checksum=None, task_id=None,
                            validation=None, similarity=None)
        if current.task_id:
            self.invalidate_jobs(current.task_id, keep_checksum=None)
        return p

    # ------------------------------------------------------------------ benchmark queue

    def enqueue(self, job: BenchmarkJob) -> bool:
        """Add a job unless the same (task, package checksum, agent, attempt) is already queued or done."""
        if self.jobs.find_one({"_id": job.job_id}) is not None:
            return False
        self.jobs.insert_one(_doc(job, job.job_id))
        if job.proposal_id:
            self.try_transition(job.proposal_id, "enqueue", f"benchmark {job.agent_id} attempt {job.attempt}")
        return True

    def get_job(self, job_id: str) -> BenchmarkJob | None:
        doc = _strip(self.jobs.find_one({"_id": job_id}))
        return BenchmarkJob.model_validate(doc) if doc else None

    def list_jobs(self, run_id: str | None = None, states: Iterable[JobState | str] | None = None
                  ) -> list[BenchmarkJob]:
        q: dict = {}
        if run_id:
            q["run_id"] = run_id
        if states is not None:
            q["state"] = {"$in": [JobState(s).value for s in states]}
        return [BenchmarkJob.model_validate(_strip(d)) for d in self.jobs.find(q).sort("created", 1)]

    def lease(self, worker: str | None = None, lease_s: float = 120.0, now: float | None = None,
              run_id: str | None = None) -> BenchmarkJob | None:
        """Take the oldest runnable job: queued and due, or running with an expired lease (resume)."""
        now = time.time() if now is None else now
        worker = worker or f"{socket.gethostname()}:{os.getpid()}"
        q: dict = {"$or": [{"state": JobState.QUEUED.value, "not_before": {"$lte": now}},
                           {"state": JobState.RUNNING.value, "lease_until": {"$lt": now}}]}
        if run_id:
            q["run_id"] = run_id
        doc = self.jobs.find_one_and_update(
            q, {"$set": {"state": JobState.RUNNING.value, "worker": worker, "lease_until": now + lease_s},
                "$inc": {"tries": 1}},
            sort=[("created", 1)])
        if doc is None:
            return None
        job = self.get_job(doc["_id"])
        if job.proposal_id:
            self.try_transition(job.proposal_id, "lease", f"{job.agent_id} attempt {job.attempt} on {worker}")
        return job

    def heartbeat(self, job_id: str, worker: str, lease_s: float = 120.0) -> bool:
        r = self.jobs.update_one({"_id": job_id, "worker": worker, "state": JobState.RUNNING.value},
                                 {"$set": {"lease_until": time.time() + lease_s}})
        return r.modified_count == 1

    def complete(self, job_id: str, result: BenchmarkResult) -> None:
        self.results.replace_one({"_id": job_id}, _doc(result, job_id), upsert=True)
        self.jobs.update_one({"_id": job_id}, {"$set": {"state": JobState.DONE.value, "lease_until": None,
                                                        "error": None}})
        self._settle(job_id)

    def infra_error(self, job_id: str, error: str, backoff_s: float = 30.0, result: BenchmarkResult | None = None
                    ) -> JobState:
        """Never a model failure: retried with backoff; after max_retries the job is RUN_FAILED."""
        job = self.get_job(job_id)
        if job is None:
            raise TransitionError(f"unknown job {job_id}")
        if job.tries > job.budget.max_retries:
            if result is not None:
                self.results.replace_one({"_id": job_id}, _doc(result, job_id), upsert=True)
            self.jobs.update_one({"_id": job_id}, {"$set": {"state": JobState.RUN_FAILED.value, "error": error,
                                                            "lease_until": None}})
            self._settle(job_id)
            return JobState.RUN_FAILED
        delay = backoff_s * (2 ** max(job.tries - 1, 0))
        self.jobs.update_one({"_id": job_id}, {"$set": {"state": JobState.QUEUED.value, "error": error,
                                                        "lease_until": None, "not_before": time.time() + delay}})
        if job.proposal_id and not self._active(job, exclude=job_id):
            self.try_transition(job.proposal_id, "infra_retry", error[:200])
        return JobState.QUEUED

    def _active(self, job: BenchmarkJob, exclude: str | None = None) -> int:
        q = {"task_id": job.task_id, "package_checksum": job.package_checksum,
             "state": {"$in": [JobState.QUEUED.value, JobState.RUNNING.value]}}
        if exclude:
            q["_id"] = {"$ne": exclude}
        return self.jobs.count_documents(q)

    def _settle(self, job_id: str) -> None:
        """When no job of the task is left to run, the proposal is BENCHMARKED (or RUN_FAILED)."""
        job = self.get_job(job_id)
        if job is None or not job.proposal_id or self._active(job):
            return
        done = self.jobs.count_documents({"task_id": job.task_id, "package_checksum": job.package_checksum,
                                          "state": JobState.DONE.value})
        self.try_transition(job.proposal_id, "finished" if done else "retries_exhausted",
                            f"{done} job(s) finished")

    def invalidate_jobs(self, task_id: str, keep_checksum: str | None) -> int:
        """Jobs for an older package of the task stop counting (and stop running)."""
        q: dict = {"task_id": task_id, "state": {"$ne": JobState.INVALIDATED.value}}
        if keep_checksum:
            q["package_checksum"] = {"$ne": keep_checksum}
        r = self.jobs.update_many(q, {"$set": {"state": JobState.INVALIDATED.value, "lease_until": None}})
        return r.modified_count

    def list_results(self, run_id: str | None = None, current_only: bool = True) -> list[BenchmarkResult]:
        q = {"run_id": run_id} if run_id else {}
        out = [BenchmarkResult.model_validate(_strip(d)) for d in self.results.find(q)]
        if current_only:
            valid = {d["_id"] for d in self.jobs.find({"state": {"$in": [JobState.DONE.value,
                                                                         JobState.RUN_FAILED.value]}}, {"_id": 1})}
            out = [r for r in out if r.job_id in valid]
        return sorted(out, key=lambda r: (r.task_id, r.agent_id, r.attempt))

    # ------------------------------------------------------------------ kill switch

    def set_kill(self, on: bool, reason: str = "") -> None:
        self.control.update_one({"_id": "kill"}, {"$set": {"on": on, "reason": reason, "at": time.time()}},
                                upsert=True)

    def killed(self) -> bool:
        doc = self.control.find_one({"_id": "kill"})
        return bool(doc and doc.get("on"))


def _json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, ProposalState | JobState):
        return value.value
    return value
