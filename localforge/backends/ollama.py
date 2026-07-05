from __future__ import annotations

import httpx

from localforge.backends.base import ModelBackend
from localforge.config import BackendConfig
from localforge.models import Message, Role


class OllamaBackend(ModelBackend):
    def __init__(self, config: BackendConfig) -> None:
        self.config = config

    def generate(self, messages: list[Message]) -> str:
        payload = {
            "model": self.config.model,
            "stream": False,
            "messages": [{"role": message.role.value, "content": message.content} for message in messages],
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
                "num_ctx": self.config.context_window_tokens,
            },
        }
        if self.config.force_json:
            payload["format"] = "json"
        with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
            response = client.post(f"{self.config.ollama_url.rstrip('/')}/api/chat", json=payload)
            if response.status_code == 400:
                return self._generate_fallback(client, messages)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(f"Ollama chat request failed: {response.text}") from exc
            data = response.json()
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise RuntimeError(f"Unexpected Ollama response shape: {data!r}")
        return content

    def _generate_fallback(self, client: httpx.Client, messages: list[Message]) -> str:
        payload = {
            "model": self.config.model,
            "stream": False,
            "prompt": self._render_prompt(messages),
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
                "num_ctx": self.config.context_window_tokens,
            },
        }
        if self.config.force_json:
            payload["format"] = "json"
        response = client.post(f"{self.config.ollama_url.rstrip('/')}/api/generate", json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Ollama generate fallback failed: {response.text}") from exc
        data = response.json()
        content = data.get("response")
        if not isinstance(content, str):
            raise RuntimeError(f"Unexpected Ollama generate response shape: {data!r}")
        return content

    @staticmethod
    def _render_prompt(messages: list[Message]) -> str:
        labels = {
            Role.SYSTEM: "System",
            Role.USER: "User",
            Role.ASSISTANT: "Assistant",
            Role.TOOL: "Tool",
        }
        rendered = [f"{labels[message.role]}:\n{message.content}" for message in messages]
        rendered.append("Assistant:")
        return "\n\n".join(rendered)

    def healthcheck(self) -> str:
        with httpx.Client(timeout=10) as client:
            response = client.get(f"{self.config.ollama_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            data = response.json()
        models = [item.get("name", "") for item in data.get("models", []) if isinstance(item, dict)]
        return "\n".join(models)
