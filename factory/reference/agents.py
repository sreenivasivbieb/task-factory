"""Zone A sub-agents: one role, one model, a fresh context per call, replies cached by prompt hash.

The controller decides what each role sees (the "must not see" column of v2.2 section 10) by what it
passes in; a sub-agent never keeps a conversation, so a critic cannot see a drafter's reasoning and
the test writer cannot see the patch. Every model used is recorded for the run's declarations.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..errors import ConfigError, ModelProviderError, PackageError
from ..generation.author import _json_object
from .snapshot import ReplayMiss, Snapshot

log = logging.getLogger("factory.reference.agents")

ROLES = ("profiler", "scout", "researcher", "miner", "drafter", "critic", "author", "test_writer")

_FAMILIES = [
    ("anthropic", r"claude|anthropic|opus|sonnet|haiku|fable"),
    ("openai", r"\bgpt|openai|\bo[1-9]\b|codex"),
    ("google", r"gemini|gemma|google"),
    ("deepseek", r"deepseek"),
    ("qwen", r"qwen|qwq"),
    ("meta", r"llama|meta-"),
    ("zhipu", r"glm|zhipu|z-ai"),
    ("moonshot", r"kimi|moonshot"),
    ("mistral", r"mistral|codestral|devstral|magistral"),
    ("xai", r"grok|x-ai"),
    ("minimax", r"minimax"),
    ("nvidia", r"nemotron|nvidia"),
    ("xiaomi", r"\bmimo"),
    ("tencent", r"hunyuan|\bhy\d"),
]


def family(model: str | None) -> str:
    """The model family, for "at least one critic from another family" (unknown: the model id itself)."""
    m = (model or "").lower()
    for name, pattern in _FAMILIES:
        if re.search(pattern, m):
            return name
    return m.rsplit("/", 1)[-1] or "unknown"


def client_reply(model: str) -> Callable[[str], str]:
    """A reply function for ``model`` through the factory's model client (opencode CLI or HTTP API)."""
    from ..model_client import ModelClient

    client = ModelClient(model)

    def reply(prompt: str) -> str:
        return client.reply([{"role": "user", "content": prompt}])

    return reply


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    failures: int = 0
    chars_in: int = 0
    chars_out: int = 0
    seconds: float = 0.0  # time spent waiting for live replies
    max_s: float = 0.0


