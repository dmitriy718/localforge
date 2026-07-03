from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from localforge.config import ToolConfig
from localforge.models import RunContext, ToolResult, ToolSpec
from localforge.tools.base import Tool, ToolRegistry


def _string_arg(
    arguments: dict[str, object], name: str, *, default: str | None = None, allow_empty: bool = False
) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or (value == "" and not allow_empty):
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _bool_arg(arguments: dict[str, object], name: str, *, default: bool = False) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _resolve_workspace_path(workspace: Path, requested: str) -> Path:
    path = Path(requested)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


class ShellTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="shell",
            description=(
                "Run a local shell command from the workspace. Use for builds, tests, searches, "
                "package manager commands, git inspection, and local automation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "cwd": {"type": "string", "description": "Optional working directory."},
                },
                "required": ["cmd"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        if not self.config.allow_shell:
            return ToolResult("shell", False, "Shell execution is disabled by config.")
        command = _string_arg(arguments, "cmd")
        cwd_raw = arguments.get("cwd")
        cwd = context.workspace if cwd_raw is None else _resolve_workspace_path(context.workspace, str(cwd_raw))
        if context.dry_run:
            return ToolResult("shell", True, f"DRY RUN: would execute in {cwd}: {command}")
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            executable=os.environ.get("SHELL", "/bin/bash"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.config.command_timeout_seconds,
            check=False,
        )
        output = completed.stdout
        if completed.stderr:
            output += ("\nSTDERR:\n" if output else "STDERR:\n") + completed.stderr
        return ToolResult(
            "shell",
            completed.returncode == 0,
            _truncate(output, self.config.max_output_chars),
            {"returncode": completed.returncode, "cwd": str(cwd)},
        )


class ReadFileTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from disk.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        path = _resolve_workspace_path(context.workspace, _string_arg(arguments, "path"))
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult("read_file", False, f"File is not valid UTF-8: {path}")
        except OSError as exc:
            return ToolResult("read_file", False, f"Failed to read {path}: {exc}")
        return ToolResult(
            "read_file",
            True,
            _truncate(content, self.config.max_file_read_chars),
            {"path": str(path), "size": path.stat().st_size},
        )


class WriteFileTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_file",
            description=(
                "Create or replace a UTF-8 text file. Parent directories are created. "
                "Existing files are backed up in the run directory before replacement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        if not self.config.allow_file_write:
            return ToolResult("write_file", False, "File writing is disabled by config.")
        path = _resolve_workspace_path(context.workspace, _string_arg(arguments, "path"))
        content = _string_arg(arguments, "content", default="", allow_empty=True)
        if context.dry_run:
            return ToolResult("write_file", True, f"DRY RUN: would write {len(content)} chars to {path}")
        backup_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                rel = path.name + ".bak"
                backup_dir = context.run_dir / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / rel
                counter = 1
                while backup_path.exists():
                    backup_path = backup_dir / f"{path.name}.{counter}.bak"
                    counter += 1
                shutil.copy2(path, backup_path)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult("write_file", False, f"Failed to write {path}: {exc}")
        metadata: dict[str, Any] = {"path": str(path), "chars": len(content)}
        if backup_path:
            metadata["backup"] = str(backup_path)
        return ToolResult("write_file", True, f"Wrote {len(content)} chars to {path}", metadata)


class ListFilesTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_files",
            description="List files under a directory, excluding common generated folders.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_files": {"type": "integer"},
                },
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        root = _resolve_workspace_path(context.workspace, str(arguments.get("path", ".")))
        max_files_raw = arguments.get("max_files", 300)
        max_files = int(max_files_raw) if isinstance(max_files_raw, int | float | str) else 300
        ignored = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
        files: list[str] = []
        try:
            for current, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in ignored]
                for filename in sorted(filenames):
                    files.append(str((Path(current) / filename).relative_to(context.workspace)))
                    if len(files) >= max_files:
                        return ToolResult("list_files", True, "\n".join(files), {"truncated": True})
        except OSError as exc:
            return ToolResult("list_files", False, f"Failed to list files: {exc}")
        return ToolResult("list_files", True, "\n".join(files), {"truncated": False})


class SearchTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search",
            description="Search text in files using ripgrep when available, falling back to Python.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        pattern = _string_arg(arguments, "pattern")
        path = _resolve_workspace_path(context.workspace, str(arguments.get("path", ".")))
        if shutil.which("rg"):
            completed = subprocess.run(
                ["rg", "--line-number", "--hidden", "-g", "!venv", "-g", "!node_modules", pattern, str(path)],
                cwd=context.workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
            output = completed.stdout or completed.stderr
            ok = completed.returncode in {0, 1}
            return ToolResult("search", ok, _truncate(output, self.config.max_output_chars))

        matches: list[str] = []
        for candidate in path.rglob("*"):
            if not candidate.is_file() or any(part in {"venv", "node_modules"} for part in candidate.parts):
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append(f"{candidate}:{line_no}:{line}")
        return ToolResult("search", True, _truncate("\n".join(matches), self.config.max_output_chars))


class FetchUrlTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="fetch_url",
            description="Fetch a URL over HTTP(S) when network fetch is enabled.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        if not self.config.allow_network_fetch:
            return ToolResult("fetch_url", False, "Network fetch is disabled by config.")
        url = _string_arg(arguments, "url")
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                text = response.text
        except httpx.HTTPError as exc:
            return ToolResult("fetch_url", False, f"HTTP fetch failed: {exc}")
        return ToolResult(
            "fetch_url",
            True,
            _truncate(text, self.config.max_output_chars),
            {"url": url, "status_code": response.status_code},
        )


class WriteJsonTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_json",
            description="Write structured JSON to a file with stable formatting.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "data": {"type": "object"}},
                "required": ["path", "data"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        data = arguments.get("data")
        if not isinstance(data, dict):
            return ToolResult("write_json", False, "data must be an object")
        content = json.dumps(data, indent=2, sort_keys=True) + "\n"
        return WriteFileTool(self.config).run(
            {"path": _string_arg(arguments, "path"), "content": content}, context
        )


class ApplyPatchTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="apply_patch",
            description=(
                "Apply a unified diff patch to the workspace using git apply. "
                "The patch is validated before application."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Unified diff text accepted by git apply.",
                    }
                },
                "required": ["patch"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        if not self.config.allow_file_write:
            return ToolResult("apply_patch", False, "Patch application is disabled by config.")
        patch = _string_arg(arguments, "patch")
        if context.dry_run:
            check = self._git_apply(context.workspace, patch, ["--check"])
            return ToolResult(
                "apply_patch",
                check.returncode == 0,
                "DRY RUN: patch validates." if check.returncode == 0 else check.stderr,
                {"returncode": check.returncode},
            )
        check = self._git_apply(context.workspace, patch, ["--check"])
        if check.returncode != 0:
            return ToolResult(
                "apply_patch",
                False,
                _truncate(check.stderr or check.stdout, self.config.max_output_chars),
                {"returncode": check.returncode, "phase": "check"},
            )
        applied = self._git_apply(context.workspace, patch, [])
        ok = applied.returncode == 0
        output = applied.stdout
        if applied.stderr:
            output += ("\nSTDERR:\n" if output else "STDERR:\n") + applied.stderr
        return ToolResult(
            "apply_patch",
            ok,
            _truncate(output or ("Patch applied." if ok else "Patch failed."), self.config.max_output_chars),
            {"returncode": applied.returncode},
        )

    def _git_apply(self, workspace: Path, patch: str, extra_args: list[str]) -> subprocess.CompletedProcess[str]:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
            handle.write(patch)
            patch_path = handle.name
        try:
            return subprocess.run(
                ["git", "apply", *extra_args, patch_path],
                cwd=workspace,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
        finally:
            try:
                Path(patch_path).unlink()
            except OSError:
                pass


def create_builtin_registry(config: ToolConfig) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ShellTool(config))
    registry.register(ReadFileTool(config))
    registry.register(WriteFileTool(config))
    registry.register(WriteJsonTool(config))
    registry.register(ApplyPatchTool(config))
    registry.register(ListFilesTool())
    registry.register(SearchTool(config))
    registry.register(FetchUrlTool(config))
    return registry
