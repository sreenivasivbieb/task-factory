"""Repo inspector (section 8): detect language, test command and setup; smoke-check it.

Detection failure returns ``needs_manual_config`` with reasons. It never guesses silently.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path

from ..config import EnvOverrides
from ..errors import BuildError, FactoryError
from ..models import EnvConfig, InspectResult, RepoSpec
from ..runner.test_runner import NODE_TEST_COMMAND, run_tests
from ..sandbox.base import SandboxFactory

log = logging.getLogger("factory.inspector")

MARKERS = [
    ("python", ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"]),
    ("node", ["package.json"]),
    ("java", ["pom.xml", "build.gradle", "build.gradle.kts"]),
    ("go", ["go.mod"]),
    ("rust", ["Cargo.toml"]),
]

PYTEST_COMMAND = ("python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 "
                  "--junitxml={report} {select}")


def detect_language(path: Path | str) -> str | None:
    path = Path(path)
    for lang, files in MARKERS:
        if any((path / f).exists() for f in files):
            return lang
    return None


# --------------------------------------------------------------------------- per-language rules


def _python(root: Path) -> tuple[EnvConfig | None, list[str]]:
    setup: list[str] = []
    py_version = "3.11"
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            return None, [f"pyproject.toml is not valid TOML: {exc}"]
        project = data.get("project", {})
        m = re.search(r">=\s*3\.(\d+)", project.get("requires-python", ""))
        if m and int(m.group(1)) > 11:
            py_version = f"3.{m.group(1)}"
        extras = [e for e in ("test", "tests", "testing", "dev") if e in project.get("optional-dependencies", {})]
        has_build = "build-system" in data or (root / "setup.py").exists()
        if has_build or project:
            setup.append(f"pip install -q -e '.[{extras[0]}]'" if extras else "pip install -q -e .")
    elif (root / "setup.py").exists():
        setup.append("pip install -q -e .")
    for req in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "dev-requirements.txt",
                "test-requirements.txt"):
        if (root / req).exists():
            setup.append(f"pip install -q -r {req}")
    setup.append("pip install -q pytest")
    env = EnvConfig(base_image=f"python:{py_version}-slim", setup_commands=setup,
                    test_command=PYTEST_COMMAND, framework="pytest", report_format="junit")
    return env, []


def _node(root: Path) -> tuple[EnvConfig | None, list[str]]:
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"package.json is not valid JSON: {exc}"]
    if (root / "package-lock.json").exists():
        install = "npm ci --no-audit --no-fund"
    elif (root / "yarn.lock").exists():
        install = "corepack enable && yarn install --frozen-lockfile"
    elif (root / "pnpm-lock.yaml").exists():
        install = "corepack enable && pnpm install --frozen-lockfile"
    else:
        install = "npm install --no-audit --no-fund"
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    script = (pkg.get("scripts") or {}).get("test", "")
    base = "node:22-slim"
    if "jest" in deps or script.strip().startswith("jest"):
        return EnvConfig(base_image=base, setup_commands=[install], framework="jest", report_format="jest-json",
                         report_path=".factory/report.json",
                         test_command="npx jest --ci --json --outputFile={report} {select}"), []
    if "vitest" in deps:
        return EnvConfig(base_image=base, setup_commands=[install], framework="node", report_format="junit",
                         test_command="npx vitest run --reporter=junit --outputFile={report} {select}"), []
    if "node --test" in script:
        return EnvConfig(base_image=base, setup_commands=[install], framework="node", report_format="junit",
                         test_command=NODE_TEST_COMMAND), []
    return None, [f"unknown Node test runner (scripts.test = {script!r}); set env.test_command, "
                  "env.framework and env.report_format in factory.toml"]


def _java(root: Path) -> tuple[EnvConfig | None, list[str]]:
    if (root / "pom.xml").exists():
        return EnvConfig(base_image="maven:3.9-eclipse-temurin-17",
                         setup_commands=["mvn -q -B -DskipTests dependency:go-offline test-compile"],
                         test_command="mvn -q -B -o test", framework="maven", report_format="junit",
                         report_path="target/surefire-reports/"), []
    return EnvConfig(base_image="gradle:8-jdk17", setup_commands=["gradle --no-daemon -q testClasses"],
                     test_command="gradle --no-daemon --offline -q test", framework="gradle",
                     report_format="junit", report_path="build/test-results/test/"), []


def _go(root: Path) -> tuple[EnvConfig | None, list[str]]:
    return EnvConfig(base_image="golang:1.22", setup_commands=["go mod download", "go test -count=1 -run XXX ./..."],
                     test_command="go test -count=1 -json {select}", framework="go", report_format="go-json"), []


def _rust(root: Path) -> tuple[EnvConfig | None, list[str]]:
    return EnvConfig(base_image="rust:1-slim", setup_commands=["cargo fetch", "cargo test --no-run"],
                     test_command="cargo test --offline", framework="cargo", report_format="cargo-text"), []


RULES = {"python": _python, "node": _node, "java": _java, "go": _go, "rust": _rust}


def _apply_overrides(env: EnvConfig | None, ov: EnvOverrides | None) -> tuple[EnvConfig | None, list[str]]:
    if ov is None:
        return env, []
    fields = {k: v for k, v in ov.model_dump().items() if v is not None}
    if not fields:
        return env, []
    if env is None:
        try:
            return EnvConfig(**fields), []
        except ValueError as exc:
            return None, [f"overrides are not a complete EnvConfig: {exc}"]
    return env.model_validate({**env.model_dump(), **fields}), []


def inspect_repo(spec: RepoSpec, overrides: EnvOverrides | None = None) -> InspectResult:
    """Detect the EnvConfig from marker files, then apply overrides (no build)."""
    root = Path(spec.local_path)
    lang = spec.language or detect_language(root)
    reasons: list[str] = []
    env = None
    if lang is None:
        reasons.append("no known marker file (pyproject.toml, setup.py, requirements.txt, package.json, "
                       "pom.xml, build.gradle, go.mod, Cargo.toml)")
    else:
        env, reasons = RULES[lang](root)
    env, more = _apply_overrides(env, overrides)
    reasons += more
    if env is None:
        return InspectResult(status="needs_manual_config", reasons=reasons)
    return InspectResult(status="ok", env=env)


def smoke_check(spec: RepoSpec, result: InspectResult, factory: SandboxFactory) -> InspectResult:
    """Build the image and run the full test suite once. Records the baseline."""
    if result.env is None:
        return result
    try:
        image = factory.build_image(spec, result.env)
    except BuildError as exc:
        return InspectResult(status="needs_manual_config", env=result.env,
                             reasons=[f"image build failed: {exc}", exc.log[-3000:]])
    try:
        with factory.new_sandbox(image) as sb:
            summary = run_tests(sb, result.env)
    except FactoryError as exc:
        return InspectResult(status="needs_manual_config", env=result.env, reasons=[f"sandbox failed: {exc}"])
    if summary.crashed:
        return InspectResult(status="needs_manual_config", env=result.env, baseline=summary,
                             reasons=[f"baseline test run crashed: {summary.error}"])
    log.info("baseline: %s", summary.counts)
    return InspectResult(status="ok", env=result.env, baseline=summary)
