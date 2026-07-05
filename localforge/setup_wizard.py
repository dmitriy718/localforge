from __future__ import annotations

import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt

from localforge.config import write_default_config

LOCALCONFIG_NAME = "localforge.localconfig"
SKIP_SETUP_ENV = "LOCALFORGE_SKIP_SETUP"
_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_SAFE_VALUE_PATTERN = re.compile(r"[A-Za-z0-9_./:@%+=,-]*")


@dataclass(frozen=True)
class CredentialedMcp:
    name: str
    env_key: str
    label: str
    description: str


CREDENTIAL_MCP_PROFILES = (
    CredentialedMcp(
        name="github",
        env_key="GITHUB_PERSONAL_ACCESS_TOKEN",
        label="GitHub",
        description="Repositories, issues, pull requests, workflow state, and code security.",
    ),
    CredentialedMcp(
        name="supabase",
        env_key="SUPABASE_ACCESS_TOKEN",
        label="Supabase",
        description="Supabase project and backend orchestration.",
    ),
    CredentialedMcp(
        name="brave_search",
        env_key="BRAVE_API_KEY",
        label="Brave Search",
        description="Live privacy-focused web search for troubleshooting and research.",
    ),
    CredentialedMcp(
        name="neon",
        env_key="NEON_API_KEY",
        label="Neon Postgres",
        description="Neon project, branch, schema, and query management.",
    ),
    CredentialedMcp(
        name="firecrawl",
        env_key="FIRECRAWL_API_KEY",
        label="Firecrawl",
        description="Website crawling, scraping, and Markdown extraction.",
    ),
)


def setup_marker_path(config_path: Path | None) -> Path:
    base = config_path.parent if config_path is not None else Path.cwd()
    return (base / LOCALCONFIG_NAME).resolve()


def ensure_first_run_setup(config_path: Path | None, console: Console) -> None:
    marker = setup_marker_path(config_path)
    if marker.exists() or os.environ.get(SKIP_SETUP_ENV) == "1":
        return
    if not console.is_terminal:
        raise RuntimeError(
            f"First-run setup is required, but this session is not interactive. "
            f"Run `localforge setup --config {config_path or 'localforge.yaml'}` in a terminal, "
            f"or set {SKIP_SETUP_ENV}=1 for CI/noninteractive checks."
        )
    run_setup_wizard(config_path, console, force=False)


def run_setup_wizard(config_path: Path | None, console: Console, *, force: bool = False) -> None:
    resolved_config = (config_path or Path("localforge.yaml")).resolve()
    marker = setup_marker_path(resolved_config)
    if marker.exists() and not force:
        console.print(f"[green]Setup already completed:[/green] {marker}")
        return

    console.print(
        Panel(
            "LocalForge needs a few local settings before the first real run.\n\n"
            "This wizard keeps setup local-first, avoids committing secrets, checks Ollama when "
            "available, and explains each decision as it goes.",
            title="LocalForge Setup",
        )
    )
    workspace = resolved_config.parent
    console.print(f"Workspace: [bold]{workspace}[/bold]")
    console.print(f"Config file: [bold]{resolved_config}[/bold]")

    if not resolved_config.exists():
        console.print("\nNo `localforge.yaml` was found, so I will create the default config.")
        write_default_config(resolved_config)
        console.print("[green]Created localforge.yaml[/green]")

    config_data = _read_yaml(resolved_config)
    backend = _mapping(config_data.setdefault("backend", {}))
    backend.setdefault("provider", "ollama")
    backend.setdefault("ollama_url", os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))

    selected_model = _configure_ollama_model(console, backend)
    if selected_model:
        backend["model"] = selected_model

    tools = _mapping(config_data.setdefault("tools", {}))
    if "allow_network_fetch" not in tools:
        tools["allow_network_fetch"] = Confirm.ask(
            "\nAllow LocalForge's built-in HTTP fetch tool? MCP search/scrape tools are still separately controlled",
            default=False,
        )

    _configure_optional_mcps(console, workspace, config_data)
    _write_yaml_with_backup(resolved_config, config_data)

    marker_data = {
        "version": 1,
        "setup_completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workspace": str(workspace),
        "config_path": str(resolved_config),
        "backend_provider": str(backend.get("provider", "")),
        "backend_model": str(backend.get("model", "")),
        "enabled_mcp_servers": _enabled_mcp_names(config_data),
        "notes": "Local machine setup marker. Do not store secrets here.",
    }
    _write_yaml_with_backup(marker, marker_data)
    console.print(
        Panel(
            "Setup is complete.\n\n"
            "Next useful commands:\n"
            "  localforge doctor --config localforge.yaml\n"
            "  localforge mcp-smoke --config localforge.yaml\n"
            "  localforge chat --config localforge.yaml",
            title="Ready",
            border_style="green",
        )
    )


