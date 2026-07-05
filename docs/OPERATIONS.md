# LocalForge Operations

## Runtime requirements

- Python 3.11 or newer.
- A local model backend:
  - Ollama reachable at `backend.ollama_url`, or
  - llama.cpp `llama-cli` plus a local `.gguf` model path.
- A dedicated workspace directory for model-generated projects.

## First-run setup

LocalForge checks for `localforge.localconfig` next to the active config file. If the marker is missing and the terminal is interactive, the setup wizard runs automatically.

Manual setup:

```bash
localforge setup
```

Rerun setup:

```bash
localforge setup --force
```

The wizard creates or updates:

- `localforge.yaml`: runtime configuration.
- `.env`: optional MCP credentials such as `GITHUB_PERSONAL_ACCESS_TOKEN`, `SUPABASE_ACCESS_TOKEN`, `BRAVE_API_KEY`, `NEON_API_KEY`, and `FIRECRAWL_API_KEY`.
- `localforge.localconfig`: local setup marker and non-secret setup metadata.

Existing `localforge.yaml`, `.env`, and `localforge.localconfig` files are backed up before the wizard modifies them. The marker intentionally stores no secrets.

For CI or noninteractive deployment checks, set `LOCALFORGE_SKIP_SETUP=1`.

## Health checks

```bash
make doctor
localforge mcp-list
localforge mcp-smoke
make compile
make lint
make typecheck
make test
./scripts/smoke.sh
```

`doctor` verifies that Ollama is reachable and that the configured model appears in `/api/tags`.
`mcp-smoke` starts every enabled MCP server and verifies that each one returns a tool list.

## Running a build

```bash
make run PROMPT='Build a production-ready FastAPI API in projects/api with tests and docs.'
```

For interactive operation:

```bash
localforge chat start
```

Use dry-run for protocol and planning checks:

```bash
make dry-run PROMPT='Inspect this workspace and propose improvements.'
```

## Audit artifacts

Each run creates:

- `runs/<run-id>/events.jsonl`: structured event log with model responses, tool calls, and tool results.
- `runs/<run-id>/transcript.json`: complete chat transcript.
- `runs/<run-id>/backups/`: backups of overwritten files.

Persist `runs/` if you need traceability, debugging, or commercial support workflows.

Inspect recent runs:

```bash
localforge runs --limit 10
localforge runs --limit 10 --json
localforge show-run <run-id> --tail 20
localforge show-run <run-id> --tail 20 --json
```

`runs` summarizes status, iterations, tool calls, and detected tool/protocol errors. `show-run` prints artifact paths and the latest audit events for one run.

## Filesystem safety

Built-in file tools are workspace-confined by default. Absolute paths, `~` paths, and shell command path references outside the workspace are rejected unless they live under `tools.allow_external_paths`.

Example for allowing a Desktop export directory:

```yaml
tools:
  allow_external_paths:
    - /Users/dima/Desktop
```

Keep this list as narrow as possible. Prefer using workspace-relative paths under `projects/` for generated code.

For one-off operations, prefer a temporary grant:

```bash
localforge run \
  --allow-external-path /Users/dima/Desktop \
  'Create a verified folder on my Desktop named localforge-check.'
```

Check a path directly:

```bash
localforge path-info projects
localforge path-info /Users/dima/Desktop/localforge-check \
  --allow-external-path /Users/dima/Desktop \
  --json
```

Use `create_directory` instead of shell `mkdir` when possible. It is idempotent and verifies that the path is a directory before returning success. Use `path_info` after filesystem mutations when the agent needs to prove that a file or directory exists.

After a successful mutation tool call, LocalForge blocks final success reports until the agent runs a successful verification tool call. This prevents runs from ending with claims like "folder created" when no postcondition was checked.

## Docker usage

Build:

```bash
docker build -t localforge:0.1.0 .
```

Run against host Ollama on macOS:

```bash
docker run --rm -it \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -v "$PWD:/workspace" \
  localforge:0.1.0 \
  run --config /workspace/localforge.yaml \
  'Inspect the workspace and report status.'
```

If the container cannot reach host Ollama, set `backend.ollama_url` to the appropriate host gateway URL for your Docker runtime.

The config loader honors `OLLAMA_HOST`, so Docker can use:

```bash
docker run --rm -it \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -v "$PWD:/workspace" \
  localforge:0.1.0 \
  doctor --config /workspace/localforge.yaml
```

Compose smoke:

```bash
docker compose run --rm localforge doctor --config /workspace/localforge.yaml
```

If the GitHub MCP profile is enabled, containerized LocalForge also needs Docker CLI access because the official GitHub MCP profile launches `ghcr.io/github/github-mcp-server`. The image includes `docker-cli`; mount the host Docker socket when you want that MCP available:

```bash
docker run --rm -it \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -e LOCALFORGE_SKIP_SETUP=1 \
  -v "$PWD:/workspace" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  localforge:0.1.0 \
  mcp-smoke --config /workspace/localforge.yaml
```

Mounting the Docker socket is powerful. Only do this in a workspace and host account where LocalForge is trusted to manage development resources.

On macOS local runs, Docker-backed MCP startup checks `docker info`. If the daemon is down and Docker Desktop is installed, LocalForge opens Docker Desktop and waits for the daemon before launching the MCP server. If startup still fails, `mcp-smoke` reports the daemon failure explicitly instead of allowing a false healthy state.

## MCP operations

MCP servers are configured explicitly in YAML. LocalForge starts each enabled server as a child process, lists its tools, registers them into the model tool manifest, and shuts the server down when the run completes.

If an MCP server cannot start or returns invalid JSON-RPC, the run fails with a concrete error. This is intentional; hidden tool degradation makes agent output untrustworthy.

## Security posture

LocalForge is designed for trusted local autonomy. With shell enabled, the model can execute commands as the operating-system user running the harness.

Recommended deployment controls:

- Use a dedicated OS user.
- Use a dedicated workspace.
- Keep production secrets out of the workspace unless explicitly needed.
- Review MCP server commands before enabling them.
- Keep `allow_network_fetch` disabled unless a run requires web access.
- Store run artifacts for auditability.

## Incident response

If a run behaves unexpectedly:

1. Stop the process.
2. Inspect `runs/<run-id>/events.jsonl`.
3. Inspect changed files with your VCS or filesystem diff tool.
4. Restore any overwritten file from `runs/<run-id>/backups/`.
5. Re-run with `--dry-run` and a narrower prompt before allowing writes again.
