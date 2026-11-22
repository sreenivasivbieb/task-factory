"""Task Factory's direct connection to a compatible model provider."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import ConfigError, PackageError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ModelClient:
    def __init__(self, model: str | None):
        self.model = model or os.environ.get("FACTORY_MODEL")
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
            raise PackageError(f"Model provider returned HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise PackageError("Could not read a response from the model provider") from exc

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
        content = self.complete(messages).get("content")
        if not isinstance(content, str) or not content.strip():
            raise PackageError("The model returned no text")
        return content
