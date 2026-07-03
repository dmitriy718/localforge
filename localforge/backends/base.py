from __future__ import annotations

from abc import ABC, abstractmethod

from localforge.models import Message


class ModelBackend(ABC):
    @abstractmethod
    def generate(self, messages: list[Message]) -> str:
        """Return a model response for the supplied chat messages."""

