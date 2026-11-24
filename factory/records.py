"""Records in MongoDB, next to the files (dual write).

The factory keeps its records as files in the work folder: review events, generations, harness
attempts and runs, rejections, terminal session links and the SWE miner's outcomes. When
``TASK_FACTORY_MONGO_URI`` is set (docker-compose sets it), every such record is also written to
MongoDB, so a team gets one queryable store across machines and projects. Task packages stay files
(their layout is the deliverable); the ``tasks`` collection is an index of them.

Files stay what the pipeline reads, so nothing depends on MongoDB being up: a failed write logs one
warning and switches MongoDB off for the rest of the process, and ``factory db sync`` fills the
gaps later. Every record has a deterministic ``_id`` (the same record from a live write or from a
sync is one document, never two).

The TypeScript side (SWE miner, terminal plugin) writes through ``python -m factory.records put
<collection>`` (JSON lines on stdin), so it needs no MongoDB driver of its own.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("factory.records")

ENV = "TASK_FACTORY_MONGO_URI"
DEFAULT_DB = "task_factory"
COLLECTIONS = ("tasks", "review_events", "generations", "attempts", "runs", "rejections",
               "session_runs", "session_tasks", "swe_prs", "swe_mining")


def _hash(*parts: str, n: int = 24) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:n]


def workdir_key(workdir: Path | str) -> str:
    """A short stable name for a work folder (records from many projects share one database)."""
    return _hash(str(Path(workdir).resolve()), n=10)


class Records:
    def __init__(self, uri: str | None = None) -> None:
        self.uri = os.environ.get(ENV, "") if uri is None else uri
        self._db = None
        self._off = False

    @property
    def enabled(self) -> bool:
        return bool(self.uri) and not self._off

    def db(self):
        if self._db is None:
            from pymongo import MongoClient

            client = MongoClient(self.uri, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000, appname="task-factory")
            self._db = client.get_default_database(default=DEFAULT_DB)
        return self._db

    def _fail(self, exc: Exception) -> None:
        self._off = True
        log.warning("MongoDB unavailable (%s); records stay in files only. Run `factory db sync` later.",
                    str(exc).splitlines()[0][:200])

    def put_many(self, collection: str, docs: Iterable[dict]) -> int:
        """Upsert documents (each with its ``_id``); returns how many were written (0 when off)."""
        if collection not in COLLECTIONS:
            raise ValueError(f"unknown collection {collection!r}; known: {', '.join(COLLECTIONS)}")
        docs = [d for d in docs if d.get("_id")]
        if not docs or not self.enabled:
            return 0
        from pymongo import ReplaceOne
        from pymongo.errors import PyMongoError

        try:
            self.db()[collection].bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs],
                                             ordered=False)
            return len(docs)
        except PyMongoError as exc:
            self._fail(exc)
            return 0

    def put(self, collection: str, doc: dict) -> bool:
        return self.put_many(collection, [doc]) == 1

    def set_fields(self, collection: str, _id: str, fields: dict) -> None:
        if not self.enabled:
            return
        from pymongo.errors import PyMongoError

        try:
            self.db()[collection].update_one({"_id": _id}, {"$set": fields})
        except PyMongoError as exc:
            self._fail(exc)

    def counts(self) -> dict[str, int]:
        db = self.db()
        return {c: db[c].estimated_document_count() for c in COLLECTIONS}


records = Records()


# --------------------------------------------------------------------------- documents
# One builder per record type, used by the live writers and by sync alike (same _id either way).


def task_doc(pkg: Path, status: str | None = None, store: Path | None = None) -> dict:
    """``store``: the task store the package belongs to (a deleted package sits in its .trash)."""
    meta = tomllib.loads((pkg / "task.toml").read_text(encoding="utf-8"))
    store = Path(store) if store else pkg.parent
    doc: dict[str, Any] = {
        "_id": f"{workdir_key(store)}:{meta['task_id']}", "task_id": meta["task_id"], "store": str(store),
        "package": str(pkg), "repo": meta.get("repo"), "repo_name": meta.get("repo_name"),
        "commit_sha": meta.get("commit_sha"), "language": meta.get("language"), "kind": meta.get("kind"),
        "tags": meta.get("tags", []), "touched_files": meta.get("touched_files", []),
        "fail_to_pass": len(meta.get("fail_to_pass", [])), "pass_to_pass": len(meta.get("pass_to_pass", [])),
        "generated_by": meta.get("generated_by", {}), "validation": meta.get("validation", {}),
        "source": "swe" if (pkg / "swe.json").exists() else "gated",
    }
    if status:
        doc["status"] = status
    swe = pkg / "swe.json"
    if swe.exists():
        data = json.loads(swe.read_text(encoding="utf-8"))
        doc["swe"] = {k: data.get(k) for k in ("instance_id", "pull_request", "issues", "base_commit",
                                                 "created_at", "merged_at", "quality")}
    return doc


def event_doc(store: Path, line: str) -> dict:
    event = json.loads(line)
    return {"_id": _hash(str(Path(store).resolve()), line.strip()), "store": str(store), **event}


def generation_doc(workdir: Path, line: str) -> dict:
    return {"_id": _hash(str(Path(workdir).resolve()), line.strip()), "workdir": str(workdir), **json.loads(line)}


def attempt_doc(workdir: Path, run_id: str, row: dict) -> dict:
    key = f"{workdir_key(workdir)}:{run_id}/{row['task_id']}/{row.get('attempt', 1)}"
    return {"_id": key, "workdir": str(workdir), "run_id": run_id, **row}


def run_doc(workdir: Path, run_id: str, summary: dict) -> dict:
    return {"_id": f"{workdir_key(workdir)}:{run_id}", "workdir": str(workdir), "run_id": run_id, **summary}


def line_doc(workdir: Path, line: str) -> dict:
    """Any other JSON-lines record (rejections, session links): keyed by its content."""
    return {"_id": _hash(str(Path(workdir).resolve()), line.strip()), "workdir": str(workdir), **json.loads(line)}


# --------------------------------------------------------------------------- sync (files -> MongoDB)


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            json.loads(line)
            out.append(line)
        except ValueError:
            continue  # a half-written line
    return out


def _status(store: Path) -> dict[str, str]:
    after = {"add": "pending", "restore": "pending", "edit": "pending", "approve": "approved", "delete": "deleted"}
    status: dict[str, str] = {}
    for line in _lines(store / ".review" / "log.jsonl"):
        e = json.loads(line)
        if e.get("action") in after:
            status[e["task_id"]] = after[e["action"]]
    return status


def sync_workdir(workdir: Path, rec: Records = records) -> dict[str, int]:
    """Write every record of one work folder (a project's, or a SWE store) to MongoDB."""
    wd = Path(workdir)
    n: dict[str, int] = {}

    def put(collection: str, docs: list[dict]) -> None:
        if docs:
            n[collection] = n.get(collection, 0) + rec.put_many(collection, docs)

    store = wd / "tasks"
    if store.is_dir():
        status = _status(store)
        put("tasks", [task_doc(p, "deleted" if p.parent.name == ".trash" else status.get(p.name, "pending"), store)
                      for p in [*sorted(store.iterdir()), *sorted((store / ".trash").glob("*"))]
                      if p.is_dir() and (p / "task.toml").exists()])
        put("review_events", [event_doc(store, ln) for ln in _lines(store / ".review" / "log.jsonl")])
    put("generations", [generation_doc(wd, ln) for ln in _lines(wd / "generations.jsonl")])
    put("rejections", [line_doc(wd, ln) for ln in _lines(wd / "rejections.jsonl")])
    put("session_runs", [line_doc(wd, ln) for ln in _lines(wd / "session-runs.jsonl")])
    put("session_tasks", [line_doc(wd, ln) for ln in _lines(wd / "session-tasks.jsonl")])
    harness = wd / "harness"
    if harness.is_dir():
        for run in sorted(p for p in harness.iterdir() if (p / "results.jsonl").exists()):
            put("attempts", [attempt_doc(wd, run.name, json.loads(ln)) for ln in _lines(run / "results.jsonl")])
            if (run / "summary.json").exists():
                put("runs", [run_doc(wd, run.name, json.loads((run / "summary.json").read_text(encoding="utf-8")))])
    prs = wd / "prs"
    if prs.is_dir():
        put("swe_prs", [{"_id": f"{workdir_key(wd)}:pr-{p.stem}", "workdir": str(wd),
                         **json.loads(p.read_text(encoding="utf-8"))} for p in sorted(prs.glob("*.json"))])
    if (wd / "mined.json").exists():
        put("swe_mining", [{"_id": f"{workdir_key(wd)}:mined", "workdir": str(wd),
                            **json.loads((wd / "mined.json").read_text(encoding="utf-8"))}])
    return n


def default_workdirs() -> list[Path]:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "task-factory"
    dirs = [p for group in ("projects", "swe") for p in sorted((state / group).glob("*")) if p.is_dir()]
    return dirs + ([Path("workdir")] if Path("workdir").is_dir() else [])


def main(argv: list[str] | None = None) -> int:
    """``python -m factory.records put <collection>``: upsert JSON lines from stdin (the TS shim)."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "put":
        written = records.put_many(args[1], [json.loads(ln) for ln in sys.stdin if ln.strip()])
        print(json.dumps({"written": written, "enabled": records.enabled}))
        return 0
    if len(args) == 2 and args[0] == "sync":  # one work folder, e.g. a SWE store after mining
        print(json.dumps({"written": sync_workdir(Path(args[1])), "enabled": records.enabled}))
        return 0
    print("usage: python -m factory.records put <collection> (JSON lines with _id on stdin) | sync <workdir>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
