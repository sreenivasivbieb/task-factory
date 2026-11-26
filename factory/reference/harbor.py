"""P9 validation through Harbor (v2.2 section 12) and the Harbor runner the benchmark reuses.

Official checks: ``harbor run -p <task> -a nop`` must give 0.0 and ``-a oracle`` 1.0. On top: a
stability check (the oracle again, same reward every time) and format checks (exactly the five
official parts, a 40-character base_commit, valid JSON test lists, a Dockerfile, no network or
credentials in the tests). Mutation testing (G6) already ran in our own gates before export.

A null reward is an environment crash, never a verdict: it is retried, then reported as an
infrastructure error.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import EnvironmentProblem

HARBOR_VERSION = "0.25.*"
OFFICIAL_PARTS = {"instruction.md", "solution", "tests", "environment", "task.toml"}

# Things a hidden test must never do: reach the network or read real credentials.
_NETWORK = re.compile(
    r"https?://(?!localhost|127\.0\.0\.1|example\.(?:com|org)|\[::1\])|\brequests\.(?:get|post|put|delete|head)\(|"
    r"\burllib\.request\b|\bsocket\.(?:create_connection|socket)\(|\bhttpx\.|\baiohttp\b|\bfetch\(\s*['\"`]https?:|"
    r"\b(?:curl|wget)\s+https?:")
_CREDENTIALS = re.compile(
    r"(?:os\.environ|process\.env|getenv)\W{0,3}['\"]?[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)|"
    r"\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{30,}|\bsk-[A-Za-z0-9]{20,}")


def harbor_command() -> list[str]:
    """How to start Harbor: $HARBOR_BIN, ``harbor`` on PATH, or ``uvx --from harbor==0.25.* harbor``."""
    env = os.environ.get("HARBOR_BIN")
    if env:
        return shlex.split(env)
    found = shutil.which("harbor")
    if found:
        return [found]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--from", f"harbor=={HARBOR_VERSION}", "harbor"]
    raise EnvironmentProblem(f'Harbor CLI not found: pip install "harbor=={HARBOR_VERSION}" (or set HARBOR_BIN)')


def read_reward(out: Path) -> float | None:
    """The verifier's reward of the newest trial under ``out``; None = no reward (a crash)."""
    files = sorted(Path(out).glob("**/verifier/reward.txt"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        return float(files[-1].read_text().strip())
    except ValueError:
        return None


_COUNTS = re.compile(r"\[verifier\] reward [0-9.]+ \((?:(\d+) of (\d+)|all (\d+)) feature tests passed\)")


def feature_counts(out: Path) -> tuple[int | None, int | None]:
    """(feature tests passed, total) from the line our verifier prints (tests/test.sh)."""
    files = sorted((p for p in Path(out).glob("**/verifier/*") if p.is_file() and p.suffix in {".txt", ".log", ""}),
                   key=lambda p: p.stat().st_mtime)
    for f in reversed(files):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = None
        for m in _COUNTS.finditer(text):
            pass
        if m:
            if m.group(3):
                return int(m.group(3)), int(m.group(3))
            return int(m.group(1)), int(m.group(2))
    return None, None


@dataclass
class HarborRun:
    agent: str
    reward: float | None
    exit_code: int | None
    seconds: float
    logs: str
    command: str
    model: str | None = None
    feature_passed: int | None = None
    feature_total: int | None = None


def run_harbor(task: Path, agent: str, out: Path, model: str | None = None, timeout_s: int = 3600,
               extra: list[str] | None = None) -> HarborRun:
    """One ``harbor run`` of one agent on one task folder."""
    cmd = [*harbor_command(), "run", "-p", str(task), "-a", agent, "-k", "1", "-o", str(out), "--no-delete", "-y"]
    if model:
        cmd += ["-m", model]
    cmd += extra or []
    start = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        code = r.returncode
        (out / "harbor.log").write_text((r.stdout or "")[-200_000:] + "\n--- stderr ---\n" + (r.stderr or "")[-50_000:],
                                        encoding="utf-8")
    except subprocess.TimeoutExpired:
        code = None
    passed, total = feature_counts(out)
    return HarborRun(agent, read_reward(out), code, round(time.monotonic() - start, 1), str(out), " ".join(cmd),
                     model, passed, total)


# --------------------------------------------------------------------------- format checks


def format_problems(task: Path) -> list[str]:
    task = Path(task)
    problems = []
    parts = {p.name for p in task.iterdir()} if task.is_dir() else set()
    missing, extra = OFFICIAL_PARTS - parts, parts - OFFICIAL_PARTS
    if missing:
        problems.append(f"missing official parts: {', '.join(sorted(missing))}")
    if extra:
        problems.append(f"extra entries in the task folder (keep exactly the five parts): {', '.join(sorted(extra))}")
    try:
        meta = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problems.append(f"task.toml unreadable: {exc}")
        meta = {}
    base = meta.get("base_commit") or meta.get("metadata", {}).get("base_commit")
    if not (isinstance(base, str) and re.fullmatch(r"[0-9a-f]{40}", base)):
        problems.append(f"base_commit must be a full 40-character hash, got {base!r}")
    for name in ("fail_to_pass.json", "pass_to_pass.json"):
        try:
            data = json.loads((task / "tests" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"tests/{name} unreadable: {exc}")
            continue
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            problems.append(f"tests/{name} must be a JSON list of test ids")
        elif name == "fail_to_pass.json" and not data:
            problems.append("tests/fail_to_pass.json is empty")
    for rel in ("environment/Dockerfile", "tests/test.sh", "solution/solve.sh", "instruction.md"):
        if not (task / rel).is_file():
            problems.append(f"{rel} is missing")
    files = task / "tests" / "files"
    for p in sorted(files.rglob("*")) if files.is_dir() else []:
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(files).as_posix()
        m = _NETWORK.search(text)
        if m:
            problems.append(f"hidden test {rel} reaches the network ({m.group(0)[:40]!r})")
        m = _CREDENTIALS.search(text)
        if m:
            problems.append(f"hidden test {rel} uses credentials ({m.group(0)[:40]!r})")
    return problems


# --------------------------------------------------------------------------- validator


@dataclass
class HarborReport:
    task_id: str
    format_problems: list[str] = field(default_factory=list)
    nop: HarborRun | None = None
    oracle: list[HarborRun] = field(default_factory=list)
    stable: bool = False
    ok: bool = False
    infra_error: str | None = None
    harbor_version: str | None = None

    def to_dict(self) -> dict:
        def run(r):
            return None if r is None else {"reward": r.reward, "exit_code": r.exit_code, "seconds": r.seconds,
                                           "logs": r.logs, "command": r.command}
        return {"task_id": self.task_id, "ok": self.ok, "stable": self.stable, "infra_error": self.infra_error,
                "format_problems": self.format_problems, "nop": run(self.nop),
                "oracle": [run(r) for r in self.oracle], "harbor_version": self.harbor_version}

    def reason(self) -> str:
        if self.format_problems:
            return "format: " + "; ".join(self.format_problems)
        if self.infra_error:
            return f"infrastructure: {self.infra_error}"
        if self.nop and self.nop.reward != 0.0:
            return f"nop reward {self.nop.reward} (expected 0.0): the tests pass without the feature"
        bad = [r.reward for r in self.oracle if r.reward != 1.0]
        if bad:
            return f"oracle reward {bad[0]} (expected 1.0): the reference solution does not pass"
        if not self.stable:
            return f"oracle rewards differ between runs: {[r.reward for r in self.oracle]}"
        return "ok"


class HarborValidator:
    """nop once, oracle ``repeats`` times; null rewards are retried ``retries`` times."""

    def __init__(self, out_root: Path, repeats: int = 2, retries: int = 1, runner=None, timeout_s: int = 3600):
        self.out_root = Path(out_root)
        self.repeats = max(1, repeats)
        self.retries = retries
        self.runner = runner or run_harbor
        self.timeout_s = timeout_s

    def version(self) -> str | None:
        if self.runner is not run_harbor:
            return None
        try:
            r = subprocess.run([*harbor_command(), "--version"], capture_output=True, text=True, timeout=300)
            return r.stdout.strip() or None
        except (OSError, subprocess.SubprocessError, EnvironmentProblem):
            return None

    def _run(self, task: Path, agent: str, label: str) -> HarborRun:
        run = None
        for i in range(self.retries + 1):
            out = self.out_root / task.name / f"{label}-{i}" if i else self.out_root / task.name / label
            run = self.runner(task, agent, out, timeout_s=self.timeout_s)
            if run.reward is not None:
                return run
        return run

    def validate(self, task: Path) -> HarborReport:
        task = Path(task)
        rep = HarborReport(task.name, format_problems=format_problems(task))
        if rep.format_problems:
            return rep
        rep.nop = self._run(task, "nop", "nop")
        if rep.nop.reward is None:
            rep.infra_error = f"nop gave no reward (exit {rep.nop.exit_code}); logs: {rep.nop.logs}"
            return rep
        for i in range(self.repeats):
            r = self._run(task, "oracle", f"oracle-{i + 1}")
            rep.oracle.append(r)
            if r.reward is None:
                rep.infra_error = f"oracle gave no reward (exit {r.exit_code}); logs: {r.logs}"
                return rep
        rewards = {r.reward for r in rep.oracle}
        rep.stable = len(rewards) == 1
        rep.ok = rep.nop.reward == 0.0 and rewards == {1.0}
        return rep
