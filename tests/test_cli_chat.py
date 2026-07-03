from __future__ import annotations

import unittest

from pathlib import Path

from localforge.cli import _effective_config_path, _extract_agent_prompt, _is_simple_greeting


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


if __name__ == "__main__":
    unittest.main()
