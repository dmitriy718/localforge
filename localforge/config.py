from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

import yaml


@dataclass(frozen=True)
class BackendConfig:
    provider: str = "ollama"
    model: str = "qwen2.5-coder:14b"
    ollama_url: str = "http://127.0.0.1:11434"
    llama_cpp_binary: str = "llama-cli"
    llama_cpp_model_path: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    context_window_tokens: int = 32768
    request_timeout_seconds: float = 300.0
    force_json: bool = False
    heartbeat_seconds: float = 10.0


@dataclass(frozen=True)
class ToolConfig:
    allow_shell: bool = True
    allow_file_write: bool = True
    allow_network_fetch: bool = False
    command_timeout_seconds: float = 120.0
    max_output_chars: int = 30000
    max_file_read_chars: int = 200000


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: list[str]
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    required_env: tuple[str, ...] = ()
    startup_timeout_seconds: float = 45.0


@dataclass(frozen=True)
class HarnessConfig:
    workspace: Path = Path(".")
    runs_dir: Path = Path("runs")
    projects_dir: Path = Path("projects")
    max_iterations: int = 30
    backend: BackendConfig = field(default_factory=BackendConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    mcp_servers: tuple[McpServerConfig, ...] = ()


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def load_config(path: Path | None) -> HarnessConfig:
    if path is None:
        return HarnessConfig()
    raw = _require_mapping(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))
    backend_raw = _require_mapping(raw.get("backend"), "backend")
    tools_raw = _require_mapping(raw.get("tools"), "tools")
    mcp_raw = raw.get("mcp_servers", [])
    if not isinstance(mcp_raw, list):
        raise ValueError("mcp_servers must be a list")
    base_dir = path.parent.resolve()
    workspace = Path(raw.get("workspace", "."))
    if not workspace.is_absolute():
        workspace = (base_dir / workspace).resolve()
    _load_dotenv(workspace / ".env")
    runs_dir = Path(raw.get("runs_dir", "runs"))
    projects_dir = Path(raw.get("projects_dir", "projects"))
    variables = {
        "WORKSPACE": str(workspace),
        "RUNS_DIR": str(runs_dir),
        "PROJECTS_DIR": str(projects_dir),
    }
    servers: list[McpServerConfig] = []
    for index, server in enumerate(mcp_raw):
        server_map = _require_mapping(server, f"mcp_servers[{index}]")
        command = server_map.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ValueError(f"mcp_servers[{index}].command must be a list of strings")
        required_env_raw = server_map.get("required_env", [])
        if not isinstance(required_env_raw, list) or not all(
            isinstance(item, str) for item in required_env_raw
        ):
            raise ValueError(f"mcp_servers[{index}].required_env must be a list of strings")
        servers.append(
            McpServerConfig(
                name=str(server_map["name"]),
                command=[_expand_value(part, variables) for part in command],
                enabled=bool(server_map.get("enabled", True)),
                env={
                    str(k): _expand_value(str(v), variables)
                    for k, v in _require_mapping(server_map.get("env"), "env").items()
                },
                description=str(server_map.get("description", "")),
                required_env=tuple(required_env_raw),
                startup_timeout_seconds=float(server_map.get("startup_timeout_seconds", 45.0)),
            )
        )

    return HarnessConfig(
        workspace=workspace,
        runs_dir=runs_dir,
        projects_dir=projects_dir,
        max_iterations=int(raw.get("max_iterations", 30)),
        backend=BackendConfig(
            provider=str(backend_raw.get("provider", "ollama")),
            model=str(backend_raw.get("model", BackendConfig.model)),
            ollama_url=_normalize_url(
                _first_nonempty(
                    os.environ.get("OLLAMA_HOST"),
                    str(backend_raw.get("ollama_url", "http://127.0.0.1:11434")),
                )
            ),
            llama_cpp_binary=str(backend_raw.get("llama_cpp_binary", "llama-cli")),
            llama_cpp_model_path=backend_raw.get("llama_cpp_model_path"),
            temperature=float(backend_raw.get("temperature", 0.2)),
            max_tokens=int(backend_raw.get("max_tokens", 4096)),
            context_window_tokens=int(backend_raw.get("context_window_tokens", 32768)),
            request_timeout_seconds=float(backend_raw.get("request_timeout_seconds", 300.0)),
            force_json=bool(backend_raw.get("force_json", False)),
            heartbeat_seconds=float(backend_raw.get("heartbeat_seconds", 10.0)),
        ),
        tools=ToolConfig(
            allow_shell=bool(tools_raw.get("allow_shell", True)),
            allow_file_write=bool(tools_raw.get("allow_file_write", True)),
            allow_network_fetch=bool(tools_raw.get("allow_network_fetch", False)),
            command_timeout_seconds=float(tools_raw.get("command_timeout_seconds", 120.0)),
            max_output_chars=int(tools_raw.get("max_output_chars", 30000)),
            max_file_read_chars=int(tools_raw.get("max_file_read_chars", 200000)),
        ),
        mcp_servers=tuple(servers),
    )


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_value(value: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return variables[name]
        return os.environ.get(name, "")

    return _ENV_PATTERN.sub(replace, value)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_no}: expected KEY=value")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env key on line {line_no}: {key}")
        if key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return "http://127.0.0.1:11434"


