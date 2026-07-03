from __future__ import annotations

from localforge.backends.base import ModelBackend
from localforge.backends.llamacpp import LlamaCppBackend
from localforge.backends.ollama import OllamaBackend
from localforge.config import BackendConfig


def create_backend(config: BackendConfig) -> ModelBackend:
    provider = config.provider.lower()
    if provider == "ollama":
        return OllamaBackend(config)
    if provider in {"llama.cpp", "llamacpp", "llama-cpp"}:
        return LlamaCppBackend(config)
    raise ValueError(f"Unsupported backend provider: {config.provider}")

