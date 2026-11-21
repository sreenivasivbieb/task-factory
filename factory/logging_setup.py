"""Structured JSONL log in ``workdir/logs`` plus a readable console summary."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _CurrentStderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is now (survives test capture and redirection)."""

    @property
    def stream(self):
        import sys

        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def setup_logging(workdir: Path, verbose: bool = False) -> Path:
    """Attach a JSONL file handler and a console handler to the ``factory`` logger."""
    log_dir = Path(workdir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    root = logging.getLogger("factory")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(_JsonlFormatter())
    fh.setLevel(logging.DEBUG)
    ch = _CurrentStderrHandler()
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)
    return log_path