def _normalize_url(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return value.rstrip("/")
    return "http://" + value.rstrip("/")


def write_default_config(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing config: {path}")
    path.write_text(
        """# LocalForge production config.
workspace: .
runs_dir: runs
projects_dir: projects
max_iterations: 30

backend:
  provider: ollama
  model: qwen2.5-coder:14b
  ollama_url: http://127.0.0.1:11434
  llama_cpp_binary: llama-cli
  llama_cpp_model_path:
  temperature: 0.2
  max_tokens: 4096
  context_window_tokens: 32768
  request_timeout_seconds: 300
  force_json: false
  heartbeat_seconds: 10

tools:
  allow_shell: true
  allow_file_write: true
  allow_network_fetch: false
  command_timeout_seconds: 120
  max_output_chars: 30000
  max_file_read_chars: 200000

mcp_servers:
  - name: context7
    enabled: true
    description: Live, version-aware library documentation and code examples.
    command: ["npx", "-y", "@upstash/context7-mcp@latest"]
    env: {}

  - name: playwright
    enabled: true
    description: Browser automation, localhost UI inspection, console logs, and E2E workflows.
    command: ["npx", "-y", "@playwright/mcp@latest"]
    env: {}

  - name: github
    enabled: false
    description: Official GitHub MCP for repositories, issues, pull requests, Actions, and releases.
    command:
      - docker
      - run
      - -i
      - --rm
      - -e
      - GITHUB_PERSONAL_ACCESS_TOKEN
      - -e
      - GITHUB_TOOLSETS=repos,issues,pull_requests,actions,code_security
      - ghcr.io/github/github-mcp-server
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_PERSONAL_ACCESS_TOKEN}"
    required_env: ["GITHUB_PERSONAL_ACCESS_TOKEN"]

  - name: supabase
    enabled: false
    description: Supabase backend orchestration for development projects.
    command: ["npx", "-y", "@supabase/mcp-server-supabase@latest"]
    env:
      SUPABASE_ACCESS_TOKEN: "${SUPABASE_ACCESS_TOKEN}"
    required_env: ["SUPABASE_ACCESS_TOKEN"]

  - name: brave_search
    enabled: false
    description: Brave Search API web search for live troubleshooting and research.
    command: ["npx", "-y", "@modelcontextprotocol/server-brave-search@latest"]
    env:
      BRAVE_API_KEY: "${BRAVE_API_KEY}"
    required_env: ["BRAVE_API_KEY"]

  - name: filesystem
    enabled: true
    description: Local filesystem MCP access scoped to this workspace.
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem@latest", "${WORKSPACE}"]
    env: {}

  - name: shadcn
    enabled: true
    description: shadcn registry browsing and component installation.
    command: ["npx", "-y", "shadcn@latest", "mcp"]
    env: {}

  - name: neon
    enabled: false
    description: Neon Postgres project, branch, schema, and query management.
    command: ["npx", "-y", "@neondatabase/mcp-server-neon@latest"]
    env:
      NEON_API_KEY: "${NEON_API_KEY}"
    required_env: ["NEON_API_KEY"]

  - name: firecrawl
    enabled: false
    description: Firecrawl web scraping, crawling, and Markdown extraction.
    command: ["npx", "-y", "firecrawl-mcp@latest"]
    env:
      FIRECRAWL_API_KEY: "${FIRECRAWL_API_KEY}"
    required_env: ["FIRECRAWL_API_KEY"]

  - name: sequential_thinking
    enabled: true
    description: Structured sequential reasoning for complex debugging and architecture work.
    command: ["npx", "-y", "@modelcontextprotocol/server-sequential-thinking@latest"]
    env: {}
""",
        encoding="utf-8",
    )
