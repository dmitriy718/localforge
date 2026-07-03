from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localforge.config import ToolConfig
from localforge.models import RunContext
from localforge.tools.builtin import ApplyPatchTool, ReadFileTool, ShellTool, WriteFileTool


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
