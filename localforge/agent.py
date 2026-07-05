from __future__ import annotations

import json
import os
import queue
import re
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Callable

from localforge.audit import JsonlAuditLog, new_run_id
from localforge.backends.base import ModelBackend
from localforge.config import HarnessConfig
from localforge.mcp.client import McpStdioClient, McpToolAdapter
from localforge.models import AgentAction, AgentEvent, Message, Role, RunContext, ToolCall, ToolResult
from localforge.tools.base import ToolRegistry
from localforge.tools.builtin import create_builtin_registry


SYSTEM_PROMPT = """You are LocalForge, an autonomous local software builder.

You have real tools. Do not invent command output, file contents, test results, or deployment status.
Inspect before editing. Implement incrementally. Verify meaningful changes. When you are done,
produce a concise final report with completed work, files changed, commands run, verification,
and remaining risks.

You may think freely and explain your direction. When you want LocalForge to execute tools,
include an executable action in one of these forms.

Preferred action object:
{
  "thought": "short reasoning grounded in current observations",
  "tool_calls": [
    {"name": "tool_name", "arguments": {"key": "value"}}
  ],
  "final": null
}

Direct single-tool object:
{
  "name": "tool_name",
  "arguments": {"key": "value"}
}

When finished, you can return:
{
  "thought": "why the work is complete",
  "tool_calls": [],
  "final": "final report"
}

Your raw output will be shown to the operator. Be honest about uncertainty and what you are trying.
If a tool fails, adapt. If you need current file contents, read them. If verification cannot be
performed, say exactly why in final. Do not use placeholders, mock-only behavior, empty functions,
or TODO-driven core behavior.
"""


