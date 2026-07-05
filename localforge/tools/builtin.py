from __future__ import annotations

import json
import ipaddress
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _resolve_workspace_path(workspace: Path, requested: str, config: ToolConfig) -> Path:
    path = Path(requested).expanduser()
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    if not _path_is_allowed(workspace, resolved, config):
        allowed = ", ".join(str(path) for path in config.allow_external_paths) or "none"
        raise ValueError(
            f"Path resolves outside the workspace and is not allowlisted: {resolved}. "
            f"Workspace: {workspace}. Allowed external paths: {allowed}."
        )
    return resolved


def _path_is_allowed(workspace: Path, path: Path, config: ToolConfig) -> bool:
    workspace = workspace.resolve()
    if path == workspace or workspace in path.parents:
        return True
    for allowed in config.allow_external_paths:
        allowed_resolved = allowed.expanduser().resolve()
        if path == allowed_resolved or allowed_resolved in path.parents:
            return True
    return False


def _disallowed_shell_path(command: str, workspace: Path, config: ToolConfig) -> Path | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for token in tokens:
        if "://" in token:
            continue
        path_candidate = _shell_token_path(token)
        if path_candidate is None:
            continue
        path = path_candidate.expanduser()
        if not path.is_absolute():
            path = workspace / path
        resolved = path.resolve()
        if not _path_is_allowed(workspace, resolved, config):
            return resolved
    return None


def _shell_token_path(token: str) -> Path | None:
    if token.startswith("-") or "=" in token:
        return None
    if token.startswith(("/", "~/", "$HOME/", "../")) or token in {".."}:
        return Path(token.replace("$HOME", "~", 1))
    return None


def _display_path(workspace: Path, path: Path) -> str:
    resolved_workspace = workspace.resolve()
    resolved_path = path.resolve()
    try:
        return str(resolved_path.relative_to(resolved_workspace))
    except ValueError:
        return str(resolved_path)


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
        try:
            cwd = (
                context.workspace
                if cwd_raw is None
                else _resolve_workspace_path(context.workspace, str(cwd_raw), self.config)
            )
        except ValueError as exc:
            return ToolResult("shell", False, str(exc))
        disallowed = _disallowed_shell_path(command, context.workspace, self.config)
        if disallowed is not None:
            return ToolResult(
                "shell",
                False,
                (
                    f"Shell command references a path outside the workspace that is not allowlisted: "
                    f"{disallowed}"
                ),
            )
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
        try:
            path = _resolve_workspace_path(
                context.workspace, _string_arg(arguments, "path"), self.config
            )
        except ValueError as exc:
            return ToolResult("read_file", False, str(exc))
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
        try:
            path = _resolve_workspace_path(
                context.workspace, _string_arg(arguments, "path"), self.config
            )
        except ValueError as exc:
            return ToolResult("write_file", False, str(exc))
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
            if not path.is_file():
                return ToolResult("write_file", False, f"Write verification failed; file is missing: {path}")
            if path.read_text(encoding="utf-8") != content:
                return ToolResult(
                    "write_file",
                    False,
                    f"Write verification failed; file content did not match requested content: {path}",
                )
        except OSError as exc:
            return ToolResult("write_file", False, f"Failed to write {path}: {exc}")
        metadata: dict[str, Any] = {"path": str(path), "chars": len(content)}
        if backup_path:
            metadata["backup"] = str(backup_path)
        return ToolResult("write_file", True, f"Wrote {len(content)} chars to {path}", metadata)


class CreateDirectoryTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="create_directory",
            description=(
                "Create a directory idempotently, including parents, then verify that the path "
                "exists and is a directory."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        if not self.config.allow_file_write:
            return ToolResult("create_directory", False, "Directory creation is disabled by config.")
        try:
            path = _resolve_workspace_path(
                context.workspace, _string_arg(arguments, "path"), self.config
            )
        except ValueError as exc:
            return ToolResult("create_directory", False, str(exc))
        existed_before = path.exists()
        if context.dry_run:
            return ToolResult("create_directory", True, f"DRY RUN: would create directory {path}")
        try:
            if existed_before and not path.is_dir():
                return ToolResult(
                    "create_directory",
                    False,
                    f"Path exists but is not a directory: {path}",
                    {"path": str(path), "exists": True, "is_dir": False},
                )
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                return ToolResult(
                    "create_directory",
                    False,
                    f"Directory verification failed after create: {path}",
                    {"path": str(path), "exists": path.exists(), "is_dir": path.is_dir()},
                )
        except OSError as exc:
            return ToolResult("create_directory", False, f"Failed to create directory {path}: {exc}")
        return ToolResult(
            "create_directory",
            True,
            f"Directory {'already existed' if existed_before else 'created'}: {path}",
            {"path": str(path), "exists": True, "is_dir": True, "existed_before": existed_before},
        )


class PathInfoTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="path_info",
            description=(
                "Verify whether a path exists and return file/directory metadata. Use this after "
                "creating files or directories before reporting success."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

    def run(self, arguments: dict[str, object], context: RunContext) -> ToolResult:
        try:
            path = _resolve_workspace_path(
                context.workspace, _string_arg(arguments, "path"), self.config
            )
        except ValueError as exc:
            return ToolResult("path_info", False, str(exc))
        exists = path.exists()
        metadata: dict[str, Any] = {
            "path": str(path),
            "exists": exists,
            "is_file": path.is_file(),
            "is_dir": path.is_dir(),
        }
        if exists:
            try:
                stat = path.stat()
            except OSError as exc:
                return ToolResult("path_info", False, f"Failed to stat {path}: {exc}", metadata)
            metadata.update(
                {
                    "size": stat.st_size,
                    "mode": oct(stat.st_mode & 0o777),
                    "mtime": stat.st_mtime,
                }
            )
        return ToolResult(
            "path_info",
            exists,
            f"{path}: {'exists' if exists else 'does not exist'}",
            metadata,
        )


class ListFilesTool(Tool):
    def __init__(self, config: ToolConfig) -> None:
        self.config = config

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
        try:
            root = _resolve_workspace_path(
                context.workspace, str(arguments.get("path", ".")), self.config
            )
        except ValueError as exc:
            return ToolResult("list_files", False, str(exc))
        max_files_raw = arguments.get("max_files", 300)
        max_files = int(max_files_raw) if isinstance(max_files_raw, int | float | str) else 300
        ignored = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
        files: list[str] = []
        try:
            for current, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in ignored]
                for filename in sorted(filenames):
                    files.append(_display_path(context.workspace, Path(current) / filename))
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
        try:
            path = _resolve_workspace_path(
                context.workspace, str(arguments.get("path", ".")), self.config
            )
        except ValueError as exc:
            return ToolResult("search", False, str(exc))
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
        validation_error = _validate_fetch_url(url, self.config)
        if validation_error:
            return ToolResult("fetch_url", False, validation_error)
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.get(url)
                redirect_count = 0
                while response.is_redirect:
                    redirect_url = response.headers.get("location")
                    if not redirect_url:
                        return ToolResult("fetch_url", False, "HTTP redirect response omitted Location header.")
                    next_url = str(response.url.join(redirect_url))
                    validation_error = _validate_fetch_url(next_url, self.config)
                    if validation_error:
                        return ToolResult("fetch_url", False, validation_error)
                    redirect_count += 1
                    if redirect_count > 5:
                        return ToolResult("fetch_url", False, "HTTP redirect limit exceeded.")
                    response = client.get(next_url)
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


def _validate_fetch_url(url: str, config: ToolConfig) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "Only http:// and https:// URLs are supported."
    if not parsed.hostname:
        return "URL must include a hostname."
    if config.allow_private_network_fetch:
        return None
    try:
        addresses = _resolve_host_addresses(parsed.hostname, parsed.port)
    except OSError as exc:
        return f"Could not resolve URL hostname {parsed.hostname}: {exc}"
    for address in addresses:
        if _is_private_or_local_address(address):
            return (
                f"Network fetch to private, local, link-local, reserved, or multicast address "
                f"is blocked by default: {parsed.hostname} resolved to {address}."
            )
    return None


def _resolve_host_addresses(hostname: str, port: int | None) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    infos = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for family, _type, _proto, _canonname, sockaddr in infos:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        raw_address = sockaddr[0]
        addresses.add(ipaddress.ip_address(raw_address))
    return addresses


def _is_private_or_local_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
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
    registry.register(CreateDirectoryTool(config))
    registry.register(PathInfoTool(config))
    registry.register(ApplyPatchTool(config))
    registry.register(ListFilesTool(config))
    registry.register(SearchTool(config))
    registry.register(FetchUrlTool(config))
    return registry
