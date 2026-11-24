"""Task Factory's direct connection to a compatible model provider."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import ConfigError, ModelProviderError, PackageError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def uses_opencode_cli(model: str | None) -> bool:
    """``opencode/<model>`` runs through the local ``opencode`` CLI unless a provider URL is set.

    OpenCode's free models only answer requests made by the OpenCode client, so the factory
    asks the installed CLI (``opencode run``) instead of calling the HTTP API directly.
    ``FACTORY_MODEL_BACKEND=opencode`` forces the CLI for any model ID it knows.
    """
    backend = os.environ.get("FACTORY_MODEL_BACKEND", "").lower()
    if backend in {"http", "api"}:
        return False
    if backend == "opencode":
        return True
    return bool(model and model.startswith("opencode/") and not os.environ.get("FACTORY_MODEL_URL"))


def opencode_text(stdout: str) -> str:
    """The assistant's text from ``opencode run --format json`` events (one JSON object per line).

    Raises PackageError with the provider's message when the run reports an error
    (for example the free usage limit), so a quota problem is not mistaken for a bad draft.
    """
    texts: dict[str, str] = {}
    errors = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            err = event.get("error") or {}
            errors.append(str(err.get("message") or err.get("type") or "unknown error")
                          if isinstance(err, dict) else str(err))
            continue
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            texts[str(part.get("id") or len(texts))] = part["text"]  # later events update the same part
    text = "\n".join(t for t in texts.values() if t.strip())
    if not text.strip() and errors:
        raise ModelProviderError(f"opencode: {errors[-1]}")
    return text


class OpencodeCLI:
    """A model reached through ``opencode run``: one prompt in, the reply text out."""

    def __init__(self, model: str, binary: str | None = None, timeout_s: int | None = None) -> None:
        self.model = model
        self.binary = binary or os.environ.get("FACTORY_OPENCODE_BIN") or shutil.which("opencode") or "opencode"
        self.timeout_s = timeout_s or int(os.environ.get("FACTORY_MODEL_TIMEOUT", "900"))

    def reply(self, messages: list[dict]) -> str:
        prompt = "\n\n".join(str(m.get("content", "")) for m in messages)
        cmd = [self.binary, "run", "-m", self.model, "--format", "json", "--archived"]
        # An empty folder: the CLI is an agent, and it must not see or touch any repository.
        with tempfile.TemporaryDirectory(prefix="factory-opencode-") as cwd:
            try:
                r = subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True,
                                   timeout=self.timeout_s)
            except FileNotFoundError as exc:
                raise ConfigError(f"opencode CLI not found ({self.binary}); install it or set "
                                  "FACTORY_OPENCODE_BIN") from exc
            except subprocess.TimeoutExpired as exc:
                raise ModelProviderError(f"opencode did not answer within {self.timeout_s}s") from exc
        text = opencode_text(r.stdout)
        if not text.strip():
            tail = " ".join((r.stderr or r.stdout or "").split())[-300:]
            raise ModelProviderError(f"opencode returned no text (exit {r.returncode}): {tail}")
        return text


class ModelClient:
    def __init__(self, model: str | None):
        self.model = model or os.environ.get("FACTORY_MODEL")
        self._cli = OpencodeCLI(self.model) if uses_opencode_cli(self.model) else None
        base = os.environ.get("FACTORY_MODEL_URL", "https://api.openai.com/v1")
        url = urlsplit(base)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or (url.scheme != "https" and not (
                    url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
                ))):
            raise ConfigError("FACTORY_MODEL_URL must use HTTPS or loopback HTTP")
        self.base_url = base.rstrip("/")
        self.key = os.environ.get("FACTORY_MODEL_KEY")
        if self.key is None and url.hostname == "api.openai.com":
            self.key = os.environ.get("OPENAI_API_KEY")

    def _request(self, route: str, data: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        request = Request(self.base_url + route, headers=headers,
                          data=None if data is None else json.dumps(data).encode("utf-8"))
        try:
            with build_opener(_NoRedirect()).open(request, timeout=180) as response:
                return json.load(response)
        except HTTPError as exc:
            raise ModelProviderError(f"Model provider returned HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise ModelProviderError("Could not read a response from the model provider") from exc

    def models(self) -> list[str]:
        try:
            return sorted({entry["id"] for entry in self._request("/models")["data"]
                           if isinstance(entry.get("id"), str)})
        except (KeyError, TypeError, AttributeError) as exc:
            raise PackageError("The provider returned an invalid model list") from exc

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        if not self.model:
            raise ConfigError("Select a model with /models, --model, or FACTORY_MODEL")
        data = {"model": self.model, "messages": messages}
        if tools:
            data["tools"] = tools
        try:
            message = self._request("/chat/completions", data)["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PackageError("The provider returned an invalid chat response") from exc
        if not isinstance(message, dict):
            raise PackageError("The provider returned an invalid chat message")
        return message

    def reply(self, messages: list[dict]) -> str:
        if self._cli is not None:
            return self._cli.reply(messages)
        content = self.complete(messages).get("content")
        if not isinstance(content, str) or not content.strip():
            raise PackageError("The model returned no text")
        return content