@dataclass
class SubAgent:
    role: str
    model: str
    reply: Callable[[str], str]
    snapshot: Snapshot | None = None
    usage: Usage = field(default_factory=Usage)
    fallbacks: list = field(default_factory=list)  # [(model, reply)] tried in order when a model fails (quota)
    answered_by: str | None = None  # the model that answers now (after a fallback)
    call_log: list | None = None  # shared with the pool: one entry per call, for the latency report
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def ask(self, prompt: str) -> str:
        key = {"role": self.role, "model": self.model,
               "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
        with self._lock:
            self.usage.chars_in += len(prompt)
        if self.snapshot is not None:
            try:
                cached = self.snapshot.get("llm", key) if self.snapshot.mode != "refresh" else None
            except (OSError, ValueError):
                cached = None
            if cached is not None:
                with self._lock:
                    self.usage.cached += 1
                self._log(0.0, len(prompt), len(cached["reply"] or ""), cached=True)
                return cached["reply"]
            if self.snapshot.mode == "replay":
                raise ReplayMiss(f"replay: no cached {self.role} reply from {self.model} for this prompt")
        t0 = time.monotonic()
        text = self._call(prompt)
        took = time.monotonic() - t0
        with self._lock:
            self.usage.calls += 1
            self.usage.chars_out += len(text or "")
            self.usage.seconds += took
            self.usage.max_s = max(self.usage.max_s, took)
        self._log(took, len(prompt), len(text or ""), cached=False)
        if self.snapshot is not None:  # saved under this agent's key, so replay finds it whoever answered
            self.snapshot.put("llm", key, {"role": self.role, "model": self.model,
                                           "answered_by": self.answered_by or self.model, "reply": text})
        return text

    def _log(self, seconds: float, chars_in: int, chars_out: int, cached: bool) -> None:
        if self.call_log is not None:
            self.call_log.append({"role": self.role, "model": self.answered_by or self.model,
                                  "seconds": round(seconds, 2), "chars_in": chars_in, "chars_out": chars_out,
                                  "cached": cached, "at": round(time.time(), 2)})

    def _call(self, prompt: str) -> str:
        """The reply from the current model; on a provider failure (quota, HTTP) the next fallback takes over
        for good, like the v1 model chain."""
        while True:
            current = self.reply
            try:
                return current(prompt)
            except ModelProviderError as exc:
                with self._lock:
                    self.usage.failures += 1
                    if self.reply is not current:  # another thread already switched models: use the new one
                        continue
                    if not self.fallbacks:
                        raise
                    model, reply = self.fallbacks.pop(0)
                    log.warning("%s: %s failed (%s); switching to %s", self.role, self.answered_by or self.model,
                                str(exc)[:160], model)
                    self.reply, self.answered_by = reply, model

    def ask_json(self, prompt: str, retries: int = 1) -> dict:
        last = ""
        for _ in range(retries + 1):
            text = self.ask(prompt if not last else prompt + "\n\nYour previous reply was not one JSON object. "
                            "Reply with only the JSON object.")
            data = _json_object(text or "")
            if data is not None:
                return data
            last = " ".join((text or "").split())[:160]
        raise PackageError(f"{self.role} ({self.model}) did not return a JSON object (it began: {last!r})")


class AgentPool:
    """Sub-agents by role. Several drafters and critics; critics prefer another model family."""

    def __init__(self, models: dict[str, list[str]], snapshot: Snapshot | None = None,
                 make_reply: Callable[[str], Callable[[str], str]] = client_reply,
                 fallback: list[str] | None = None) -> None:
        self.snapshot = snapshot
        self._make = make_reply
        self._replies: dict[str, Callable[[str], str]] = {}
        self.models = {r: [m for m in models.get(r) or [] if m] for r in ROLES}
        default = next((ms[0] for ms in self.models.values() if ms), None)
        if default is None:
            raise ConfigError("no model for the reference pipeline: pass --model or set [reference.models] "
                              "in factory.toml (or FACTORY_MODEL)")
        for r in ROLES:
            if not self.models[r]:
                self.models[r] = [default]
        self.fallback_models = list(dict.fromkeys(fallback or []))
        self.call_log: list[dict] = []  # every call: role, model, seconds, sizes, cached
        self.agents: dict[str, list[SubAgent]] = {}
        for role in ROLES:
            self.agents[role] = [SubAgent(role, m, self._reply(m), snapshot, fallbacks=self._fallbacks(m),
                                          call_log=self.call_log) for m in self.models[role]]
        self._order_critics()

    def _fallbacks(self, model: str) -> list:
        return [(m, self._reply(m)) for m in self.fallback_models if m != model]

    def _reply(self, model: str):
        if model not in self._replies:
            if self.snapshot is not None and self.snapshot.mode == "replay":
                def offline(prompt: str, _m=model) -> str:
                    raise ReplayMiss(f"replay: no cached reply from {_m}")
                self._replies[model] = offline
            else:
                self._replies[model] = self._make(model)
        return self._replies[model]

    def _order_critics(self) -> None:
        drafter_families = {family(a.model) for a in self.agents["drafter"]}
        crit = self.agents["critic"]
        crit.sort(key=lambda a: family(a.model) in drafter_families)  # another family first
        while len(crit) < 2:  # two critics, each with a fresh context; same model if only one is given
            crit.append(SubAgent("critic", crit[0].model, crit[0].reply, self.snapshot,
                                 fallbacks=self._fallbacks(crit[0].model), call_log=self.call_log))

    def one(self, role: str, i: int = 0) -> SubAgent:
        agents = self.agents[role]
        return agents[i % len(agents)]

    def all(self, role: str) -> list[SubAgent]:
        return list(self.agents[role])

    def cross_family_critic(self) -> bool:
        drafter_families = {family(a.model) for a in self.agents["drafter"]}
        return any(family(a.model) not in drafter_families for a in self.agents["critic"])

    def declarations(self) -> list[dict]:
        """Every model and role used, with call counts (the guide: all models must be declared)."""
        out = []
        for role, agents in self.agents.items():
            for a in agents:
                if a.usage.calls or a.usage.cached or a.usage.failures:
                    out.append({"role": role, "model": a.model, "family": family(a.model),
                                "answered_by": a.answered_by or a.model, "calls": a.usage.calls,
                                "cached": a.usage.cached, "failures": a.usage.failures,
                                "chars_in": a.usage.chars_in, "chars_out": a.usage.chars_out,
                                "seconds": round(a.usage.seconds, 1), "max_s": round(a.usage.max_s, 1)})
        return out
