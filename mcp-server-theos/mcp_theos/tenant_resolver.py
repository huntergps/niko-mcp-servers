"""Resolve a tenant's Velneo connection from Supabase / Postgres.

The niko backend sends ``X-Tenant-Id`` as a header on every request to
this MCP server (already the convention used by mcp-server-odoo for
tenant scoping). We read ``public.tenants`` via PostgREST and return
a small ``TenantVelneoConfig`` dataclass with the decrypted API key.

Results are cached in process memory; the cache is invalidated when
the FastAPI lifespan tears down.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import HTTPException, Request

from mcp_theos.auth.encryption import decrypt_secret
from mcp_theos.config import settings


@dataclass
class TenantVelneoConfig:
    tenant_id: str
    slug: str
    name: str
    commercial_name: str
    base_url: str
    api_key: str
    schema: str
    extra: dict[str, Any] = field(default_factory=dict)


_cache: dict[str, TenantVelneoConfig] = {}
_cache_lock = asyncio.Lock()


def _postgrest_headers() -> dict[str, str]:
    key = settings.supabase_service_key or settings.supabase_jwt_secret
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept-Profile": "public",
        "Content-Profile": "public",
    }


async def _load_tenant_row(tenant_id: str) -> dict[str, Any]:
    if not settings.supabase_url:
        raise RuntimeError("SUPABASE_URL not configured")
    fields = (
        "id,slug,name,commercial_name,erp_type,"
        "erp_api_url,erp_api_key_encrypted,erp_api_schema,erp_api_extra"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{settings.supabase_url}/rest/v1/tenants",
            params={"id": f"eq.{tenant_id}", "select": fields},
            headers=_postgrest_headers(),
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"supabase tenant lookup failed: {resp.status_code} {resp.text[:200]}",
        )
    rows = resp.json()
    if not rows:
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id} not found")
    return rows[0]


async def get_tenant_config(request: Request) -> TenantVelneoConfig:
    """FastAPI dependency: resolve tenant from ``X-Tenant-Id`` header."""
    tenant_id = (
        request.headers.get("x-tenant-id")
        or request.headers.get("X-Tenant-Id")
        or ""
    ).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="missing X-Tenant-Id header")

    cached = _cache.get(tenant_id)
    if cached is not None:
        return cached

    async with _cache_lock:
        cached = _cache.get(tenant_id)
        if cached is not None:
            return cached

        row = await _load_tenant_row(tenant_id)

        erp_type = (row.get("erp_type") or "").lower()
        if erp_type != "velneo":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"tenant {tenant_id} erp_type={erp_type!r}; "
                    "mcp-server-theos only handles erp_type=velneo"
                ),
            )

        base_url = (row.get("erp_api_url") or "").rstrip("/") + "/"
        if not base_url.strip("/"):
            raise HTTPException(
                status_code=500,
                detail=f"tenant {tenant_id} has no erp_api_url",
            )

        try:
            api_key = decrypt_secret(row.get("erp_api_key_encrypted") or "")
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"erp_api_key decrypt failed: {type(exc).__name__}",
            ) from exc
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail=f"tenant {tenant_id} has empty Velneo API key",
            )

        extra = row.get("erp_api_extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                extra = {}

        cfg = TenantVelneoConfig(
            tenant_id=row["id"],
            slug=row.get("slug") or "",
            name=row.get("name") or "",
            commercial_name=row.get("commercial_name") or row.get("name") or "",
            base_url=base_url,
            api_key=api_key,
            schema=row.get("erp_api_schema") or "",
            extra=extra,
        )
        _cache[tenant_id] = cfg
        return cfg


def clear_cache() -> None:
    _cache.clear()
