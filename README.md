# niko-mcp-servers

Standalone MCP (Model Context Protocol) servers used by [Niko](https://github.com/huntergps/niko) and available for third-party integration.

This repo is a monorepo of two independent MCP server images that are built and published to GHCR on every push to `main` and on version tags (`v*`).

## Images

| Image | Port | Purpose |
|---|---|---|
| `ghcr.io/huntergps/niko-mcp-server-odoo:latest` | 8085 | Multi-tenant, multi-version Odoo ERP MCP. Exposes `/mcp` and `/health`. Handles XML-RPC auth, per-tenant credentials, SRI (Ecuador electronic invoicing), and a curated tool surface over `sales`, `inventory`, `reports`, and generic Odoo ops. |
| `ghcr.io/huntergps/niko-mcp-memory:latest` | 8090 | Shared-memory MCP backed by Supabase (Postgres + pgvector). Contact profiles, per-contact memories (Mem0 pattern), and cross-agent conversation RAG. Embeddings via Ollama `bge-m3` (1024-d). |

Available tags for each image:

- `latest` — tip of `main`
- `sha-<short>` — per-commit immutable
- `v*` — when a git tag is pushed

## Pulling

```bash
docker pull ghcr.io/huntergps/niko-mcp-server-odoo:latest
docker pull ghcr.io/huntergps/niko-mcp-memory:latest
```

Both packages are public — no auth needed.

## Running

`mcp-server-odoo` requires Supabase credentials to resolve per-tenant Odoo config; see `mcp-server-odoo/mcp_odoo/config.py` for env vars.

`mcp-memory` expects `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `TENANT_SLUG`, `TENANT_ID`, and `OLLAMA_URL` pointing at a reachable Ollama with the `bge-m3` model pulled.

## Origin

Both servers were originally developed inside the private `worker/` monorepo. They are vendored here verbatim so that consumers (starting with Niko's `docker-compose.yml`) can pull pre-built images instead of building from source at deploy time.

## Local build

```bash
cd mcp-server-odoo && docker build -t niko-mcp-server-odoo:dev .
cd mcp-memory      && docker build -t niko-mcp-memory:dev      .
```

## License

MIT — see `LICENSE`.
