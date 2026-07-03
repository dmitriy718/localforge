from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from localforge.config import load_config
from localforge.setup_wizard import (
    _credential_present,
    _enabled_mcp_names,
    _find_mcp_server,
    _read_env_file,
    _upsert_env_value,
    ensure_first_run_setup,
    setup_marker_path,
)


class ConfigTests(unittest.TestCase):
    def test_ollama_host_env_overrides_config_and_normalizes_scheme(self) -> None:
        previous = os.environ.get("OLLAMA_HOST")
        os.environ["OLLAMA_HOST"] = "host.docker.internal:11434"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "localforge.yaml"
                path.write_text(
                    """
workspace: .
backend:
  provider: ollama
  model: qwen2.5-coder:14b
  ollama_url: http://127.0.0.1:11434
""",
                    encoding="utf-8",
                )
                cfg = load_config(path)
        finally:
            if previous is None:
                os.environ.pop("OLLAMA_HOST", None)
            else:
                os.environ["OLLAMA_HOST"] = previous
        self.assertEqual(cfg.backend.ollama_url, "http://host.docker.internal:11434")

    def test_empty_ollama_host_env_is_ignored(self) -> None:
        previous = os.environ.get("OLLAMA_HOST")
        os.environ["OLLAMA_HOST"] = ""
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "localforge.yaml"
                path.write_text(
                    """
workspace: .
backend:
  provider: ollama
  model: qwen2.5-coder:14b
  ollama_url: http://127.0.0.1:11434
""",
                    encoding="utf-8",
                )
                cfg = load_config(path)
        finally:
            if previous is None:
                os.environ.pop("OLLAMA_HOST", None)
            else:
                os.environ["OLLAMA_HOST"] = previous
        self.assertEqual(cfg.backend.ollama_url, "http://127.0.0.1:11434")

    def test_context_window_defaults_to_agentic_size_and_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_path = Path(tmp) / "default.yaml"
            default_path.write_text("workspace: .\nbackend:\n  provider: ollama\n", encoding="utf-8")
            override_path = Path(tmp) / "override.yaml"
            override_path.write_text(
                """
workspace: .
backend:
  provider: ollama
  context_window_tokens: 65536
""",
                encoding="utf-8",
            )

            default_cfg = load_config(default_path)
            override_cfg = load_config(override_path)

        self.assertEqual(default_cfg.backend.context_window_tokens, 32768)
        self.assertEqual(override_cfg.backend.context_window_tokens, 65536)

    def test_setup_marker_path_lives_next_to_config(self) -> None:
        path = Path("/tmp/example/localforge.yaml")
        self.assertEqual(
            setup_marker_path(path),
            Path("/tmp/example/localforge.localconfig").resolve(),
        )

    def test_first_run_noninteractive_requires_setup_or_skip_env(self) -> None:
        previous = os.environ.pop("LOCALFORGE_SKIP_SETUP", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "localforge.yaml"
                config.write_text("workspace: .\n", encoding="utf-8")
                console = Mock()
                console.is_terminal = False
                with self.assertRaises(RuntimeError):
                    ensure_first_run_setup(config, console)
        finally:
            if previous is not None:
                os.environ["LOCALFORGE_SKIP_SETUP"] = previous

    def test_first_run_skip_env_bypasses_marker_requirement(self) -> None:
        previous = os.environ.get("LOCALFORGE_SKIP_SETUP")
        os.environ["LOCALFORGE_SKIP_SETUP"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "localforge.yaml"
                config.write_text("workspace: .\n", encoding="utf-8")
                console = Mock()
                console.is_terminal = False
                ensure_first_run_setup(config, console)
        finally:
            if previous is None:
                os.environ.pop("LOCALFORGE_SKIP_SETUP", None)
            else:
                os.environ["LOCALFORGE_SKIP_SETUP"] = previous

    def test_env_upsert_replaces_existing_value_and_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "# keep comments\nexport GITHUB_PERSONAL_ACCESS_TOKEN=old\nBRAVE_API_KEY=brave\n",
                encoding="utf-8",
            )

            _upsert_env_value(env_path, "GITHUB_PERSONAL_ACCESS_TOKEN", "new-secret")

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "# keep comments\nGITHUB_PERSONAL_ACCESS_TOKEN=new-secret\nBRAVE_API_KEY=brave\n",
            )
            backups = list(Path(tmp).glob(".env.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertIn("old", backups[0].read_text(encoding="utf-8"))

    def test_env_reader_handles_export_quotes_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                """
# comment
export GITHUB_PERSONAL_ACCESS_TOKEN='github-token'
BRAVE_API_KEY="brave-token"
invalid key=value
""",
                encoding="utf-8",
            )

            values = _read_env_file(env_path)

        self.assertEqual(values["GITHUB_PERSONAL_ACCESS_TOKEN"], "github-token")
        self.assertEqual(values["BRAVE_API_KEY"], "brave-token")
        self.assertNotIn("invalid key", values)

    def test_credential_present_checks_environment_before_env_file(self) -> None:
        previous = os.environ.get("NEON_API_KEY")
        os.environ["NEON_API_KEY"] = "from-environment"
        try:
            self.assertTrue(_credential_present("NEON_API_KEY", {}))
        finally:
            if previous is None:
                os.environ.pop("NEON_API_KEY", None)
            else:
                os.environ["NEON_API_KEY"] = previous
        self.assertTrue(_credential_present("NEON_API_KEY", {"NEON_API_KEY": "from-file"}))
        self.assertFalse(_credential_present("NEON_API_KEY", {"NEON_API_KEY": ""}))

    def test_mcp_profile_helpers_find_and_summarize_enabled_servers(self) -> None:
        config = {
            "mcp_servers": [
                {"name": "github", "enabled": True},
                {"name": "firecrawl", "enabled": False},
                {"name": "filesystem", "enabled": True},
            ]
        }

        github = _find_mcp_server(config, "github")

        self.assertIsNotNone(github)
        self.assertEqual(github["name"], "github")
        self.assertIsNone(_find_mcp_server(config, "missing"))
        self.assertEqual(_enabled_mcp_names(config), ["github", "filesystem"])


if __name__ == "__main__":
    unittest.main()
