"""Evidence snapshots: every research result and every LLM reply, saved so a run replays offline.

Layout::

    <root>/<snapshot_id>/manifest.json         repo, commit, created, mode history
    <root>/<snapshot_id>/<kind>/<hash>.json    {"key": ..., "data": ...}   (github, search, clone, ...)
    <root>/<snapshot_id>/llm/<hash>.json       {"role", "model", "prompt_hash", "reply"}

Modes: ``live`` uses a saved entry when there is one and records new ones (so a rerun with the same
snapshot asks nothing twice); ``refresh`` records again over saved entries; ``replay`` never touches
the network and fails on a missing entry. The judges' environment may block the web: ship the
snapshot folder with the repo and run with ``--replay <snapshot_id>``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Literal

from ..errors import EnvironmentProblem

Mode = Literal["live", "refresh", "replay"]


class ReplayMiss(EnvironmentProblem):
    """Replay mode needed a result the snapshot does not have."""


def key_hash(key) -> str:
    text = key if isinstance(key, str) else json.dumps(key, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class Snapshot:
    def __init__(self, root: Path, snapshot_id: str, mode: Mode = "live") -> None:
        self.root = Path(root) / snapshot_id
        self.id = snapshot_id
        self.mode = mode
        self.hits = 0
        self.misses = 0
        if mode == "replay" and not self.root.is_dir():
            raise ReplayMiss(f"snapshot {snapshot_id} not found under {root}")
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ manifest

    def manifest(self) -> dict:
        p = self.root / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def write_manifest(self, **data) -> None:
        m = self.manifest()
        m.setdefault("created", time.time())
        m.update(data)
        m.setdefault("sessions", []).append({"mode": self.mode, "at": time.time()})
        (self.root / "manifest.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ entries

    def path(self, kind: str, key) -> Path:
        return self.root / kind / f"{key_hash(key)}.json"

    def has(self, kind: str, key) -> bool:
        return self.path(kind, key).exists()

    def get(self, kind: str, key):
        p = self.path(kind, key)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))["data"]

    def put(self, kind: str, key, data) -> None:
        p = self.path(kind, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")  # writers run in parallel
        tmp.write_text(json.dumps({"key": key, "data": data, "at": time.time()}, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(p)

    def fetch(self, kind: str, key, fn: Callable[[], object]):
        """The saved result for ``key``, or ``fn()`` saved (never ``fn()`` in replay mode)."""
        if self.mode != "refresh" and self.has(kind, key):
            self.hits += 1
            return self.get(kind, key)
        if self.mode == "replay":
            raise ReplayMiss(f"replay: snapshot {self.id} has no {kind} entry for {str(key)[:160]}")
        self.misses += 1
        data = fn()
        self.put(kind, key, data)
        return data

    def files(self, kind: str) -> list[Path]:
        d = self.root / kind
        return sorted(d.glob("*.json")) if d.is_dir() else []

    def save_record(self, name: str, data) -> Path:
        """A named result (profile, peers, features): human-readable, beside the raw entries."""
        p = self.root / "records" / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    def load_record(self, name: str):
        p = self.root / "records" / f"{name}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
