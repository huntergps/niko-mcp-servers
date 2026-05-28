"""MCP Server Theos — FastAPI entrypoint.

Multi-tenant gateway in front of the Velneo V7 REST API. Each request
must carry ``X-Tenant-Id``; the tenant resolver fetches the Velneo
base URL + API key from ``public.tenants`` (encrypted with the same
AES-256-GCM key used for Odoo credentials).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from mcp_theos.config import settings
from mcp_theos.tenant_resolver import clear_cache
from mcp_theos.transports.mcp_transport import router as mcp_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    clear_cache()


app = FastAPI(
    title="MCP Server Theos",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(mcp_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "mcp-server-theos",
        "supabase_configured": "yes" if settings.supabase_url else "no",
    }
