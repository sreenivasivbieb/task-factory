"""Repo inspector (section 8): detect language, test command and setup; smoke-check it.

Detection failure returns ``needs_manual_config`` with reasons. It never guesses silently.
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path

from ..config import EnvOverrides
from ..ecosystems import Ecosystem, EnvKeys, helper_text, registry
from ..errors import BuildError, FactoryError
from ..models import EnvConfig, InspectResult, RepoSpec
from ..runner.test_runner import run_tests
from ..sandbox.base import SandboxFactory

log = logging.getLogger("factory.inspector")

_ENV_KEYS = [k for k in EnvKeys.model_fields if k != "helpers"]


def detect_language(path: Path | str) -> str | None:
    """The ecosystem whose marker files the repo has (see ecosystems.toml)."""
    return registry().detect(path)


# --------------------------------------------------------------------------- adapters
# An adapter reads the repo's own build files where plain data is not enough.


def _python_adapter(root: Path, keys: dict) -> list[str]:
    """Python version from requires-python; install the project (with its test extra) and requirements."""
    version, setup = "3.11", []
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            return [f"pyproject.toml is not valid TOML: {exc}"]
        project = data.get("project", {})
        m = re.search(r">=\s*3\.(\d+)", project.get("requires-python", ""))
        if m and int(m.group(1)) > 11:
            version = f"3.{m.group(1)}"
        extras = [e for e in ("test", "tests", "testing", "dev") if e in project.get("optional-dependencies", {})]
        if "build-system" in data or (root / "setup.py").exists() or project:
            setup.append(f"pip install -q -e '.[{extras[0]}]'" if extras else "pip install -q -e .")
    elif (root / "setup.py").exists():
        setup.append("pip install -q -e .")
    for req in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "dev-requirements.txt",
                "test-requirements.txt"):
        if (root / req).exists():
            setup.append(f"pip install -q -r {req}")
    keys["setup_commands"] = setup + list(keys.get("setup_commands") or [])
    keys["base_image"] = str(keys.get("base_image", "")).replace("{python}", version)
    return []


ADAPTERS = {"python": _python_adapter}


def env_for(eco: Ecosystem, root: Path) -> tuple[EnvConfig | None, list[str]]:
    """Build the EnvConfig an ecosystem describes for this repo: variant, install step, adapter."""
    keys = {k: getattr(eco, k) for k in _ENV_KEYS if getattr(eco, k) is not None}
    helpers = dict(eco.helpers or {})
    if eco.variants:
        variant = next((v for v in eco.variants if v.matches(root)), None)
        if variant is None:
            names = ", ".join(v.name for v in eco.variants)
            checked = sorted({f for v in eco.variants for f in (v.if_file, v.if_contains and v.if_contains.file) if f})
            return None, [f"{eco.name}: none of the known test setups ({names}) matches this repo "
                          f"(checked {', '.join(checked) or 'nothing'}); set "
                          "env.test_command, env.framework and env.report_format in factory.toml, or add a "
                          f"variant under [ecosystems.{eco.name}]"]
        keys.update({k: getattr(variant, k) for k in _ENV_KEYS if getattr(variant, k) is not None})
        helpers.update(variant.helpers or {})
    install = next((i.command for i in eco.install if i.if_file is None or (root / i.if_file).exists()), None)
    if install:
        keys["setup_commands"] = [install, *(keys.get("setup_commands") or [])]
    if eco.adapter:
        reasons = ADAPTERS[eco.adapter](root, keys)
        if reasons:
            return None, reasons
    missing = [k for k in ("base_image", "test_command", "framework", "report_format") if not keys.get(k)]
    if missing:
        return None, [f"{eco.name}: the ecosystem does not set {', '.join(missing)}"]
    keys["artifacts"] = list(dict.fromkeys([*registry().common.artifacts, *eco.artifacts]))
    keys["protected_paths"] = registry().protected_re()
    try:
        keys["helper_files"] = {path: helper_text(name) for path, name in helpers.items()}
        return EnvConfig(**keys), []
    except (ValueError, FactoryError) as exc:
        return None, [f"{eco.name}: {exc}"]


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
    reg = registry()
    lang = spec.language if spec.language in reg.ecosystems else detect_language(root)
    reasons: list[str] = []
    env = None
    if lang is None:
        reasons.append(f"no known marker file ({', '.join(reg.all_markers())}); describe the language under "
                       "[ecosystems.<name>] in factory.toml, or set the [env] keys there")
    else:
        env, reasons = env_for(reg.ecosystems[lang], root)
    env, more = _apply_overrides(env, overrides)
    reasons += more
    if env is None:
        return InspectResult(status="needs_manual_config", reasons=reasons)
    return InspectResult(status="ok", env=env)


def smoke_check(spec: RepoSpec, result: InspectResult, factory: SandboxFactory) -> InspectResult:
    """Build the image and run the full test suite once. Records the baseline."""
    if result.env is None:
        return result
    pinned = factory.pin_image(result.env.base_image)
    if pinned != result.env.base_image:
        result = result.model_copy(update={"env": result.env.model_copy(update={"base_image": pinned})})
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
