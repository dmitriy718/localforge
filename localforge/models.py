from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentAction:
    thought: str
    tool_calls: tuple[ToolCall, ...]
    final: str | None = None


@dataclass(frozen=True)
class RunContext:
    run_id: str
    workspace: Path
    run_dir: Path
    dry_run: bool


@dataclass(frozen=True)
class AgentEvent:
    type: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