def _configure_ollama_model(console: Console, backend: dict[str, Any]) -> str | None:
    console.print(
        "\n[bold]Model backend[/bold]\n"
        "LocalForge works best when Ollama is running with at least one downloaded model. "
        "I will check your configured Ollama URL and let you choose a model if models are found."
    )
    ollama_url = Prompt.ask(
        "Ollama URL",
        default=str(backend.get("ollama_url") or "http://127.0.0.1:11434"),
    ).strip()
    backend["ollama_url"] = ollama_url
    try:
        models = _ollama_models(ollama_url)
    except Exception as exc:
        console.print(f"[yellow]Could not reach Ollama yet:[/yellow] {exc}")
        console.print(
            "You can continue. Later, start Ollama and run `localforge doctor --config localforge.yaml`."
        )
        return Prompt.ask("Model name to keep in config", default=str(backend.get("model", ""))).strip()

    if not models:
        console.print("[yellow]Ollama responded, but no models were listed.[/yellow]")
        return Prompt.ask("Model name to keep in config", default=str(backend.get("model", ""))).strip()

    console.print("\nDetected Ollama models:")
    for index, model in enumerate(models, start=1):
        console.print(f"  {index}. {model}")
    current = str(backend.get("model") or models[0])
    default_index = models.index(current) + 1 if current in models else 1
    choice = IntPrompt.ask("Choose the default model", default=default_index)
    if choice < 1 or choice > len(models):
        console.print("[yellow]Invalid choice; keeping the existing/default model.[/yellow]")
        return current if current else models[0]
    return models[choice - 1]


def _configure_optional_mcps(console: Console, workspace: Path, config_data: dict[str, Any]) -> None:
    console.print(
        "\n[bold]Optional cloud MCPs[/bold]\n"
        "LocalForge ships with credentialed MCP profiles for GitHub, Supabase, Brave Search, "
        "Neon, and Firecrawl. They stay opt-in because they can read or change external systems. "
        "Secrets are stored in `.env`; localforge.yaml only stores enabled/disabled state."
    )
    env_path = workspace / ".env"
    env_values = _read_env_file(env_path)

    for profile in CREDENTIAL_MCP_PROFILES:
        server = _find_mcp_server(config_data, profile.name)
        if server is None:
            console.print(f"[yellow]Skipping {profile.label}: profile is not in localforge.yaml.[/yellow]")
            continue

        already_enabled = bool(server.get("enabled", False))
        has_secret = _credential_present(profile.env_key, env_values)
        console.print(f"\n[bold]{profile.label}[/bold]: {profile.description}")
        console.print(
            f"Credential: {profile.env_key} "
            f"({'present' if has_secret else 'not configured'})"
        )
        enable = Confirm.ask(
            f"Enable {profile.label} MCP?",
            default=already_enabled or has_secret,
        )
        if not enable:
            server["enabled"] = False
            continue

        if not has_secret:
            token = Prompt.ask(
                f"Paste {profile.label} credential, or press Enter to leave disabled",
                password=True,
                default="",
            ).strip()
            if not token:
                console.print(
                    f"[yellow]{profile.label} was not enabled because no credential was provided.[/yellow]"
                )
                server["enabled"] = False
                continue
            _upsert_env_value(env_path, profile.env_key, token)
            env_values[profile.env_key] = token
            console.print(f"[green]Stored {profile.env_key} in .env[/green]")

        server["enabled"] = True


def _ollama_models(url: str) -> list[str]:
    normalized = url if url.startswith(("http://", "https://")) else "http://" + url
    with httpx.Client(timeout=10) as client:
        response = client.get(f"{normalized.rstrip('/')}/api/tags")
        response.raise_for_status()
        data = response.json()
    return [
        str(item["name"])
        for item in data.get("models", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise ValueError("Expected YAML mapping")


def _write_yaml_with_backup(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, _backup_path(path))
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _upsert_env_value(path: Path, key: str, value: str) -> None:
    if not _ENV_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"Invalid .env key: {key}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"Invalid .env value for {key}: newlines are not allowed")
    lines: list[str] = []
    found = False
    if path.exists():
        shutil.copy2(path, _backup_path(path))
        lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"export {key}="):
            lines[index] = f"{key}={_format_env_value(value)}"
            found = True
            break
    if not found:
        lines.append(f"{key}={_format_env_value(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        try:
            parsed = shlex.split(stripped, comments=False, posix=True)
        except ValueError:
            continue
        if len(parsed) == 1:
            stripped = parsed[0]
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_PATTERN.fullmatch(key):
            continue
        values[key] = value.strip()
    return values


def _format_env_value(value: str) -> str:
    if value and _ENV_SAFE_VALUE_PATTERN.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _credential_present(key: str, env_values: dict[str, str]) -> bool:
    env_value = os.environ.get(key)
    file_value = env_values.get(key)
    return bool((env_value and env_value.strip()) or (file_value and file_value.strip()))


def _find_mcp_server(config_data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for server in config_data.get("mcp_servers", []):
        if isinstance(server, dict) and server.get("name") == name:
            return server
    return None


def _enabled_mcp_names(config_data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for server in config_data.get("mcp_servers", []):
        if isinstance(server, dict) and server.get("enabled") is True:
            names.append(str(server.get("name", "")))
    return names


def _backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S")
    candidate = path.with_suffix(path.suffix + f".{stamp}.bak")
    if not candidate.exists():
        return candidate
    for index in range(1, 1000):
        candidate = path.with_suffix(path.suffix + f".{stamp}.{index}.bak")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create a unique backup path for {path}")
