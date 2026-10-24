from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from importlib.resources import files
from typing import Any


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    def generate_json(self, prompt_name: str, context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise LLMError("model did not return JSON")
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError("model returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise LLMError("model response must be a JSON object")
    return value


class HTTPJSONProvider(LLMProvider):
    def __init__(self, model: str, timeout: int = 45):
        self.model, self.timeout = model, timeout

    def prompt(self, name: str) -> str:
        return files("spinup").joinpath("prompts", f"{name}.txt").read_text()

    def _request(self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from exc


class OllamaProvider(HTTPJSONProvider):
    def __init__(self, model: str = "qwen2.5-coder:7b", timeout: int = 45,
                 base_url: str = "http://127.0.0.1:11434"):
        super().__init__(model, timeout)
        self.base_url = base_url.rstrip("/")

    def generate_json(self, prompt_name: str, context: dict[str, Any]) -> dict[str, Any]:
        data = self._request(f"{self.base_url}/api/generate", {
            "model": self.model,
            "prompt": self.prompt(prompt_name) + "\n\nCONTEXT:\n" + json.dumps(context),
            "stream": False,
            "format": "json",
        })
        return _extract_json(str(data.get("response", "")))


class AnthropicProvider(HTTPJSONProvider):
    def __init__(self, model: str = "claude-3-5-haiku-latest", timeout: int = 45):
        super().__init__(model, timeout)

    def generate_json(self, prompt_name: str, context: dict[str, Any]) -> dict[str, Any]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set on the host")
        data = self._request("https://api.anthropic.com/v1/messages", {
            "model": self.model,
            "max_tokens": 2000,
            "system": self.prompt(prompt_name),
            "messages": [{"role": "user", "content": json.dumps(context)}],
        }, {"x-api-key": key, "anthropic-version": "2023-06-01"})
        text = "".join(part.get("text", "") for part in data.get("content", []))
        return _extract_json(text)


class UnavailableProvider(LLMProvider):
    def generate_json(self, prompt_name: str, context: dict[str, Any]) -> dict[str, Any]:
        raise LLMError("LLM is disabled")


def provider_from_settings(settings: dict[str, Any]) -> LLMProvider:
    if not settings.get("enabled", True):
        return UnavailableProvider()
    provider = settings.get("provider", "ollama")
    model = settings.get("model", "qwen2.5-coder:7b")
    timeout = int(settings.get("timeout_seconds", 45))
    if provider == "ollama":
        return OllamaProvider(model, timeout, settings.get("base_url", "http://127.0.0.1:11434"))
    if provider == "anthropic":
        return AnthropicProvider(model, timeout)
    raise ValueError("llm.provider must be ollama or anthropic")
