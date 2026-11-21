"""Test report parsers. Each returns a list of TestResult with stable IDs.

IDs look like ``path/to/test_file.py::test_name`` (file-based frameworks) or
``package::TestName`` (Go) or ``Class::method`` (JUnit without a file attribute).
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET

from ...errors import ParseError
from ...models import TestResult


def _rel(path: str, roots: list[str]) -> str:
    """Make an absolute path relative to the first matching repo root."""
    if not os.path.isabs(path):
        return path.replace("\\", "/")
    for root in roots:
        root = root.rstrip("/")
        for candidate in {root, os.path.realpath(root)}:
            if path.startswith(candidate + "/"):
                return path[len(candidate) + 1:]
        real = os.path.realpath(path)
        if real.startswith(os.path.realpath(root) + "/"):
            return real[len(os.path.realpath(root)) + 1:]
    return path.lstrip("/")


# --------------------------------------------------------------------------- JUnit XML


def parse_junit(text: str, roots: list[str] | None = None) -> list[TestResult]:
    """Generic JUnit XML (pytest xunit1, node:test, Maven Surefire, Gradle, Jest-junit)."""
    roots = roots or ["/repo"]
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseError(f"invalid JUnit XML: {exc}") from exc
    results: list[TestResult] = []
    for case in root.iter("testcase"):
        name = case.get("name", "")
        classname = case.get("classname", "") or ""
        file = case.get("file")
        if file:
            rel = _rel(file, roots)
            module = rel.rsplit(".", 1)[0].replace("/", ".")
            parts = [rel]
            if classname.startswith(module + "."):
                parts.append(classname[len(module) + 1:].replace(".", "::"))
            parts.append(name)
            test_id = "::".join(parts)
        else:
            test_id = f"{classname}::{name}" if classname else name
        tags = {child.tag for child in case}
        if "error" in tags:
            status = "error"
        elif "failure" in tags:
            status = "failed"
        elif "skipped" in tags:
            status = "skipped"
        else:
            status = "passed"
        try:
            duration = float(case.get("time") or 0.0)
        except ValueError:
            duration = 0.0
        results.append(TestResult(test_id=test_id, status=status, duration_s=round(duration, 4)))
    return _dedupe(results)


# --------------------------------------------------------------------------- Jest JSON

_JEST_STATUS = {"passed": "passed", "failed": "failed", "pending": "skipped", "skipped": "skipped",
                "todo": "skipped", "disabled": "skipped"}


def parse_jest_json(text: str, roots: list[str] | None = None) -> list[TestResult]:
    roots = roots or ["/repo"]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid Jest JSON: {exc}") from exc
    results: list[TestResult] = []
    for suite in data.get("testResults", []):
        rel = _rel(suite.get("name", ""), roots)
        cases = suite.get("assertionResults") or []
        if not cases and suite.get("status") == "failed":
            results.append(TestResult(test_id=f"{rel}::<suite>", status="error"))
        for case in cases:
            name = case.get("fullName") or case.get("title", "")
            status = _JEST_STATUS.get(case.get("status", ""), "error")
            duration = (case.get("duration") or 0) / 1000.0
            results.append(TestResult(test_id=f"{rel}::{name}", status=status, duration_s=duration))
    return _dedupe(results)


# --------------------------------------------------------------------------- go test -json


def parse_go_json(text: str, roots: list[str] | None = None) -> list[TestResult]:
    final: dict[str, TestResult] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        action, test, pkg = ev.get("Action"), ev.get("Test"), ev.get("Package", "")
        if not test or action not in ("pass", "fail", "skip"):
            continue
        status = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
        test_id = f"{pkg}::{test}"
        final[test_id] = TestResult(test_id=test_id, status=status, duration_s=float(ev.get("Elapsed") or 0))
    return list(final.values())


# --------------------------------------------------------------------------- cargo test (libtest text)


def parse_cargo_text(text: str, roots: list[str] | None = None) -> list[TestResult]:
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("test ") or " ... " not in line:
            continue
        name, outcome = line[5:].rsplit(" ... ", 1)
        status = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}.get(outcome.split()[0], None)
        if status:
            results.append(TestResult(test_id=name.strip(), status=status))
    return _dedupe(results)


def _dedupe(results: list[TestResult]) -> list[TestResult]:
    """Keep IDs unique: a repeated ID gets a ``[n]`` suffix (e.g. same test name twice in a file)."""
    seen: dict[str, int] = {}
    out = []
    for r in results:
        n = seen.get(r.test_id, 0)
        seen[r.test_id] = n + 1
        out.append(r if n == 0 else r.model_copy(update={"test_id": f"{r.test_id}[{n + 1}]"}))
    return out


PARSERS = {
    "junit": parse_junit,
    "jest-json": parse_jest_json,
    "go-json": parse_go_json,
    "cargo-text": parse_cargo_text,
}

# Formats whose report is the command's stdout instead of a file.
STDOUT_FORMATS = {"go-json", "cargo-text"}
