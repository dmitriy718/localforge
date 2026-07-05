# LocalForge Suggested Additions - 07/03/2026 #01

These suggestions are based on the current repository state plus current public guidance from MCP, OWASP GenAI, OpenTelemetry GenAI, and SLSA sources.

## 1. Pin MCP Server Versions

Replace `@latest` MCP package references with explicit versions and document a controlled upgrade command. This improves reproducibility and reduces supply-chain drift.

Source basis: SLSA emphasizes build integrity and repeatable provenance controls: https://slsa.dev/

## 2. Add Tool Capability Metadata

Add first-class tool metadata such as `read`, `write`, `network`, `external_system`, `credentialed`, and `destructive`. Use it instead of name heuristics for mutation/verification classification.

Source basis: OWASP LLM risks include excessive agency, insecure output handling, and supply-chain/tooling risk: https://owasp.org/www-project-top-10-for-large-language-model-applications/

## 3. Add OpenTelemetry Export

Keep JSONL audit logs, but also emit optional OpenTelemetry spans for model calls, tool calls, MCP calls, iterations, and run outcomes.

Source basis: OpenTelemetry GenAI conventions define attributes for model requests, responses, tool calls, usage, and workflow metadata: https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/

## 4. Add Policy Files For Tool Execution

Introduce a `localforge.policy.yaml` layer with default-deny profiles for shell, network fetch, external paths, Docker socket use, credentialed MCPs, and destructive MCP tools.

Source basis: OWASP GenAI guidance treats agentic tools and excessive agency as core risk areas: https://genai.owasp.org/llm-top-10/

## 5. Profile-Gate Docker Socket Access

Move Docker socket mounting in Compose behind an explicit profile or separate compose override file, so default container runs do not automatically request host Docker control.

Source basis: Docker socket access is equivalent to high host privilege in practice; this repo already documents it as powerful access.

## 6. Add Signed Release Provenance

Add CI release jobs that generate build provenance and attach artifacts. Target SLSA Build L1 first, then iterate toward stronger provenance.

Source basis: SLSA Build L1 requires provenance showing how the package was built: https://slsa.dev/spec/v1.0/levels

## 7. Add MCP Server Attestation Snapshot

On `mcp-smoke`, store a tool-manifest snapshot with server command, version if available, tool names, schemas, and hash of the manifest. Compare against previous snapshots for unexpected changes.

Source basis: MCP standardizes tool discovery over JSON-RPC; current stdio transport uses newline-delimited JSON-RPC messages: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports

## 8. Add Replayable Run Verifier

Create `localforge verify-run <run-id>` that re-reads audit logs, checks mutation/verification order, confirms referenced files still exist, and reports whether the final claims are supported.

Source basis: The harness already stores JSONL events and transcripts; this turns audit data into an explicit verification product.

## 9. Add Network Egress Guardrails

For `fetch_url`, add stricter egress policy: allowed host allowlists, blocked CIDRs, maximum response bytes before buffering, content-type limits, and a stronger resolver path to reduce DNS rebinding risk.

Source basis: OWASP GenAI risk categories include data exposure and tool misuse; private-network fetch controls are part of reducing agent SSRF blast radius: https://owasp.org/www-project-top-10-for-large-language-model-applications/

## 10. Add Self-Healing Diagnostics

When doctor or smoke fails, generate a structured remediation bundle with detected cause, command evidence, safe next commands, and whether the failure is environment, config, dependency, model, MCP, Docker, or code.

Source basis: OpenTelemetry and audit-driven operations favor structured, comparable failure metadata rather than free-form output only: https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/
