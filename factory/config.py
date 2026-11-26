"""Factory configuration: ``factory.toml`` plus CLI flags (flags win)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Model provider APIs an agent may reach from its sandbox (harness.egress = "allowlist").
# Zen: opencode.ai + models.opencode.ai (checked through the proxy); the others from the engine's
# provider list. Add your own in factory.toml: [harness] allowed_hosts = [...].
MODEL_API_HOSTS = (
    "opencode.ai", "*.opencode.ai", "integrate.api.nvidia.com", "api.tokenrouter.com", "openrouter.ai",
    "api.anthropic.com", "api.openai.com", "api.groq.com", "bedrock-runtime.*.amazonaws.com",
)

ALL_MUTATIONS = [
    "empty",
    "revert_half",
    "flip_comparison",
    "swap_arguments",
    "drop_error_branch",
    "hardcode_expected",
    "off_by_one",
    "swap_arithmetic",
    "negate_condition",
    "swap_boolean",
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
    # G6 passes when at least this share of mutants (the empty patch included) is caught. 1.0 = every
    # mutant; lower it only with a large operator set, where an equivalent mutant may be unavoidable.
    min_mutation_score: float = 1.0
    # G7: every name the hidden tests import from the repo must exist before the change or be named in
    # the requirements, so a solver can know the API the tests call (anti-ambiguity).
    spec_check: bool = True

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
    workers: int = 1  # attempts at the same time; >1 is opt-in (more memory, more quota errors on free models)
    # The agent's sandbox reaches the internet only through an HTTPS proxy that allows these hosts
    # (fnmatch patterns): the model APIs. "open" gives it the whole internet (it can then look up a
    # public fix). Grading and validation sandboxes never have a network.
    egress: Literal["allowlist", "open"] = "allowlist"
    allowed_hosts: list[str] = Field(default_factory=lambda: list(MODEL_API_HOSTS))
    egress_image: str = "python:3.13-slim"  # runs the proxy (standard library only)


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




PERMISSIVE_LICENCES = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "0BSD", "Unlicense", "Zlib",
                       "BSL-1.0", "PSF-2.0", "MIT-0", "BlueOak-1.0.0"]


class ReferenceModels(_Section):
    """Models per sub-agent role (lists: several drafters/critics). Empty roles use the first model given."""

    profiler: list[str] = Field(default_factory=list)
    researcher: list[str] = Field(default_factory=list)  # the research agent (ideas, with tools)
    scout: list[str] = Field(default_factory=list)
    miner: list[str] = Field(default_factory=list)
    drafter: list[str] = Field(default_factory=list)
    critic: list[str] = Field(default_factory=list)  # put another model family here when you have one
    author: list[str] = Field(default_factory=list)
    test_writer: list[str] = Field(default_factory=list)


class BenchmarkConfig(_Section):
    agents: list[str] = Field(default_factory=list)  # AI under test, "agent" or "agent:model"; empty = no benchmark
    k: int = 1
    workers: int = 1
    max_retries: int = 3
    timeout_s: int = 3600
    cost_cap_usd: float | None = None
    background: bool = True  # run the worker as a detached process after validation


class ReferenceConfig(_Section):
    """The reference-guided pipeline (``factory run``), v2.2."""

    target: int = 5  # finite-run target; continuous mode uses it to size each research batch
    continuous: bool = True  # keep researching and producing packages until the operator stops the run
    research_pause_s: float = 30.0  # pause between continuous rounds to avoid rapid repeated API calls
    workers: int = 4  # sub-agent calls at the same time (profiles, miner, novelty, drafts, critics); 1 = serial
    overgenerate: float = 2.0  # ideas per target task
    research_rounds: int = 3  # research rounds at most, while too few ideas survive the critic
    research_lanes: int = 3  # research agents at once (peers, starter+upstream, web); 1 = one agent, every source
    peers_k: int = 3
    recall: int = 25
    shortlist: int = 8
    prs_per_peer: int = 6
    peers: list[str] = Field(default_factory=list)  # manual peer override, owner/name
    licences: list[str] = Field(default_factory=lambda: list(PERMISSIVE_LICENCES))
    min_stars: int = 50
    max_age_days: int = 730
    repairs: int = 3
    auto_approve: bool = True  # False = optional review step (P7) before building
    harbor: Literal["required", "if_available", "off"] = "required"
    harbor_repeats: int = 2  # oracle runs (stability)
    similarity_flag: float = 0.5
    similarity_max: float = 0.8
    snapshots: str | None = None  # default <workdir>/reference/snapshots
    out: str | None = None  # Harbor tasks + provenance/ reviews/ benchmark/; default <workdir>/reference/harbor
    mongo_uri: str | None = None  # default $TASK_FACTORY_MONGO_URI
    models: ReferenceModels = Field(default_factory=ReferenceModels)
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)


class FactoryConfig(_Section):
    seed: int = 0
    workdir: str = "workdir"
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    env: EnvOverrides = Field(default_factory=EnvOverrides)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    reference: ReferenceConfig = Field(default_factory=ReferenceConfig)
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
