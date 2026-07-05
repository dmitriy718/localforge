from __future__ import annotations

import json
from pathlib import Path
import os
import time
from dataclasses import replace

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from localforge.backends.factory import create_backend
from localforge.backends.ollama import OllamaBackend
from localforge.config import HarnessConfig, load_config, write_default_config
from localforge.agent import AgentRunner
from localforge.audit import RunSummary, list_run_summaries, read_audit_events, summarize_run
from localforge.mcp.client import McpStdioClient, _command_requires_docker_daemon, _docker_info_ok
from localforge.models import AgentEvent
from localforge.models import Message, Role, RunContext
from localforge.setup_wizard import ensure_first_run_setup, run_setup_wizard
from localforge.tools.builtin import PathInfoTool

app = typer.Typer(help="LocalForge: local-first autonomous build harness.")
console = Console()
DEFAULT_CONFIG_PATH = Path("localforge.yaml")


@app.command()
def init(
    path: Path = typer.Option(Path("localforge.yaml"), "--path", "-p", help="Config path to create."),
) -> None:
    """Create a default production config."""
    write_default_config(path)
    console.print(f"Created {path}")


@app.command()
def setup(
    config: Path | None = typer.Option(Path("localforge.yaml"), "--config", "-c", help="Config file."),
    force: bool = typer.Option(False, "--force", help="Run setup even if localforge.localconfig exists."),
) -> None:
    """Run the interactive first-run setup wizard."""
    run_setup_wizard(config, console, force=force)


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
) -> None:
    """Verify local runtime dependencies and configured backend reachability."""
    _ensure_setup_or_exit(config)
    cfg = _load_config(config)
    backend = create_backend(cfg.backend)
    console.print(Panel.fit("LocalForge doctor"))
    console.print(f"Workspace: {cfg.workspace.resolve()}")
    console.print(f"Backend: {cfg.backend.provider}:{cfg.backend.model}")
    if isinstance(backend, OllamaBackend):
        try:
            models = backend.healthcheck()
        except Exception as exc:
            console.print(f"[red]Ollama healthcheck failed:[/red] {exc}")
            raise typer.Exit(1) from None
        if cfg.backend.model not in models:
            console.print(
                f"[yellow]Ollama is reachable, but configured model was not listed: {cfg.backend.model}[/yellow]"
            )
            console.print(models or "No Ollama models returned.")
            raise typer.Exit(2)
        console.print("[green]Ollama reachable and configured model is installed.[/green]")
    else:
        console.print("[green]Backend configuration loaded. Run command will verify generation.[/green]")
    docker_backed = [
        server.name
        for server in cfg.mcp_servers
        if server.enabled and _command_requires_docker_daemon(server.command)
    ]
    if docker_backed:
        if _docker_info_ok():
            console.print(
                "[green]Docker daemon reachable for Docker-backed MCPs:[/green] "
                + ", ".join(docker_backed)
            )
        else:
            console.print(
                "[red]Docker daemon is not reachable for Docker-backed MCPs:[/red] "
                + ", ".join(docker_backed)
            )
            console.print("Start Docker Desktop or run `localforge mcp-smoke` to trigger MCP startup preflight.")
            raise typer.Exit(3)


@app.command("mcp-list")
def mcp_list(
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
) -> None:
    """List configured MCP profiles and whether they can start with current env."""
    _ensure_setup_or_exit(config)
    cfg = _load_config(config)
    table = Table(title="Configured MCP servers")
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("Credential status")
    table.add_column("Command")
    for server in cfg.mcp_servers:
        missing = [
            name for name in server.required_env if not server.env.get(name) and not os.environ.get(name)
        ]
        if not server.required_env:
            credential_status = "not required"
        elif missing:
            credential_status = "missing: " + ", ".join(missing)
        else:
            credential_status = "present"
        table.add_row(
            server.name,
            "yes" if server.enabled else "no",
            credential_status,
            " ".join(server.command),
        )
    console.print(table)


@app.command("mcp-smoke")
def mcp_smoke(
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
) -> None:
    """Start enabled MCP servers and verify they return tool lists."""
    _ensure_setup_or_exit(config)
    cfg = _load_config(config)
    failures = 0
    for server in cfg.mcp_servers:
        if not server.enabled:
            console.print(f"[yellow]skip[/yellow] {server.name}: disabled")
            continue
        missing = [
            name for name in server.required_env if not server.env.get(name) and not os.environ.get(name)
        ]
        if missing:
            console.print(f"[yellow]skip[/yellow] {server.name}: missing {', '.join(missing)}")
            continue
        console.print(f"[cyan]start[/cyan] {server.name}")
        client: McpStdioClient | None = None
        try:
            client = McpStdioClient(server)
            tools = client.list_tools()
        except Exception as exc:
            failures += 1
            console.print(f"[red]fail[/red] {server.name}: {exc}")
        else:
            console.print(f"[green]ok[/green] {server.name}: {len(tools)} tools")
        finally:
            if client is not None:
                client.close()
    if failures:
        raise typer.Exit(1)


