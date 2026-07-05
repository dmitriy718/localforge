from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localforge.config import ToolConfig
from localforge.models import RunContext
from localforge.tools.builtin import (
    ApplyPatchTool,
    CreateDirectoryTool,
    ListFilesTool,
    PathInfoTool,
    ReadFileTool,
    ShellTool,
    WriteFileTool,
)
from localforge.tools.base import ToolRegistry


class ToolTests(unittest.TestCase):
    def test_write_file_backs_up_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "test"
            context = RunContext("test", root, run_dir, False)
            target = root / "sample.txt"
            target.write_text("old", encoding="utf-8")
            result = WriteFileTool(ToolConfig()).run({"path": "sample.txt", "content": "new"}, context)
            self.assertTrue(result.ok, result.output)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertTrue((run_dir / "backups" / "sample.txt.bak").exists())

    def test_empty_file_write_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)
            result = WriteFileTool(ToolConfig()).run({"path": "empty.txt", "content": ""}, context)
            self.assertTrue(result.ok, result.output)
            self.assertEqual((root / "empty.txt").read_text(encoding="utf-8"), "")

    def test_read_file_reports_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)
            result = ReadFileTool(ToolConfig()).run({"path": "missing.txt"}, context)
            self.assertFalse(result.ok)
            self.assertIn("Failed to read", result.output)

    def test_shell_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)
            result = ShellTool(ToolConfig(allow_shell=False)).run({"cmd": "echo hi"}, context)
            self.assertFalse(result.ok)
            self.assertIn("disabled", result.output)

    def test_registry_converts_unexpected_tool_exception_to_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)
            registry = ToolRegistry()
            registry.register(ShellTool(ToolConfig()))

            result = registry.run("shell", {}, context)

            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["exception_type"], "ValueError")
            self.assertIn("unexpected error", result.output)

    def test_write_file_rejects_external_path_without_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            external = Path(tmp) / "external" / "sample.txt"
            context = RunContext("test", root, root / "runs" / "test", False)

            result = WriteFileTool(ToolConfig()).run(
                {"path": str(external), "content": "new"}, context
            )

            self.assertFalse(result.ok)
            self.assertIn("not allowlisted", result.output)
            self.assertFalse(external.exists())

    def test_write_file_allows_configured_external_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            external_root = Path(tmp) / "external"
            target = external_root / "sample.txt"
            context = RunContext("test", root, root / "runs" / "test", False)

            result = WriteFileTool(ToolConfig(allow_external_paths=(external_root,))).run(
                {"path": str(target), "content": "new"}, context
            )

            self.assertTrue(result.ok, result.output)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_shell_rejects_external_path_reference_without_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            external = Path(tmp) / "Desktop" / "demo"
            context = RunContext("test", root, root / "runs" / "test", False)

            result = ShellTool(ToolConfig()).run({"cmd": f"mkdir -p {external}"}, context)

            self.assertFalse(result.ok)
            self.assertIn("not allowlisted", result.output)
            self.assertFalse(external.exists())

    def test_shell_rejects_parent_relative_path_escape_without_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            context = RunContext("test", root, root / "runs" / "test", False)

            result = ShellTool(ToolConfig()).run({"cmd": "mkdir -p ../escaped"}, context)

            self.assertFalse(result.ok)
            self.assertIn("not allowlisted", result.output)
            self.assertFalse((Path(tmp) / "escaped").exists())

    def test_shell_allows_external_path_reference_when_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            external_root = Path(tmp) / "Desktop"
            external = external_root / "demo"
            context = RunContext("test", root, root / "runs" / "test", False)

            result = ShellTool(ToolConfig(allow_external_paths=(external_root,))).run(
                {"cmd": f"mkdir -p {external}"}, context
            )

            self.assertTrue(result.ok, result.output)
            self.assertTrue(external.is_dir())

    def test_list_files_supports_allowlisted_external_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            external_root = Path(tmp) / "Desktop"
            external_root.mkdir()
            target = external_root / "visible.txt"
            target.write_text("content", encoding="utf-8")
            context = RunContext("test", root, root / "runs" / "test", False)

            result = ListFilesTool(ToolConfig(allow_external_paths=(external_root,))).run(
                {"path": str(external_root)}, context
            )

            self.assertTrue(result.ok, result.output)
            self.assertIn(str(target.resolve()), result.output)

    def test_create_directory_is_idempotent_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)
            tool = CreateDirectoryTool(ToolConfig())

            first = tool.run({"path": "projects/demo"}, context)
            second = tool.run({"path": "projects/demo"}, context)

            self.assertTrue(first.ok, first.output)
            self.assertTrue(second.ok, second.output)
            self.assertFalse(first.metadata["existed_before"])
            self.assertTrue(second.metadata["existed_before"])
            self.assertTrue((root / "projects/demo").is_dir())

    def test_create_directory_rejects_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "occupied"
            target.write_text("content", encoding="utf-8")
            context = RunContext("test", root, root / "runs" / "test", False)

            result = CreateDirectoryTool(ToolConfig()).run({"path": "occupied"}, context)

            self.assertFalse(result.ok)
            self.assertIn("not a directory", result.output)

    def test_path_info_reports_existing_directory_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "projects"
            target.mkdir()
            context = RunContext("test", root, root / "runs" / "test", False)

            result = PathInfoTool(ToolConfig()).run({"path": "projects"}, context)

            self.assertTrue(result.ok, result.output)
            self.assertEqual(result.metadata["path"], str(target.resolve()))
            self.assertTrue(result.metadata["exists"])
            self.assertTrue(result.metadata["is_dir"])
            self.assertIn("mode", result.metadata)

    def test_path_info_fails_for_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = RunContext("test", root, root / "runs" / "test", False)

            result = PathInfoTool(ToolConfig()).run({"path": "missing"}, context)

            self.assertFalse(result.ok)
            self.assertFalse(result.metadata["exists"])

    def test_apply_patch_applies_valid_unified_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.txt"
            target.write_text("old\n", encoding="utf-8")
            context = RunContext("test", root, root / "runs" / "test", False)
            patch = """diff --git a/sample.txt b/sample.txt
--- a/sample.txt
+++ b/sample.txt
@@ -1 +1 @@
-old
+new
"""
            result = ApplyPatchTool(ToolConfig()).run({"patch": patch}, context)
            self.assertTrue(result.ok, result.output)
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")


if __name__ == "__main__":
    unittest.main()
