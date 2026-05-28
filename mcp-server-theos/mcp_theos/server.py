"""MCP Server Theos — FastAPI entrypoint.

Multi-tenant gateway in front of the Velneo V7 REST API. Each request
must carry ``X-Tenant-Id``; the tenant resolver fetches the Velneo
base URL + API key from ``public.tenants`` (encrypted with the same
AES-256-GCM key used for Odoo credentials).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from mcp_theos.config import settings
from mcp_theos.tenant_resolver import clear_cache, get_tenant_config
from mcp_theos.transports.mcp_transport import router as mcp_router
from mcp_theos.velneo_http import VelneoClient


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
async def health(request: Request) -> dict[str, Any]:
    """Liveness probe.

    Always reports the process is up + whether Supabase is configured.
    When the caller passes ``X-Tenant-Id`` we additionally ping the
    tenant's Velneo REST so operators can tell *that specific* tenant's
    backend is alive — useful for the support agent's monitor cron.

    The Velneo ping is a small ``GET PRODUCTOS?page[size]=1`` (lightest
    real table in every Theos install). We do not raise if the ping
    fails — the response status flips to ``degraded`` so the caller can
    treat it as an alert without losing the process-level signal.
    """
    body: dict[str, Any] = {
        "status": "ok",
        "service": "mcp-server-theos",
        "supabase_configured": "yes" if settings.supabase_url else "no",
    }
    tenant_hdr = (
        request.headers.get("x-tenant-id")
        or request.headers.get("X-Tenant-Id")
        or ""
    ).strip()
    if not tenant_hdr:
        return body

    try:
        cfg = await get_tenant_config(request)
    except Exception as exc:  # noqa: BLE001 — health endpoint should not raise
        body["status"] = "degraded"
        body["tenant_id"] = tenant_hdr
        body["tenant_error"] = f"{type(exc).__name__}: {exc}"
        return body

    body["tenant_id"] = cfg.tenant_id
    body["tenant_slug"] = cfg.slug
    body["velneo_base_url"] = cfg.base_url
    try:
        async with VelneoClient(cfg) as client:
            resp = await asyncio.wait_for(
                client.get("PRODUCTOS", params={"pagesize": 1}, fields=["ID"]),
                timeout=8.0,
            )
        body["velneo_ping"] = "ok"
        body["velneo_total_count"] = resp.total_count
    except asyncio.TimeoutError:
        body["status"] = "degraded"
        body["velneo_ping"] = "timeout"
    except Exception as exc:  # noqa: BLE001
        body["status"] = "degraded"
        body["velneo_ping"] = "error"
        body["velneo_error"] = f"{type(exc).__name__}: {exc}"
    return body
