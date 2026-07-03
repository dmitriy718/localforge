from __future__ import annotations

import shlex
import subprocess

from localforge.backends.base import ModelBackend
from localforge.config import BackendConfig
from localforge.models import Message, Role


class LlamaCppBackend(ModelBackend):
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        if not self.config.llama_cpp_model_path:
            raise ValueError("llama_cpp_model_path is required when backend.provider is llama.cpp")

    def generate(self, messages: list[Message]) -> str:
        prompt = self._render_prompt(messages)
        command = [
            self.config.llama_cpp_binary,
            "-m",
            self.config.llama_cpp_model_path or "",
            "-p",
            prompt,
            "-n",
            str(self.config.max_tokens),
            "--ctx-size",
            str(self.config.context_window_tokens),
            "--temp",
            str(self.config.temperature),
        ]
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.config.request_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            rendered = " ".join(shlex.quote(part) for part in command[:4]) + " ..."
            raise RuntimeError(
                f"llama.cpp command failed ({completed.returncode}) for {rendered}\n{completed.stderr}"
            )
        return completed.stdout.strip()

    @staticmethod
    def _render_prompt(messages: list[Message]) -> str:
        rendered: list[str] = []
        for message in messages:
            label = {
                Role.SYSTEM: "System",
                Role.USER: "User",
                Role.ASSISTANT: "Assistant",
                Role.TOOL: "Tool",
            }[message.role]
            rendered.append(f"{label}:\n{message.content}")
        rendered.append("Assistant:")
        return "\n\n".join(rendered)
