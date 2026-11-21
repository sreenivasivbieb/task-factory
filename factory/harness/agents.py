"""Agent adapters for the coding harness.

An agent is described by data, not code: how to install it into the task's
image, and a shell command that runs it in the repo with the instruction.
Placeholders in ``run``:

- ``{instruction_file}``: path of the instruction inside the sandbox
- ``{model_args}``: the agent's model flag filled with ``--model``, or empty

Add your own agent in ``factory.toml``::

    [harness.agents.my-agent]
    install = ["pip install -q my-agent"]
    run = "my-agent --task-file {instruction_file} {model_args}"
    model_flag = "--model {model}"
    secrets = ["MY_API_KEY"]
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ConfigError

_ENSURE_NODE = ("command -v npm >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq "
                "--no-install-recommends nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/*)")
# OpenCode v2 ships as @opencode/cli (the older "opencode-ai" package is v1 and lacks --standalone).
OPENCODE_VERSION = "2.3.2"

_BEDROCK = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_REGION_NAME",
            "AWS_BEARER_TOKEN_BEDROCK"]
_ENSURE_PIP = ("command -v pip >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq "
               "--no-install-recommends python3-pip && rm -rf /var/lib/apt/lists/*)")


def _pip(pkg: str) -> str:
    return f"pip install -q {pkg} || pip install -q --break-system-packages {pkg}"


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    kind: Literal["command", "oracle", "noop"] = "command"
    description: str = ""
    install: list[str] = Field(default_factory=list)  # run as root when building the agent image
    run: str = ""  # shell command, run as the sandbox user in the repo
    model_flag: str = ""  # e.g. "--model {model}"
    default_model: str | None = None
    secrets: list[str] = Field(default_factory=list)  # forwarded from the host when set
    env: dict[str, str] = Field(default_factory=dict)
    network: bool = True
    timeout_s: int = 1800
    homepage: str = ""
    experimental: bool = False
    # Optional: copy the user's own login for this agent into the sandbox (opt-in, --use-host-auth).
    # Only the entry for the model's provider ("nvidia" in "nvidia/...") is copied.
    host_auth_file: str | None = None
    sandbox_auth_file: str | None = None

    def command(self, instruction_file: str, model: str | None) -> str:
        model = model or self.default_model
        model_args = self.model_flag.format(model=model) if (model and self.model_flag) else ""
        return self.run.replace("{instruction_file}", instruction_file).replace("{model_args}", model_args).strip()


BUILTIN: dict[str, AgentSpec] = {
    "oracle": AgentSpec(kind="oracle", network=False,
                        description="Applies the reference patch. Sanity check: should resolve every task."),
    "noop": AgentSpec(kind="noop", network=False,
                      description="Changes nothing. Sanity check: should resolve no task."),
    "claude-code": AgentSpec(
        description="Anthropic's Claude Code CLI in headless mode (claude -p).",
        install=[_ENSURE_NODE, "npm install -g @anthropic-ai/claude-code"],
        run='claude -p "$(cat {instruction_file})" --dangerously-skip-permissions --output-format json {model_args}',
        model_flag="--model {model}", secrets=["ANTHROPIC_API_KEY"], experimental=True,
        homepage="https://github.com/anthropics/claude-code"),
    "codex": AgentSpec(
        description="OpenAI Codex CLI (codex exec).",
        install=[_ENSURE_NODE, "npm install -g @openai/codex"],
        run='codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox {model_args} '
            '"$(cat {instruction_file})"',
        model_flag="-m {model}", secrets=["OPENAI_API_KEY"], experimental=True,
        homepage="https://github.com/openai/codex"),
    "aider": AgentSpec(
        description="Aider, the AI pair programmer, non-interactive with --message-file.",
        install=[_ENSURE_PIP, _pip("aider-chat")],
        run="aider --yes-always --no-auto-commits --no-show-model-warnings --no-check-update "
            "--message-file {instruction_file} {model_args}",
        model_flag="--model {model}", secrets=["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"],
        experimental=True, homepage="https://github.com/Aider-AI/aider"),
    "opencode": AgentSpec(
        description="OpenCode CLI (opencode run --auto). Any OpenCode provider: nvidia/..., tokenrouter/..., "
                    "opencode/... (Zen), amazon-bedrock/qwen... ",
        install=[_ENSURE_NODE, f"npm install -g @opencode/cli@{OPENCODE_VERSION}"],
        run='opencode run --auto --standalone --format json {model_args} "$(cat {instruction_file})"',
        model_flag="-m {model}",
        secrets=["NVIDIA_API_KEY", "TOKENROUTER_API_KEY", "OPENCODE_API_KEY", "OPENROUTER_API_KEY", *_BEDROCK],
        host_auth_file="~/.local/share/opencode/auth.json",
        sandbox_auth_file="/home/factory/.local/share/opencode/auth.json",
        experimental=True, homepage="https://github.com/sst/opencode"),
    "mini-swe-agent": AgentSpec(
        description="mini-swe-agent from the SWE-agent team (mini -y --exit-immediately); LiteLLM models such as "
                    "bedrock/qwen.qwen3-coder-480b-a35b-v1:0 or nvidia_nim/...",
        install=[_ENSURE_PIP, _pip("mini-swe-agent")],
        run='mini -t "$(cat {instruction_file})" -y --exit-immediately -l 0 {model_args}',
        model_flag="-m {model}",
        secrets=["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "NVIDIA_NIM_API_KEY", "OPENROUTER_API_KEY",
                 *_BEDROCK],
        env={"MSWEA_CONFIGURED": "true", "MSWEA_COST_TRACKING": "ignore_errors"}, experimental=True,
        homepage="https://github.com/SWE-agent/mini-swe-agent"),
}


def agent_registry(custom: dict[str, dict] | None = None) -> dict[str, AgentSpec]:
    agents = {name: spec.model_copy(update={"name": name}) for name, spec in BUILTIN.items()}
    for name, data in (custom or {}).items():
        try:
            agents[name] = AgentSpec.model_validate({**data, "name": name})
        except ValueError as exc:
            raise ConfigError(f"bad [harness.agents.{name}] in factory.toml: {exc}") from exc
    return agents


def get_agent(name: str, custom: dict[str, dict] | None = None, cmd: str | None = None) -> AgentSpec:
    """Look up an agent; ``command`` with ``cmd`` runs any shell command as the agent."""
    if name == "command":
        if not cmd:
            raise ConfigError("--agent command needs --cmd 'your command {instruction_file}'")
        return AgentSpec(name="command", description="custom command", run=cmd)
    agents = agent_registry(custom)
    if name not in agents:
        raise ConfigError(f"unknown agent '{name}'; known: {', '.join(sorted(agents))}, command")
    return agents[name]
