# Bundled MCP Catalog

LocalForge ships with 10 configured MCP profiles in `localforge.yaml`.

Enabled by default:

- `context7`: `npx -y @upstash/context7-mcp@latest`
- `playwright`: `npx -y @playwright/mcp@latest`
- `filesystem`: `npx -y @modelcontextprotocol/server-filesystem@latest ${WORKSPACE}`
- `shadcn`: `npx -y shadcn@latest mcp`
- `sequential_thinking`: `npx -y @modelcontextprotocol/server-sequential-thinking@latest`

Configured but disabled until credentials are supplied:

- `github`: official GitHub MCP Docker image, requires `GITHUB_PERSONAL_ACCESS_TOKEN`.
- `supabase`: `@supabase/mcp-server-supabase`, requires `SUPABASE_ACCESS_TOKEN` for local stdio use.
- `brave_search`: `@modelcontextprotocol/server-brave-search`, requires `BRAVE_API_KEY`.
- `neon`: `@neondatabase/mcp-server-neon`, requires `NEON_API_KEY`.
- `firecrawl`: `firecrawl-mcp`, requires `FIRECRAWL_API_KEY`.

## Enabling credentialed MCPs

Use the setup wizard for the lowest-friction path:

```bash
localforge setup --force
```

The wizard detects credentials already present in `.env` or the process environment, prompts only for missing credentials you choose to enable, and flips the matching MCP profile to `enabled: true`.

Manual path:

```bash
cp .env.example .env
# edit .env with the credentials you want
localforge mcp-list
```

Secrets from `.env` are loaded into process environment variables but are not printed by `mcp-list`. Existing `.env` files are backed up before the setup wizard modifies them.

## Security policy

Database, GitHub, search, and scraping MCPs are powerful and can affect external systems. LocalForge ships them configured, but they are opt-in so a fresh install remains local-first and credential-free.
