from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from localforge.agent import AgentRunner, interpret_action, parse_action
from localforge.backends.base import ModelBackend
from localforge.config import HarnessConfig, ToolConfig
from localforge.models import AgentEvent, Message
from localforge.tools.builtin import create_builtin_registry


class ScriptedBackend(ModelBackend):
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def generate(self, messages: list[Message]) -> str:
        if self.calls >= len(self.responses):
            raise AssertionError("No scripted response available")
        response = self.responses[self.calls]
        self.calls += 1
        return response


class SlowBackend(ModelBackend):
    def generate(self, messages: list[Message]) -> str:
        time.sleep(0.05)
        return '{"thought":"done","tool_calls":[],"final":"Completed."}'


class AgentProtocolTests(unittest.TestCase):
    def test_parse_action_accepts_valid_tool_call(self) -> None:
        action = parse_action(
            '{"thought":"inspect","tool_calls":[{"name":"list_files","arguments":{"path":"."}}],"final":null}'
        )
        self.assertEqual(action.thought, "inspect")
        self.assertEqual(action.tool_calls[0].name, "list_files")
        self.assertIsNone(action.final)

    def test_parse_action_rejects_invalid_json(self) -> None:
        with self.assertRaises(ValueError):
            parse_action("not json")

    def test_interpreter_accepts_direct_tool_object(self) -> None:
        action, error = interpret_action('{"name":"list_files","properties":{"path":"."}}', None)
        self.assertIsNone(error)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.tool_calls[0].name, "list_files")
        self.assertEqual(action.tool_calls[0].arguments, {"path": "."})

    def test_interpreter_extracts_markdown_json_tool_object(self) -> None:
        action, error = interpret_action(
            'I will inspect now.\n```json\n{"name":"list_files","arguments":{"path":"."}}\n```',
            None,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.tool_calls[0].name, "list_files")

    def test_interpreter_accepts_nested_model_action_object(self) -> None:
        raw = """{
          "metadata": {},
          "ok": true,
          "output": {
            "action": {
              "command": "mcp__filesystem__list_directory",
              "properties": {"path": "."}
            }
          },
          "tool": "agent_protocol"
        }"""
        action, error = interpret_action(raw, None)
        self.assertIsNone(error)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.tool_calls[0].name, "mcp__filesystem__list_directory")
        self.assertEqual(action.tool_calls[0].arguments, {"path": "."})

    def test_interpreter_accepts_slash_tool_command(self) -> None:
        action, error = interpret_action("/mcp__filesystem__list_directory", None)
        self.assertIsNone(error)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.tool_calls[0].name, "mcp__filesystem__list_directory")
        self.assertEqual(action.tool_calls[0].arguments, {"path": "."})

    def test_interpreter_accepts_bare_tool_command_from_code_block(self) -> None:
        registry = create_builtin_registry(ToolConfig())
        action, error = interpret_action("```bash\nlist_files\n```", registry)
        self.assertIsNone(error)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.tool_calls[0].name, "list_files")
        self.assertEqual(action.tool_calls[0].arguments, {"path": "."})

    def test_interpreter_parses_bare_tool_command_flags(self) -> None:
        registry = create_builtin_registry(ToolConfig())
        action, error = interpret_action("```bash\nlist_files --path docs\n```", registry)
        self.assertIsNone(error)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.tool_calls[0].arguments, {"path": "docs"})

    def test_interpreter_accepts_tool_mentioned_in_planning_prose(self) -> None:
        registry = create_builtin_registry(ToolConfig())
        action, error = interpret_action(
            "First, I'll use the list_files tool to inspect the workspace.",
            registry,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.tool_calls[0].name, "list_files")
        self.assertEqual(action.tool_calls[0].arguments, {"path": ".", "max_files": 300})

    def test_interpreter_ignores_echoed_tool_observation(self) -> None:
        action, error = interpret_action(
            '{"tool":"mcp__filesystem__list_directory","ok":true,"output":"[FILE] README.md"}',
            None,
        )
        self.assertIsNone(action)
        self.assertIsNotNone(error)

    def test_runner_executes_real_file_tool_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            backend = ScriptedBackend(
                [
                    '{"thought":"write file","tool_calls":[{"name":"write_file","arguments":{"path":"projects/demo/README.md","content":"# Demo\\n"}}],"final":null}',
                    '{"thought":"verified enough","tool_calls":[],"final":"Completed\\n\\nVerification: file written by tool."}',
                ]
            )
            cfg = HarnessConfig(
                workspace=workspace,
                runs_dir=Path("runs"),
                projects_dir=Path("projects"),
                max_iterations=5,
                tools=ToolConfig(allow_shell=False, allow_file_write=True),
            )
            runner = AgentRunner(cfg, backend)
            try:
                report = runner.run("make a demo")
            finally:
                runner.close()
            self.assertIn("Completed", report)
            self.assertEqual((workspace / "projects/demo/README.md").read_text(), "# Demo\n")
            run_dirs = list((workspace / "runs").iterdir())
            self.assertEqual(len(run_dirs), 1)
            self.assertTrue((run_dirs[0] / "events.jsonl").exists())
            self.assertTrue((run_dirs[0] / "transcript.json").exists())

    def test_runner_emits_live_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            backend = ScriptedBackend(
                [
                    '{"thought":"inspect","tool_calls":[{"name":"list_files","arguments":{"path":"."}}],"final":null}',
                    '{"thought":"done","tool_calls":[],"final":"Completed."}',
                ]
            )
            events: list[AgentEvent] = []
            cfg = HarnessConfig(
                workspace=workspace,
                runs_dir=Path("runs"),
                projects_dir=Path("projects"),
                max_iterations=5,
                tools=ToolConfig(allow_shell=False, allow_file_write=False),
            )
            runner = AgentRunner(cfg, backend, event_handler=events.append)
            try:
                runner.run("inspect only")
            finally:
                runner.close()
            event_types = [event.type for event in events]
            self.assertIn("run_start", event_types)
            self.assertIn("model_wait", event_types)
            self.assertIn("model_thought", event_types)
            self.assertIn("tool_start", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("final", event_types)
            self.assertIn("run_artifacts", event_types)

    def test_runner_accepts_natural_language_final_after_tool_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            backend = ScriptedBackend(
                [
                    '{"name":"list_files","arguments":{"path":"."}}',
                    "The workspace was inspected and contains no project files yet.",
                ]
            )
            cfg = HarnessConfig(
                workspace=workspace,
                runs_dir=Path("runs"),
                projects_dir=Path("projects"),
                max_iterations=5,
                tools=ToolConfig(allow_shell=False, allow_file_write=False),
            )
            runner = AgentRunner(cfg, backend)
            try:
                report = runner.run("inspect only")
            finally:
                runner.close()
            self.assertIn("workspace was inspected", report)

    def test_runner_emits_model_heartbeat_during_slow_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            events: list[AgentEvent] = []
            cfg = HarnessConfig(
                workspace=workspace,
                runs_dir=Path("runs"),
                projects_dir=Path("projects"),
                max_iterations=2,
            )
            cfg = HarnessConfig(
                workspace=cfg.workspace,
                runs_dir=cfg.runs_dir,
                projects_dir=cfg.projects_dir,
                max_iterations=cfg.max_iterations,
                backend=type(cfg.backend)(heartbeat_seconds=0.01),
                tools=cfg.tools,
                mcp_servers=cfg.mcp_servers,
            )
            runner = AgentRunner(cfg, SlowBackend(), event_handler=events.append)
            try:
                runner.run("slow")
            finally:
                runner.close()
            self.assertIn("model_heartbeat", [event.type for event in events])


if __name__ == "__main__":
    unittest.main()
