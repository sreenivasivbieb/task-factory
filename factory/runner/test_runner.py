"""Run a repo's tests inside a sandbox and return per-test results (section 9)."""

from __future__ import annotations

import json
import logging
import posixpath
import shlex
from pathlib import Path

from ..errors import ParseError
from ..models import EnvConfig, ExecResult, RunSummary
from ..sandbox.base import Sandbox
from .parsers import PARSERS, STDOUT_FORMATS

log = logging.getLogger("factory.runner")

def _file_part(item: str) -> str:
    return item.split("::", 1)[0]


def render_select(env: EnvConfig, select: list[str] | None) -> str:
    """Turn test IDs or file paths into command-line selectors, as ``env.select`` says."""
    if not select:
        return env.select_all
    if env.select == "ids":
        return " ".join(shlex.quote(s) for s in select)
    files = dict.fromkeys(_file_part(s) for s in select)
    if env.select == "files":
        return " ".join(shlex.quote(f) for f in files)
    if env.select == "dirs":
        dirs = dict.fromkeys("./" + (posixpath.dirname(f) or ".") for f in files)
        return " ".join(shlex.quote(d) for d in dirs)
    return env.select_all  # "none": run everything, keep the wanted results


def build_command(env: EnvConfig, select: list[str] | None = None) -> str:
    cmd = env.test_command.replace("{report}", shlex.quote(env.report_path))
    return cmd.replace("{select}", render_select(env, select)).strip()


def _matches(test_id: str, select: list[str]) -> bool:
    for s in select:
        if test_id == s or test_id.startswith(s + "::") or _file_part(test_id) == s:
            return True
    return False


def _report_is_dir(env: EnvConfig) -> bool:
    return env.report_path.endswith("/") or "." not in posixpath.basename(env.report_path)


def _read_report(sb: Sandbox, env: EnvConfig, result: ExecResult) -> list[str]:
    if env.report_format in STDOUT_FORMATS:
        return [result.stdout] if result.stdout.strip() else []
    if _report_is_dir(env):
        ls = sb.exec(["sh", "-c", f"ls -1 {shlex.quote(env.report_path.rstrip('/'))}/*.xml 2>/dev/null"],
                     timeout_s=30)
        paths = [p for p in ls.stdout.splitlines() if p.strip()]
    else:
        paths = [env.report_path]
    texts = []
    for p in paths:
        try:
            texts.append(sb.read_file(p).decode("utf-8", errors="replace"))
        except FileNotFoundError:
            continue
    return texts


def run_tests(sb: Sandbox, env: EnvConfig, select: list[str] | None = None,
              log_to: Path | None = None) -> RunSummary:
    """Run the test command once. ``select`` = test IDs or test file paths (None = full suite).

    A non-zero exit with zero parsed tests is reported as a crash (``error``),
    never as all passed.
    """
    target = env.report_path.rstrip("/")
    sb.exec(["sh", "-c", f"rm -rf {shlex.quote(target)}; mkdir -p {shlex.quote(posixpath.dirname(target) or '.')}"],
            timeout_s=30)
    for path, content in env.helper_files.items():
        sb.write_file(path, content)
    cmd = build_command(env, select)
    result = sb.exec(["sh", "-c", cmd], timeout_s=env.test_timeout_s, env=env.env_vars or None)
    texts = _read_report(sb, env, result)
    parser = PARSERS[env.report_format]
    roots = [getattr(sb, "repo_root", env.workdir), env.workdir]
    results = []
    parse_error = None
    for text in texts:
        try:
            results.extend(parser(text, roots))
        except ParseError as exc:
            parse_error = str(exc)
    if select and env.select in ("ids", "files"):  # the IDs then name the selected files
        results = [r for r in results if _matches(r.test_id, select)]
    error = None
    if result.timed_out:
        error = f"test command timed out after {env.test_timeout_s}s"
    elif not results and result.exit_code != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-2000:]
        error = f"exit {result.exit_code} with no parsed tests (build or collection failure): {tail}"
    elif not results and parse_error:
        error = parse_error
    elif not results and not texts:
        error = "test command produced no report"
    summary = RunSummary.build(results, result.exit_code, timed_out=result.timed_out, error=error)
    if log_to is not None:
        _write_log(log_to, cmd, result, texts, summary)
    log.debug("tests: %s", summary.counts, extra={"data": {"cmd": cmd, "counts": summary.counts,
                                                           "duration_s": result.duration_s}})
    return summary


def _write_log(path: Path, cmd: str, result: ExecResult, reports: list[str], summary: RunSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"command": cmd, "exec": result.model_dump(), "reports": reports, "summary": summary.model_dump()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def flaky_ids(summaries: list[RunSummary]) -> set[str]:
    """Tests whose status differs across runs (missing in a run counts as a different status)."""
    all_ids = set().union(*(s.status_of().keys() for s in summaries)) if summaries else set()
    flaky = set()
    for tid in all_ids:
        statuses = {s.status_of().get(tid, "missing") for s in summaries}
        if len(statuses) > 1:
            flaky.add(tid)
    return flaky
