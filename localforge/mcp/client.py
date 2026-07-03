from __future__ import annotations

import itertools
import json
import os
import select
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from localforge.config import McpServerConfig
from localforge.models import RunContext, ToolResult, ToolSpec
from localforge.tools.base import Tool


@dataclass(frozen=True)
class McpRemoteTool:
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]


class McpStdioClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._ids = itertools.count(1)
        env = os.environ.copy()
        for key, value in config.env.items():
            if value == "" and key in os.environ:
                continue
            env[key] = value
        self._process = subprocess.Popen(
            config.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._lock = threading.Lock()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def list_tools(self) -> list[McpRemoteTool]:
        response = self._request("tools/list", {})
        tools = response.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError(f"MCP {self.config.name} returned invalid tools/list response: {response!r}")
        parsed: list[McpRemoteTool] = []
        for item in tools:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            schema = item.get("inputSchema")
            parsed.append(
                McpRemoteTool(
                    server_name=self.config.name,
                    name=item["name"],
                    description=str(item.get("description", "")),
                    input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                )
            )
        return parsed

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        response = self._request("tools/call", {"name": name, "arguments": arguments})
        content = response.get("content", [])
        if not isinstance(content, list):
            return json.dumps(response, indent=2, sort_keys=True)
        rendered: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                rendered.append(str(item.get("text", "")))
            else:
                rendered.append(json.dumps(item, sort_keys=True))
        return "\n".join(rendered)

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "localforge", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized", {})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        with self._lock:
            self._write(payload)
            while True:
                response = self._read()
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise RuntimeError(f"MCP {self.config.name} {method} failed: {response['error']}")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(f"MCP {self.config.name} {method} returned invalid result")
                return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise RuntimeError(f"MCP {self.config.name} stdin is closed")
        raw = json.dumps(payload)
        self._process.stdin.write(raw + "\n")
        self._process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        if self._process.stdout is None:
            raise RuntimeError(f"MCP {self.config.name} stdout is closed")
        while True:
            self._wait_for_stdout()
            line = self._process.stdout.readline()
            if line == "":
                stderr = ""
                if self._process.stderr is not None:
                    try:
                        stderr = self._process.stderr.read()
                    except OSError:
                        stderr = ""
                raise RuntimeError(f"MCP {self.config.name} exited while reading response. {stderr}")
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            return parsed

    def _wait_for_stdout(self) -> None:
        if self._process.stdout is None:
            raise RuntimeError(f"MCP {self.config.name} stdout is closed")
        ready, _, _ = select.select([self._process.stdout], [], [], self.config.startup_timeout_seconds)
        if not ready:
            raise TimeoutError(
                f"MCP {self.config.name} did not respond within "
                f"{self.config.startup_timeout_seconds:.1f}s"
            )


class McpToolAdapter(Tool):
    def __init__(self, client: McpStdioClient, remote_tool: McpRemoteTool) -> None:
        self.client = client
        self.remote_tool = remote_tool

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=f"mcp__{self.remote_tool.server_name}__{self.remote_tool.name}",
            description=f"MCP tool from {self.remote_tool.server_name}: {self.remote_tool.description}",
            input_schema=self.remote_tool.input_schema,
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        try:
            output = self.client.call_tool(self.remote_tool.name, dict(arguments))
        except Exception as exc:
            return ToolResult(self.spec.name, False, str(exc))
        return ToolResult(self.spec.name, True, output)
