from __future__ import annotations

from abc import ABC, abstractmethod

from localforge.models import RunContext, ToolResult, ToolSpec


class Tool(ABC):
    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """Describe the tool to the model."""

    @abstractmethod
    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        """Execute the tool."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"Duplicate tool name: {name}")
        self._tools[name] = tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def run(self, name: str, arguments: dict[str, object], context: RunContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(name=name, ok=False, output=f"Unknown tool: {name}")
        return tool.run(arguments, context)

