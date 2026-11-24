"""Factory configuration: ``factory.toml`` plus CLI flags (flags win)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


ALL_MUTATIONS = [
    "empty",
    "revert_half",
    "flip_comparison",
    "swap_arguments",
    "drop_error_branch",
    "hardcode_expected",
]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SandboxConfig(_Section):
    backend: str = "docker"  # docker | local (local = no isolation, dev only)
    docker_binary: str = "docker"  # set to "podman" to use Podman
    memory: str = "2g"
    cpus: float = 2.0
    pids_limit: int = 512
    build_timeout_s: int = 1800
    output_limit_bytes: int = 1_000_000
    local_run_setup: bool = False  # local backend: run setup commands on the host (off by default)
    pin_base_image: bool = True  # inspect records the base image by digest (name:tag@sha256:...)


class GatesConfig(_Section):
    stability_repeats: int = 1  # dev default; use 3 for final runs
    mutations: list[str] = Field(default_factory=lambda: ["empty", "flip_comparison", "drop_error_branch"])
    max_known_failure_ratio: float = 0.2

    @field_validator("mutations")
    @classmethod
    def _known_mutations(cls, names: list[str]) -> list[str]:
        unknown = sorted(set(names) - set(ALL_MUTATIONS))
        if unknown:  # a typo used to switch a mutant off silently
            raise ValueError(f"unknown mutation(s) {unknown}; known: {ALL_MUTATIONS}")
        return names


class HarnessConfig(_Section):
    agents: dict[str, dict] = Field(default_factory=dict)  # custom agents, see harness/agents.py
    k: int = 1
    default_agent: str = "task-factory"
    default_model: str | None = None  # e.g. "nvidia/qwen/qwen3-coder-480b-a35b-instruct"
    use_host_auth: bool = False  # copy the agent's stored login file into the sandbox
    branches: bool = True  # commit every resolved attempt to a new git branch in workdir/solutions
    stop_after_agent_errors: int = 2  # an exhausted quota fails every task alike: stop the run early


class EnvOverrides(_Section):
    """Optional overrides applied on top of the inspector's EnvConfig."""

    base_image: str | None = None
    setup_commands: list[str] | None = None
    test_command: str | None = None
    test_timeout_s: int | None = None
    env_vars: dict[str, str] | None = None
    framework: str | None = None
    report_format: str | None = None
    report_path: str | None = None




class FactoryConfig(_Section):
    seed: int = 0
    workdir: str = "workdir"
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    env: EnvOverrides = Field(default_factory=EnvOverrides)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    ecosystems: dict[str, dict] = Field(default_factory=dict)  # add/override languages, see ecosystems.toml

    @classmethod
    def load(cls, path: Path | None) -> "FactoryConfig":
        """Read ``factory.toml``; a missing default file gives the defaults."""
        if path is None:
            path = Path("factory.toml")
            if not path.exists():
                return cls()
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)
