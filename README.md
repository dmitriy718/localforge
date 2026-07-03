# LocalForge

LocalForge is a local-first autonomous build harness for downloaded models. It gives a local model a real tool manifest, lets it inspect and modify a workspace, run commands, verify work, and produce auditable run artifacts.

Primary backend: Ollama. Optional backend: llama.cpp via `llama-cli`. MCP servers can be attached through stdio JSON-RPC when external tools are required.

## Capabilities

- Local model execution through Ollama or llama.cpp.
- Real local tools: shell, file read/write, JSON write, unified-diff patch application, file listing, text search, and optional HTTP fetch.
- MCP stdio client that registers configured remote tools as callable model tools.
- 10 bundled MCP profiles: Context7, Playwright, GitHub, Supabase, Brave Search, Filesystem, shadcn, Neon, Firecrawl, and Sequential Thinking.
- Per-run audit log in `runs/<run-id>/events.jsonl`.
- Full transcript in `runs/<run-id>/transcript.json`.
- CLI run history and run inspection commands for support/debugging.
- Backups before file replacement.
- Dry-run mode for tool execution review.
- CLI doctor command for backend verification.

## Quick start

```bash
source ./venv/bin/activate
localforge setup
localforge doctor
localforge mcp-list
localforge run "Build a production-ready FastAPI todo service in projects/todo-api with tests and docs."
```

On first run, LocalForge checks for `localforge.localconfig`. If it is missing, the setup wizard starts automatically in an interactive terminal. The marker stores local setup metadata only; secrets still belong in `.env`.

The wizard walks through:

- Ollama URL and model selection.
- Built-in network fetch policy.
- Optional credentialed MCPs: GitHub, Supabase, Brave Search, Neon, and Firecrawl.
- Safe `.env` updates with backups before existing secret files are changed.

Rerun setup:

```bash
localforge setup --force
```

For an interactive session:

```bash
localforge chat start
```

Inside chat:

- `/doctor`: check the configured model backend.
- `/mcp`: list configured MCPs and credential status.
- `/run <task>` or `/build <task>`: start the autonomous builder with tools.
- `/exit`: quit.

Plain chat messages are handled as lightweight conversation. They do not start the autonomous build loop or MCP/tool startup. This keeps simple messages like `hi` responsive.

Inspect run history:

```bash
localforge runs --limit 10
localforge show-run <run-id> --tail 20
```

This workspace is currently configured for:

```yaml
backend:
  provider: ollama
  model: hf.co/mradermacher/DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-GGUF:Q4_K_M
```

If your Ollama model name is different, edit `localforge.yaml`. Example:

```yaml
backend:
  provider: ollama
  model: qwen2.5-coder:14b
  context_window_tokens: 32768
```

## llama.cpp backend

```yaml
backend:
  provider: llama.cpp
  llama_cpp_binary: llama-cli
  llama_cpp_model_path: /absolute/path/to/model.gguf
  temperature: 0.2
  max_tokens: 4096
  context_window_tokens: 32768
```

Then run:

```bash
localforge run "Build ..."
```

## MCP servers

LocalForge ships with 10 MCP profiles in `localforge.yaml`. Keyless local/dev tools are enabled by default. Credentialed external services are configured but disabled until setup captures the needed environment variable or you set it manually. See `docs/MCP_CATALOG.md`.

Add or override stdio MCP servers under `mcp_servers`. Example config:

```bash
localforge run --config examples/mcp-filesystem.yaml "Inspect the repo and improve it."
```

MCP failures are not hidden. A server that cannot start, returns invalid JSON-RPC, or fails a tool call is reported in the run audit log and tool observation.

## Operational model

LocalForge is permissive by design. It shows the model's raw output live and tries to interpret clear action intent rather than forcing one narrow protocol. The most reliable tool-call format is:

```json
{
  "thought": "what I learned and why I need this tool",
  "tool_calls": [
    {"name": "list_files", "arguments": {"path": ".", "max_files": 100}}
  ],
  "final": null
}
```

LocalForge also understands direct tool objects, fenced JSON, slash commands, bare tool commands in code blocks, and safe tool mentions in planning prose.

The CLI prints live run events while the agent works: MCP startup, model waits, model heartbeat updates, raw model output, interpreted tool intent, tool results, final report, and artifact path.

## Verification

Run local tests:

```bash
./venv/bin/python -m unittest discover -s tests
```

Run static syntax verification:

```bash
./venv/bin/python -m compileall localforge tests
```

Run full local smoke checks:

```bash
./scripts/smoke.sh
```

CI/noninteractive checks should set:

```bash
LOCALFORGE_SKIP_SETUP=1
```

## Security and deployment notes

This harness is intentionally powerful. With `allow_shell: true`, the model can run local shell commands as your user. That matches the intended operator-grade behavior, but it must be deployed only where that trust model is acceptable.

Recommended production deployment:

- Run in a dedicated user account.
- Use a dedicated workspace directory.
- Keep secrets out of the workspace unless the model explicitly needs them.
- Enable network fetch only when required.
- Configure MCP servers explicitly and audit their commands.
- Persist `runs/` artifacts for traceability.

Docker deployment includes Node/npm/git so bundled `npx` MCP servers can run inside the container. It also includes `docker-cli` for MCP profiles that launch containers, such as the official GitHub MCP. When running in Docker against host Ollama, set `OLLAMA_HOST=http://host.docker.internal:11434`.

If GitHub MCP is enabled inside the LocalForge container, mount the host Docker socket:

```bash
docker run --rm -it \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -e LOCALFORGE_SKIP_SETUP=1 \
  -v "$PWD:/workspace" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  localforge:0.1.0 \
  mcp-smoke --config /workspace/localforge.yaml
```

Treat Docker socket access as privileged access to the host.
