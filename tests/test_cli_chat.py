from __future__ import annotations

import unittest

from pathlib import Path
import tempfile

from typer.testing import CliRunner

from localforge.cli import (
    _effective_config_path,
    _extract_agent_prompt,
    _is_simple_greeting,
    _with_extra_external_paths,
    app,
)
from localforge.config import HarnessConfig


class CliChatTests(unittest.TestCase):
    def test_simple_greetings_do_not_trigger_agent_runner(self) -> None:
        for prompt in ("hi", "Hi", "hello!", "hey?"):
            self.assertTrue(_is_simple_greeting(prompt))
        self.assertFalse(_is_simple_greeting("build me an api"))

    def test_agent_prompt_requires_explicit_run_or_build_prefix(self) -> None:
        self.assertIsNone(_extract_agent_prompt("hi"))
        self.assertIsNone(_extract_agent_prompt("Build me an API"))
        self.assertEqual(_extract_agent_prompt("/run Build me an API"), "Build me an API")
        self.assertEqual(_extract_agent_prompt("/build Build me an API"), "Build me an API")

    def test_default_config_path_is_localforge_yaml(self) -> None:
        self.assertEqual(_effective_config_path(None), Path("localforge.yaml"))
        self.assertEqual(_effective_config_path(Path("custom.yaml")), Path("custom.yaml"))

    def test_extra_external_paths_are_added_to_tool_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            cfg = HarnessConfig(workspace=workspace)
            external = Path(tmp) / "Desktop"

            updated = _with_extra_external_paths(cfg, [external])

        self.assertEqual(updated.tools.allow_external_paths, (external.resolve(),))

    def test_path_info_json_reports_existing_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "localforge.yaml"
            target = root / "projects"
            target.mkdir()
            config.write_text("workspace: .\n", encoding="utf-8")

            result = CliRunner().invoke(
                app,
                ["path-info", "projects", "--config", str(config), "--json"],
                env={"LOCALFORGE_SKIP_SETUP": "1"},
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"ok": true', result.output)
        self.assertIn('"is_dir": true', result.output)

    def test_runs_json_outputs_machine_readable_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "localforge.yaml"
            run_dir = root / "runs" / "20260703-000000-test"
            run_dir.mkdir(parents=True)
            config.write_text("workspace: .\n", encoding="utf-8")
            (run_dir / "events.jsonl").write_text(
                '{"event":"run_start","payload":{},"ts":"2026-01-01T00:00:00Z"}\n',
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                app,
                ["runs", "--config", str(config), "--limit", "1", "--json"],
                env={"LOCALFORGE_SKIP_SETUP": "1"},
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"run_id": "20260703-000000-test"', result.output)


if __name__ == "__main__":
    unittest.main()
