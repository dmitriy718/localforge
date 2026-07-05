from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from localforge.config import McpServerConfig
from localforge.mcp.client import (
    McpRemoteTool,
    McpStdioClient,
    McpToolAdapter,
    McpToolCallResult,
    _command_requires_docker_daemon,
    _ensure_docker_daemon_available,
)
from localforge.models import RunContext


class InMemoryMcpClient(McpStdioClient):
    def __init__(self, result: McpToolCallResult) -> None:
        self.config = McpServerConfig(name="filesystem", command=["in-memory"])
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolCallResult:
        self.calls.append((name, arguments))
        return self.result

    def close(self) -> None:
        return None


def _remote_tool() -> McpRemoteTool:
    return McpRemoteTool(
        server_name="filesystem",
        name="create_directory",
        description="Create a directory.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )


class McpClientTests(unittest.TestCase):
    def test_detects_docker_run_mcp_command_as_daemon_backed(self) -> None:
        self.assertTrue(
            _command_requires_docker_daemon(["docker", "run", "-i", "--rm", "example/mcp"])
        )
        self.assertFalse(_command_requires_docker_daemon(["docker", "version"]))
        self.assertFalse(_command_requires_docker_daemon(["npx", "-y", "server"]))

    def test_docker_preflight_starts_docker_desktop_when_daemon_initially_down(self) -> None:
        down = Mock(returncode=1)
        up = Mock(returncode=0)
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: Any) -> Mock:
            calls.append(command)
            if command == ["docker", "info"]:
                return down if len([item for item in calls if item == ["docker", "info"]]) == 1 else up
            return Mock(returncode=0)

        with (
            patch("localforge.mcp.client.shutil.which", return_value="/usr/local/bin/docker"),
            patch("localforge.mcp.client.platform.system", return_value="Darwin"),
            patch("localforge.mcp.client.Path.exists", return_value=True),
            patch("localforge.mcp.client.subprocess.run", side_effect=fake_run),
            patch("localforge.mcp.client.time.sleep", return_value=None),
        ):
            _ensure_docker_daemon_available(5)

        self.assertIn(["open", "-ga", "Docker"], calls)
        self.assertGreaterEqual(calls.count(["docker", "info"]), 2)

    def test_docker_preflight_fails_actionably_when_daemon_stays_down(self) -> None:
        with (
            patch("localforge.mcp.client.shutil.which", return_value="/usr/local/bin/docker"),
            patch("localforge.mcp.client.platform.system", return_value="Linux"),
            patch("localforge.mcp.client.subprocess.run", return_value=Mock(returncode=1)),
        ):
            with self.assertRaisesRegex(RuntimeError, "Docker daemon is not reachable"):
                _ensure_docker_daemon_available(1)

    def test_adapter_preserves_mcp_is_error_as_failed_tool_result(self) -> None:
        client = InMemoryMcpClient(
            McpToolCallResult(
                ok=False,
                output="Input validation error: path is required",
                metadata={"is_error": True},
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)
            result = McpToolAdapter(client, _remote_tool()).run({}, context)

        self.assertFalse(result.ok)
        self.assertEqual(result.output, "Input validation error: path is required")
        self.assertEqual(result.metadata["is_error"], True)
        self.assertEqual(client.calls, [("create_directory", {})])

    def test_adapter_preserves_successful_mcp_tool_result(self) -> None:
        client = InMemoryMcpClient(
            McpToolCallResult(
                ok=True,
                output="Created directory /workspace/demo",
                metadata={"is_error": False},
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)
            result = McpToolAdapter(client, _remote_tool()).run({"path": "demo"}, context)

        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.metadata["is_error"], False)
        self.assertEqual(client.calls, [("create_directory", {"path": "demo"})])

    def test_stderr_drain_keeps_bounded_recent_tail(self) -> None:
        client = InMemoryMcpClient(McpToolCallResult(True, "", {}))
        client._stderr_tail = __import__("collections").deque(maxlen=3)

        class FakeStderr:
            def __iter__(self) -> object:
                return iter(["one\n", "two\n", "three\n", "four\n"])

        client._process = Mock()
        client._process.stderr = FakeStderr()

        client._drain_stderr()

        self.assertEqual(list(client._stderr_tail), ["two\n", "three\n", "four\n"])


if __name__ == "__main__":
    unittest.main()