@app.command("runs")
def runs_command(
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Number of recent runs to show."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List recent LocalForge run audit summaries."""
    _ensure_setup_or_exit(config)
    cfg = _load_config(config)
    runs_dir = _resolve_workspace_path(cfg.workspace, cfg.runs_dir)
    summaries = list_run_summaries(runs_dir, limit=limit)
    if json_output:
        console.print(json.dumps([_run_summary_dict(summary) for summary in summaries], indent=2))
        return
    table = Table(title=f"LocalForge runs: {runs_dir}")
    table.add_column("Run ID")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Iters", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Final")
    for summary in summaries:
        errors = summary.tool_failures + summary.protocol_errors + summary.invalid_event_lines
        table.add_row(
            summary.run_id,
            summary.status,
            summary.started_at or "-",
            str(summary.iterations),
            str(summary.tool_calls),
            str(errors),
            summary.final_preview,
        )
    console.print(table)
    if not summaries:
        console.print("[yellow]No run directories found.[/yellow]")


@app.command("show-run")
def show_run_command(
    run_id: str = typer.Argument(..., help="Run directory name under the configured runs_dir."),
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
    tail: int = typer.Option(20, "--tail", "-n", min=1, help="Number of recent events to show."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Inspect one LocalForge run audit log."""
    _ensure_setup_or_exit(config)
    cfg = _load_config(config)
    runs_dir = _resolve_workspace_path(cfg.workspace, cfg.runs_dir)
    run_dir = _safe_run_dir(runs_dir, run_id)
    if not run_dir.exists() or not run_dir.is_dir():
        console.print(f"[red]Run not found:[/red] {run_id}")
        raise typer.Exit(1)

    summary = summarize_run(run_dir)
    transcript = run_dir / "transcript.json"
    events_path = run_dir / "events.jsonl"
    events, invalid = read_audit_events(events_path)
    if json_output:
        console.print(
            json.dumps(
                {
                    "summary": _run_summary_dict(summary),
                    "events_path": str(events_path),
                    "transcript_path": str(transcript) if transcript.exists() else None,
                    "events_tail": [
                        {"event": item.event, "ts": item.ts, "payload": item.payload}
                        for item in events[-tail:]
                    ],
                    "invalid_event_lines": invalid,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    console.print(Panel.fit(f"Run {summary.run_id}", title="LocalForge Run"))
    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Status", summary.status)
    table.add_row("Started", summary.started_at or "-")
    table.add_row("Completed", summary.completed_at or "-")
    table.add_row("Iterations", str(summary.iterations))
    table.add_row("Tool calls", str(summary.tool_calls))
    table.add_row("Tool failures", str(summary.tool_failures))
    table.add_row("Protocol errors", str(summary.protocol_errors))
    table.add_row("Invalid event lines", str(summary.invalid_event_lines))
    table.add_row("Events", str(events_path))
    table.add_row("Transcript", str(transcript) if transcript.exists() else "missing")
    if summary.final_preview:
        table.add_row("Final preview", summary.final_preview)
    console.print(table)

    if events:
        event_table = Table(title=f"Last {min(tail, len(events))} events")
        event_table.add_column("Timestamp")
        event_table.add_column("Event")
        event_table.add_column("Summary")
        for item in events[-tail:]:
            event_table.add_row(item.ts, item.event, _event_summary(item.payload))
        console.print(event_table)
    else:
        console.print("[yellow]No readable events found.[/yellow]")


@app.command()
def run(
    prompt: str = typer.Argument(..., help="What the local model should build."),
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Let tools report intended actions."),
    allow_external_path: list[Path] = typer.Option(
        None,
        "--allow-external-path",
        help="Temporarily allow a path outside the workspace for this run.",
    ),
) -> None:
    """Run the autonomous local builder."""
    _ensure_setup_or_exit(config)
    cfg = _with_extra_external_paths(_load_config(config), allow_external_path)
    backend = create_backend(cfg.backend)
    runner = AgentRunner(cfg, backend, event_handler=_print_event)
    try:
        report = runner.run(prompt, dry_run=dry_run)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted. LocalForge closed active MCP clients and wrote artifacts if a run had started.[/yellow]")
        raise typer.Exit(130) from None
    except Exception as exc:
        console.print(f"[red]LocalForge run failed:[/red] {exc}")
        raise typer.Exit(1) from None
    finally:
        runner.close()
    console.print(Panel(report, title="LocalForge Final Report"))


@app.command()
def chat(
    action: str | None = typer.Argument(None, help="Optional alias. Use `start` to launch chat."),
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Let tools report intended actions."),
    allow_external_path: list[Path] = typer.Option(
        None,
        "--allow-external-path",
        help="Temporarily allow a path outside the workspace for /run and /build turns.",
    ),
) -> None:
    """Start an interactive LocalForge chat session."""
    if action is not None and action != "start":
        console.print("[red]Unknown chat action:[/red] use `localforge chat` or `localforge chat start`.")
        raise typer.Exit(2)
    _ensure_setup_or_exit(config)
    cfg = _with_extra_external_paths(_load_config(config), allow_external_path)
    console.print(
        Panel.fit(
            "LocalForge Chat\n"
            "Type /exit to quit, /doctor for backend health, /mcp for MCP status.\n"
            "Use /run <task> or /build <task> when you want the autonomous builder."
        )
    )
    session_context: list[str] = []
    while True:
        prompt = Prompt.ask("[bold cyan]you[/bold cyan]").strip()
        if not prompt:
            continue
        if prompt in {"/exit", "/quit", "exit", "quit"}:
            console.print("Session ended.")
            return
        if prompt == "/doctor":
            doctor(config)
            continue
        if prompt == "/mcp":
            mcp_list(config)
            continue
        if _is_simple_greeting(prompt):
            response = (
                "Hi. I am here. Use `/run <task>` when you want me to build or change files, "
                "or ask a normal question here."
            )
            console.print(Panel(response, title="LocalForge"))
            session_context.append(f"User: {prompt}\nLocalForge: {response}")
            continue

        agent_prompt = _extract_agent_prompt(prompt)
        if agent_prompt is None:
            backend_cfg = replace(cfg.backend, max_tokens=min(cfg.backend.max_tokens, 2048))
            backend = create_backend(backend_cfg)
            try:
                report = _direct_chat_response(backend, session_context, prompt)
            except KeyboardInterrupt:
                console.print("[yellow]Interrupted current turn.[/yellow]")
                continue
            except Exception as exc:
                console.print(f"[red]LocalForge chat failed:[/red] {exc}")
                continue
            console.print(Panel(report, title="LocalForge"))
            session_context.append(f"User: {prompt}\nLocalForge: {report}")
            continue

        effective_prompt = agent_prompt
        if session_context:
            effective_prompt = (
                "Conversation context from earlier LocalForge turns:\n"
                + "\n\n".join(session_context[-6:])
                + "\n\nNew user request:\n"
                + agent_prompt
            )
        backend = create_backend(cfg.backend)
        runner = AgentRunner(cfg, backend, event_handler=_print_event)
        try:
            report = runner.run(effective_prompt, dry_run=dry_run)
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted current turn.[/yellow]")
            continue
        except Exception as exc:
            console.print(f"[red]LocalForge turn failed:[/red] {exc}")
            continue
        finally:
            runner.close()
        console.print(Panel(report, title="LocalForge"))
        session_context.append(f"User: {agent_prompt}\nLocalForge: {report}")


@app.command("path-info")
def path_info_command(
    path: str = typer.Argument(..., help="Workspace-relative, absolute, or allowlisted path to inspect."),
    config: Path | None = typer.Option(None, "--config", "-c", help="Config file."),
    allow_external_path: list[Path] = typer.Option(
        None,
        "--allow-external-path",
        help="Temporarily allow a path outside the workspace for this check.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Verify a filesystem path with the same policy used by built-in tools."""
    _ensure_setup_or_exit(config)
    cfg = _with_extra_external_paths(_load_config(config), allow_external_path)
    context = RunContext("cli-path-info", cfg.workspace.resolve(), cfg.workspace / cfg.runs_dir, False)
    result = PathInfoTool(cfg.tools).run({"path": path}, context)
    if json_output:
        console.print(
            json.dumps(
                {
                    "ok": result.ok,
                    "output": result.output,
                    "metadata": result.metadata,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        style = "green" if result.ok else "red"
        console.print(f"[{style}]{result.output}[/{style}]")
        if result.metadata:
            table = Table(show_header=False)
            table.add_column("Field")
            table.add_column("Value")
            for key, value in result.metadata.items():
                table.add_row(str(key), str(value))
            console.print(table)
    if not result.ok:
        raise typer.Exit(1)


def _print_event(event: AgentEvent) -> None:
    timestamp = time.strftime("%H:%M:%S")
    style = {
        "run_start": "bold blue",
        "mcp_start": "cyan",
        "mcp_ready": "green",
        "mcp_skip": "yellow",
        "model_wait": "magenta",
        "model_heartbeat": "magenta",
        "model_output": "white",
        "model_thought": "bold",
        "tool_start": "cyan",
        "tool_result": "green",
        "protocol_error": "red",
        "interpretation_miss": "yellow",
        "final": "bold green",
        "run_artifacts": "blue",
        "run_incomplete": "red",
    }.get(event.type, "white")
    if event.type == "model_output":
        console.print(f"[dim]{timestamp}[/dim] [{style}]{event.type}[/] model responded")
        console.print(Panel(event.message, title="Model", border_style="white"))
        return
    console.print(f"[dim]{timestamp}[/dim] [{style}]{event.type}[/] {event.message}")
    if event.type == "tool_result":
        preview = str(event.data.get("output_preview", "")).strip()
        if preview:
            console.print(f"[dim]{preview[:1200]}[/dim]")


def _ensure_setup_or_exit(config: Path | None) -> None:
    try:
        ensure_first_run_setup(_effective_config_path(config), console)
    except RuntimeError as exc:
        console.print(f"[red]Setup required:[/red] {exc}")
        raise typer.Exit(1) from None


def _effective_config_path(config: Path | None) -> Path:
    return config or DEFAULT_CONFIG_PATH


def _load_config(config: Path | None) -> HarnessConfig:
    return load_config(_effective_config_path(config))


def _with_extra_external_paths(cfg: HarnessConfig, paths: list[Path] | None) -> HarnessConfig:
    if not paths:
        return cfg
    resolved = tuple(path.expanduser().resolve() for path in paths)
    return replace(
        cfg,
        tools=replace(
            cfg.tools,
            allow_external_paths=cfg.tools.allow_external_paths + resolved,
        ),
    )


def _run_summary_dict(summary: RunSummary) -> dict[str, object]:
    return {
        "run_id": summary.run_id,
        "run_dir": str(summary.run_dir),
        "status": summary.status,
        "started_at": summary.started_at,
        "completed_at": summary.completed_at,
        "iterations": summary.iterations,
        "tool_calls": summary.tool_calls,
        "tool_failures": summary.tool_failures,
        "protocol_errors": summary.protocol_errors,
        "invalid_event_lines": summary.invalid_event_lines,
        "final_preview": summary.final_preview,
    }


def _is_simple_greeting(prompt: str) -> bool:
    normalized = prompt.strip().lower().strip("!.?, ")
    return normalized in {"hi", "hello", "hey", "yo", "sup", "howdy"}


def _extract_agent_prompt(prompt: str) -> str | None:
    stripped = prompt.strip()
    for prefix in ("/run ", "/build "):
        if stripped.startswith(prefix):
            task = stripped[len(prefix) :].strip()
            if not task:
                raise typer.BadParameter(f"{prefix.strip()} requires a task")
            return task
    return None


def _direct_chat_response(backend: object, session_context: list[str], prompt: str) -> str:
    if not hasattr(backend, "generate"):
        raise RuntimeError("Configured backend does not implement generate")
    context = "\n\n".join(session_context[-4:])
    messages = [
        Message(
            Role.SYSTEM,
            "You are LocalForge chat mode. Answer directly and concisely. "
            "Do not claim you changed files, ran tools, inspected files, or verified anything. "
            "If the user wants software built or files changed, tell them to use /run <task>.",
        )
    ]
    if context:
        messages.append(Message(Role.USER, "Recent chat context:\n" + context))
    messages.append(Message(Role.USER, prompt))
    response = backend.generate(messages)
    if not isinstance(response, str):
        raise RuntimeError("Configured backend returned a non-string response")
    return response.strip()


def _resolve_workspace_path(workspace: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    return (workspace / path).resolve()


def _safe_run_dir(runs_dir: Path, run_id: str) -> Path:
    candidate = (runs_dir / run_id).resolve()
    if runs_dir.resolve() not in (candidate, *candidate.parents):
        raise typer.BadParameter("run_id must resolve inside the configured runs_dir")
    return candidate


def _event_summary(payload: dict[str, object]) -> str:
    for key in ("final", "reason", "error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _compact(value)
    response = payload.get("response")
    if isinstance(response, str) and response.strip():
        return _compact(response)
    result = payload.get("result")
    if isinstance(result, dict):
        name = result.get("name")
        ok = result.get("ok")
        output = result.get("output")
        parts = []
        if isinstance(name, str):
            parts.append(name)
        if isinstance(ok, bool):
            parts.append("ok" if ok else "failed")
        if isinstance(output, str) and output.strip():
            parts.append(_compact(output))
        if parts:
            return " | ".join(parts)
    name = payload.get("name")
    if isinstance(name, str):
        return name
    server = payload.get("server")
    if isinstance(server, str):
        return server
    return _compact(str(payload))


def _compact(value: str, *, limit: int = 120) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


if __name__ == "__main__":
    app()
