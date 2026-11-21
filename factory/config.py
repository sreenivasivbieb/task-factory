"""Factory configuration: ``factory.toml`` plus CLI flags (flags win)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import sha256_text


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


class GatesConfig(_Section):
    stability_repeats: int = 1  # dev default; use 3 for final runs
    mutations: list[str] = Field(default_factory=lambda: ["empty", "flip_comparison", "drop_error_branch"])
    max_known_failure_ratio: float = 0.2
    task_timeout_s: int = 1800


class GenerationConfig(_Section):
    max_repairs: int = 3
    max_touched_files: int = 6
    overgeneration_factor: int = 3


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


ALL_MUTATIONS = [
    "empty",
    "revert_half",
    "flip_comparison",
    "swap_arguments",
    "drop_error_branch",
    "hardcode_expected",
]


class FactoryConfig(_Section):
    seed: int = 0
    workdir: str = "workdir"
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    env: EnvOverrides = Field(default_factory=EnvOverrides)

    @classmethod
    def load(cls, path: Path | None) -> "FactoryConfig":
        """Read ``factory.toml``; a missing default file gives the defaults."""
        if path is None:
            path = Path("factory.toml")
            if not path.exists():
                return cls()
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def config_hash(self) -> str:
        return sha256_text(self.model_dump_json())[:12]