class AgentRunner:
    def __init__(
        self,
        config: HarnessConfig,
        backend: ModelBackend,
        event_handler: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.mcp_clients: list[McpStdioClient] = []
        self.event_handler = event_handler

    def close(self) -> None:
        for client in self.mcp_clients:
            client.close()

    def run(self, prompt: str, *, dry_run: bool = False) -> str:
        workspace = self.config.workspace.resolve()
        run_id = new_run_id()
        run_dir = (workspace / self.config.runs_dir / run_id).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        audit = JsonlAuditLog(run_dir / "events.jsonl")
        context = RunContext(run_id=run_id, workspace=workspace, run_dir=run_dir, dry_run=dry_run)
        self._emit(
            "run_start",
            f"Run {run_id} started in {workspace}",
            {"run_id": run_id, "workspace": str(workspace), "run_dir": str(run_dir)},
        )
        registry = self._create_registry(audit)

        messages = [
            Message(Role.SYSTEM, SYSTEM_PROMPT + "\n\nAvailable tools:\n" + _render_tool_specs(registry)),
            Message(Role.USER, prompt),
        ]
        audit.record("run_start", {"run_id": run_id, "workspace": workspace, "dry_run": dry_run})

        final_report = ""
        tool_results_seen = 0
        mutation_needs_verification = False
        try:
            for iteration in range(1, self.config.max_iterations + 1):
                audit.record("iteration_start", {"iteration": iteration})
                self._emit(
                    "model_wait",
                    f"Iteration {iteration}: waiting for {self.config.backend.provider}:{self.config.backend.model}",
                    {"iteration": iteration},
                )
                raw_response = self._generate_with_heartbeat(messages, iteration)
                audit.record("model_response", {"iteration": iteration, "response": raw_response})
                self._emit("model_output", raw_response, {"iteration": iteration})
                messages.append(Message(Role.ASSISTANT, raw_response))
                action, interpretation_error = interpret_action(raw_response, registry)
                if action is None:
                    if tool_results_seen > 0 and raw_response.strip():
                        if mutation_needs_verification:
                            result = _verification_required_result()
                            messages.append(Message(Role.TOOL, render_tool_result(result)))
                            audit.record(
                                "verification_required",
                                {"iteration": iteration, "mode": "natural_language_final"},
                            )
                            self._emit(
                                "protocol_error",
                                f"Iteration {iteration}: final report blocked until mutation verification passes",
                                {"iteration": iteration},
                            )
                            continue
                        final_report = raw_response.strip()
                        audit.record(
                            "run_final",
                            {
                                "iteration": iteration,
                                "final": final_report,
                                "mode": "natural_language_after_tool_use",
                            },
                        )
                        self._emit(
                            "final",
                            "Accepted natural-language final report after tool use",
                            {"iteration": iteration},
                        )
                        break
                    result = ToolResult(
                        name="agent_protocol",
                        ok=False,
                        output=(
                            f"{interpretation_error}\n"
                            "No executable action was found. Continue naturally, or include a tool action "
                            "when you want LocalForge to do something."
                        ),
                    )
                    messages.append(Message(Role.TOOL, render_tool_result(result)))
                    audit.record(
                        "interpretation_miss",
                        {"iteration": iteration, "error": interpretation_error},
                    )
                    self._emit(
                        "interpretation_miss",
                        f"Iteration {iteration}: no executable action found yet",
                        {"iteration": iteration, "error": interpretation_error or ""},
                    )
                    continue
                self._emit(
                    "model_thought",
                    action.thought,
                    {"iteration": iteration, "tool_calls": [call.name for call in action.tool_calls]},
                )

                if action.final is not None and not action.tool_calls:
                    if mutation_needs_verification:
                        result = _verification_required_result()
                        messages.append(Message(Role.TOOL, render_tool_result(result)))
                        audit.record(
                            "verification_required",
                            {"iteration": iteration, "mode": "structured_final"},
                        )
                        self._emit(
                            "protocol_error",
                            f"Iteration {iteration}: final report blocked until mutation verification passes",
                            {"iteration": iteration},
                        )
                        continue
                    final_report = action.final
                    audit.record("run_final", {"iteration": iteration, "final": final_report})
                    self._emit("final", "Final report received", {"iteration": iteration})
                    break

                if not action.tool_calls:
                    result = ToolResult(
                        name="agent_protocol",
                        ok=False,
                        output=(
                            "No executable tool action was found. Continue reasoning freely, or include "
                            "a direct tool object when ready."
                        ),
                    )
                    messages.append(Message(Role.TOOL, render_tool_result(result)))
                    audit.record("tool_result", {"iteration": iteration, "result": result})
                    self._emit(
                        "protocol_error",
                        f"Iteration {iteration}: model omitted both tool calls and final report",
                        {"iteration": iteration},
                    )
                    continue

                for call in action.tool_calls:
                    intent = _classify_tool_call(call)
                    audit.record(
                        "tool_call",
                        {
                            "iteration": iteration,
                            "name": call.name,
                            "arguments": call.arguments,
                            "intent": intent,
                        },
                    )
                    self._emit(
                        "tool_start",
                        f"Iteration {iteration}: running {call.name}",
                        {"iteration": iteration, "tool": call.name, "arguments": call.arguments},
                    )
                    result = registry.run(call.name, dict(call.arguments), context)
                    audit.record("tool_result", {"iteration": iteration, "result": result})
                    self._emit(
                        "tool_result",
                        f"{call.name}: {'ok' if result.ok else 'failed'}",
                        {
                            "iteration": iteration,
                            "tool": call.name,
                            "ok": result.ok,
                            "metadata": result.metadata,
                            "output_preview": result.output[:1000],
                        },
                    )
                    tool_results_seen += 1
                    if result.ok:
                        if intent == "mutation":
                            mutation_needs_verification = True
                            audit.record(
                                "verification_pending",
                                {"iteration": iteration, "tool": call.name},
                            )
                        elif intent == "verification" and mutation_needs_verification:
                            mutation_needs_verification = False
                            audit.record(
                                "verification_satisfied",
                                {"iteration": iteration, "tool": call.name},
                            )
                    messages.append(Message(Role.TOOL, render_tool_result(result)))
            else:
                final_report = (
                    f"Run stopped after max_iterations={self.config.max_iterations} without a final report. "
                    f"Audit log: {run_dir / 'events.jsonl'}"
                )
                audit.record("run_incomplete", {"reason": "max_iterations"})
                self._emit("run_incomplete", final_report, {"run_dir": str(run_dir)})
        finally:
            (run_dir / "transcript.json").write_text(
                json.dumps([{"role": m.role.value, "content": m.content} for m in messages], indent=2),
                encoding="utf-8",
            )
            self._emit("run_artifacts", f"Artifacts written to {run_dir}", {"run_dir": str(run_dir)})
        return final_report + f"\n\nRun artifacts: {run_dir}"

    def _create_registry(self, audit: JsonlAuditLog) -> ToolRegistry:
        registry = create_builtin_registry(self.config.tools)
        for server in self.config.mcp_servers:
            if not server.enabled:
                audit.record("mcp_server_skipped", {"server": server.name, "reason": "disabled"})
                self._emit(
                    "mcp_skip",
                    f"MCP {server.name}: disabled",
                    {"server": server.name, "reason": "disabled"},
                )
                continue
            missing_env = [
                name
                for name in server.required_env
                if not server.env.get(name) and not os.environ.get(name)
            ]
            if missing_env:
                audit.record(
                    "mcp_server_skipped",
                    {"server": server.name, "reason": "missing_required_env", "missing": missing_env},
                )
                self._emit(
                    "mcp_skip",
                    f"MCP {server.name}: missing credentials ({', '.join(missing_env)})",
                    {"server": server.name, "reason": "missing_required_env", "missing": missing_env},
                )
                continue
            self._emit("mcp_start", f"Starting MCP {server.name}", {"server": server.name})
            client = McpStdioClient(server)
            self.mcp_clients.append(client)
            tool_count = 0
            for tool in client.list_tools():
                adapter = McpToolAdapter(client, tool)
                registry.register(adapter)
                audit.record("mcp_tool_registered", {"server": server.name, "tool": adapter.spec.name})
                tool_count += 1
            self._emit(
                "mcp_ready",
                f"MCP {server.name}: {tool_count} tools registered",
                {"server": server.name, "tool_count": tool_count},
            )
        return registry

    def _emit(self, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
        if self.event_handler is not None:
            self.event_handler(AgentEvent(event_type, message, data or {}))

    def _generate_with_heartbeat(self, messages: list[Message], iteration: int) -> str:
        heartbeat = max(0.01, self.config.backend.heartbeat_seconds)
        started = time.monotonic()
        result_queue: queue.Queue[tuple[str, str | BaseException]] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put(("ok", self.backend.generate(list(messages))))
            except BaseException as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=worker, name=f"localforge-model-{iteration}", daemon=True)
        thread.start()
        while True:
            try:
                status, payload = result_queue.get(timeout=heartbeat)
            except queue.Empty:
                elapsed = int(time.monotonic() - started)
                self._emit(
                    "model_heartbeat",
                    f"Iteration {iteration}: still waiting on model after {elapsed}s",
                    {"iteration": iteration, "elapsed_seconds": elapsed},
                )
                continue
            if status == "ok":
                if not isinstance(payload, str):
                    raise RuntimeError("Model backend returned non-string response")
                return payload
            if isinstance(payload, BaseException):
                raise payload
            raise RuntimeError(str(payload))


def parse_action(raw_response: str) -> AgentAction:
    action, error = interpret_action(raw_response, None)
    if action is None:
        raise ValueError(error or "Could not parse model action")
    return action


def interpret_action(
    raw_response: str, registry: ToolRegistry | None
) -> tuple[AgentAction | None, str | None]:
    objects = _extract_json_objects(raw_response)
    errors: list[str] = []
    for candidate in objects:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON: {exc}")
            continue
        try:
            return _action_from_data(data, registry), None
        except ValueError as exc:
            errors.append(str(exc))
            continue
    slash_action = _extract_slash_tool_action(raw_response, registry)
    if slash_action is not None:
        return slash_action, None
    bare_tool_action = _extract_bare_tool_action(raw_response, registry)
    if bare_tool_action is not None:
        return bare_tool_action, None
    mentioned_tool_action = _extract_mentioned_tool_action(raw_response, registry)
    if mentioned_tool_action is not None:
        return mentioned_tool_action, None
    if not objects:
        return None, "No JSON action object found in model output"
    return None, "; ".join(errors) if errors else "No executable action found"


def _action_from_data(data: Any, registry: ToolRegistry | None) -> AgentAction:
    if not isinstance(data, dict):
        raise ValueError("Model response JSON must be an object")
    if {"tool", "ok", "output"}.issubset(data.keys()) and _find_nested_action(data) is None:
        raise ValueError("JSON object is an echoed tool observation, not a new action")
    nested_action = _find_nested_action(data)
    if nested_action is not None and nested_action is not data:
        return _action_from_data(nested_action, registry)
    if isinstance(data.get("tool_calls"), list) or "final" in data:
        return _structured_action_from_data(data)

    direct_name = data.get("name") or data.get("command") or data.get("tool")
    if isinstance(direct_name, str) and direct_name:
        resolved_name = _resolve_tool_name(registry, direct_name)
        arguments = data.get("arguments")
        if arguments is None and isinstance(data.get("properties"), dict):
            arguments = data["properties"]
        if arguments is None and isinstance(data.get("params"), dict):
            arguments = data["params"]
        if arguments is None:
            arguments = {
                key: value
                for key, value in data.items()
                if key not in {"name", "required", "description", "schema", "input_schema"}
            }
        if not isinstance(arguments, dict):
            raise ValueError("direct tool call arguments must be an object")
        return AgentAction(
            thought=f"Model requested tool {resolved_name}.",
            tool_calls=(ToolCall(name=resolved_name, arguments=arguments),),
            final=None,
        )

    if {"thought", "thoughtNumber", "totalThoughts"}.issubset(data.keys()):
        tool_name = _find_tool_name(registry, "sequentialthinking")
        if tool_name:
            return AgentAction(
                thought=str(data.get("thought", "Sequential thinking step.")),
                tool_calls=(ToolCall(name=tool_name, arguments=data),),
                final=None,
            )

    return _structured_action_from_data(data)


def _find_nested_action(data: dict[str, Any]) -> dict[str, Any] | None:
    action = data.get("action")
    if isinstance(action, dict):
        return action
    output = data.get("output")
    if isinstance(output, dict):
        output_action = output.get("action")
        if isinstance(output_action, dict):
            return output_action
    return None


def _structured_action_from_data(data: dict[str, Any]) -> AgentAction:
    try:
        thought = data.get("thought")
    except AttributeError as exc:
        raise ValueError("Model response JSON must be an object") from exc
    if not isinstance(thought, str):
        raise ValueError("Model response JSON requires string field: thought")
    final_raw = data.get("final")
    if final_raw is not None and not isinstance(final_raw, str):
        raise ValueError("final must be a string or null")
    calls_raw = data.get("tool_calls", [])
    if not isinstance(calls_raw, list):
        raise ValueError("tool_calls must be a list")
    calls: list[ToolCall] = []
    for index, item in enumerate(calls_raw):
        if not isinstance(item, dict):
            raise ValueError(f"tool_calls[{index}] must be an object")
        name = item.get("name")
        arguments = item.get("arguments", {})
        if not isinstance(name, str) or not name:
            raise ValueError(f"tool_calls[{index}].name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise ValueError(f"tool_calls[{index}].arguments must be an object")
        calls.append(ToolCall(name=name, arguments=arguments))
    return AgentAction(thought=thought, tool_calls=tuple(calls), final=final_raw)


def _find_tool_name(registry: ToolRegistry | None, suffix: str) -> str | None:
    if registry is None:
        return None
    for spec in registry.specs():
        if spec.name == suffix or spec.name.endswith(suffix):
            return spec.name
    return None


def _resolve_tool_name(registry: ToolRegistry | None, requested: str) -> str:
    if registry is None:
        return requested
    names = [spec.name for spec in registry.specs()]
    if requested in names:
        return requested
    aliases = {
        "list_directory": "list_files",
        "list_files": "list_files",
        "read_text_file": "read_file",
        "write_text_file": "write_file",
    }
    alias = aliases.get(requested)
    if alias in names:
        return alias
    for name in names:
        if name.endswith(f"__{requested}") or name.endswith(requested):
            return name
    return requested


def _extract_slash_tool_action(raw_response: str, registry: ToolRegistry | None) -> AgentAction | None:
    for match in re.finditer(r"(?m)^\s*/([A-Za-z0-9_.:-]+)(?:\s+([^\n`]+))?\s*$", raw_response):
        requested = match.group(1)
        resolved = _resolve_tool_name(registry, requested)
        argument_text = (match.group(2) or "").strip()
        arguments: dict[str, Any] = {}
        if argument_text:
            arguments["path"] = argument_text
        elif "list" in requested or "directory" in requested:
            arguments["path"] = "."
        return AgentAction(
            thought=f"Model requested slash command {resolved}.",
            tool_calls=(ToolCall(name=resolved, arguments=arguments),),
            final=None,
        )
    return None


def _extract_bare_tool_action(raw_response: str, registry: ToolRegistry | None) -> AgentAction | None:
    if registry is None:
        return None
    tool_names = {spec.name for spec in registry.specs()}
    candidates: list[str] = []
    if "```" in raw_response:
        blocks = raw_response.split("```")
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if lines and lines[0] in {"bash", "sh", "shell", "text"}:
                lines = lines[1:]
            candidates.extend(lines)
    candidates.extend(line.strip() for line in raw_response.splitlines() if line.strip())

    for candidate in candidates:
        command = candidate.split()[0]
        if command in tool_names:
            arguments = _parse_command_arguments(candidate)
            if "list" in command or "directory" in command:
                arguments.setdefault("path", ".")
            return AgentAction(
                thought=f"Model requested bare tool command {command}.",
                tool_calls=(ToolCall(name=command, arguments=arguments),),
                final=None,
            )
    return None


def _parse_command_arguments(candidate: str) -> dict[str, Any]:
    try:
        parts = shlex.split(candidate)
    except ValueError:
        return {}
    arguments: dict[str, Any] = {}
    index = 1
    while index < len(parts):
        part = parts[index]
        if part.startswith("--"):
            key = part[2:].replace("-", "_")
            value = "true"
            if index + 1 < len(parts) and not parts[index + 1].startswith("--"):
                value = parts[index + 1]
                index += 1
            arguments[key] = value
        index += 1
    return arguments


def _extract_mentioned_tool_action(raw_response: str, registry: ToolRegistry | None) -> AgentAction | None:
    if registry is None:
        return None
    text = raw_response.lower()
    if not any(phrase in text for phrase in ["i will use", "i'll use", "let's", "use the"]):
        return None
    safe_verbs = ("list", "read", "search", "directory", "tree", "info", "allowed")
    for spec in registry.specs():
        if spec.name not in raw_response:
            continue
        if not any(verb in spec.name for verb in safe_verbs):
            continue
        arguments = _default_arguments_for_tool(spec.name, spec.input_schema)
        return AgentAction(
            thought=f"Model mentioned intent to use {spec.name}.",
            tool_calls=(ToolCall(name=spec.name, arguments=arguments),),
            final=None,
        )
    return None


def _default_arguments_for_tool(tool_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties")
    required = schema.get("required")
    arguments: dict[str, Any] = {}
    if isinstance(properties, dict):
        if "path" in properties:
            arguments["path"] = "."
        if "pattern" in properties and isinstance(required, list) and "pattern" in required:
            arguments["pattern"] = "*"
        if "max_files" in properties:
            arguments["max_files"] = 300
    if not arguments and ("list" in tool_name or "directory" in tool_name):
        arguments["path"] = "."
    return arguments


def render_tool_result(result: ToolResult) -> str:
    return json.dumps(
        {
            "tool": result.name,
            "ok": result.ok,
            "output": result.output,
            "metadata": result.metadata,
        },
        indent=2,
        sort_keys=True,
    )


def _verification_required_result() -> ToolResult:
    return ToolResult(
        name="agent_protocol",
        ok=False,
        output=(
            "A file, directory, patch, shell, or remote MCP mutation succeeded after the last "
            "verification step. Before giving a final success report, run a concrete verification "
            "tool call that proves the requested postcondition, such as read_file, list_files, "
            "path_info, or a shell check like `test -d <path> && ls -ld <path>`."
        ),
        metadata={"verification_required": True},
    )


def _classify_tool_call(call: ToolCall) -> str:
    name = call.name.lower()
    if name == "shell":
        command = str(call.arguments.get("cmd", ""))
        return "verification" if _is_verification_shell_command(command) else "mutation"
    if name in {"read_file", "list_files", "search", "fetch_url"}:
        return "verification"
    if name in {"write_file", "write_json", "apply_patch", "create_directory", "path_info"}:
        return "mutation" if name != "path_info" else "verification"
    if name.startswith("mcp__"):
        if any(token in name for token in _MCP_MUTATION_TOKENS):
            return "mutation"
        if any(token in name for token in _MCP_VERIFICATION_TOKENS):
            return "verification"
    return "other"


_MCP_MUTATION_TOKENS = (
    "__write",
    "__edit",
    "__create",
    "__move",
    "__delete",
    "__remove",
    "__rename",
    "__execute",
    "__run",
    "__apply",
    "__update",
    "__insert",
    "__deploy",
)

_MCP_VERIFICATION_TOKENS = (
    "__read",
    "__list",
    "__search",
    "__get",
    "__stat",
    "__info",
    "__tree",
    "__query",
    "__audit",
)


def _is_verification_shell_command(command: str) -> bool:
    normalized = command.strip()
    if not normalized:
        return False
    if _contains_forbidden_shell_operator(normalized):
        return False
    segments = _split_shell_and_chain(normalized)
    if not segments:
        return False
    return all(_is_verification_shell_segment(segment) for segment in segments)


def _is_verification_shell_segment(segment: str) -> bool:
    normalized = segment.strip()
    if not normalized:
        return False
    try:
        parts = shlex.split(normalized)
    except ValueError:
        return False
    if not parts:
        return False
    read_only_commands = {
        "cat",
        "find",
        "grep",
        "ls",
        "pwd",
        "rg",
        "sed",
        "stat",
        "test",
        "wc",
    }
    if parts[0] in read_only_commands:
        return not _read_only_command_has_mutating_flags(parts)
    if normalized.startswith(("[ ", "[[ ")):
        return True
    verification_phrases = (
        "npm test",
        "npm run build",
        "npm run lint",
        "npm run typecheck",
        "pnpm test",
        "pnpm build",
        "pnpm lint",
        "pnpm typecheck",
        "pytest",
        "ruff check",
        "mypy",
        "cargo test",
        "cargo clippy",
        "go test",
        "go vet",
        "docker compose config",
        "python -m unittest",
        "python -m compileall",
        "./venv/bin/python -m unittest",
        "./venv/bin/python -m compileall",
    )
    return normalized.startswith(verification_phrases)


def _read_only_command_has_mutating_flags(parts: list[str]) -> bool:
    command = parts[0]
    flags = set(parts[1:])
    if command == "find":
        return any(flag in flags for flag in {"-delete", "-exec", "-execdir", "-ok", "-okdir"})
    if command == "sed":
        return any(flag == "-i" or flag.startswith("-i") for flag in parts[1:])
    return False


def _contains_forbidden_shell_operator(command: str) -> bool:
    in_single = False
    in_double = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if in_single or in_double:
            index += 1
            continue
        if char in {";", "|"}:
            return True
        if char == "&":
            if command.startswith("&&", index):
                index += 2
                continue
            return True
        if char in {"<", ">"}:
            return True
        if command.startswith("||", index):
            return True
        index += 1
    return False


def _split_shell_and_chain(command: str) -> list[str]:
    segments: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    start = 0
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if not in_single and not in_double and command.startswith("&&", index):
            segments.append(command[start:index].strip())
            index += 2
            start = index
            continue
        index += 1
    segments.append(command[start:].strip())
    return [segment for segment in segments if segment]


def _render_tool_specs(registry: ToolRegistry) -> str:
    return json.dumps(
        [
            {
                "name": spec.name,
                "description": spec.description[:500],
                "required": spec.input_schema.get("required", []),
                "properties": sorted(
                    (spec.input_schema.get("properties") or {}).keys()
                    if isinstance(spec.input_schema.get("properties"), dict)
                    else []
                ),
            }
            for spec in registry.specs()
        ],
        indent=2,
        sort_keys=True,
    )


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Could not find JSON object in model response: {text}")
    return stripped[start : end + 1]


def _extract_json_objects(text: str) -> list[str]:
    stripped = text.strip()
    objects: list[str] = []
    if stripped.startswith("```") or "```" in stripped:
        blocks = stripped.split("```")
        for block in blocks:
            candidate = block.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}") and candidate not in objects:
                objects.append(candidate)
    if stripped.startswith("{") and stripped.endswith("}") and stripped not in objects:
        objects.append(stripped)

    depth = 0
    start: int | None = None
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start : index + 1]
                    if candidate not in objects:
                        objects.append(candidate)
                    start = None
    return objects


def write_prompt_file(run_dir: Path, prompt: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
