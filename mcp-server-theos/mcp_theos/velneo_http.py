"""Async HTTP client for the Velneo V7 REST API.

Quirks this client handles (see Mepriga onboarding doc §5):

* Velneo returns **HTTP 200 even on errors** — the real status is inside
  ``body["errors"]``; we promote those to :class:`VelneoError` so callers
  can branch on success cleanly.
* List responses bury the row array under a key named after the lowercase
  table name (e.g. ``productos``, ``asi``, ``ent``). We expose
  :func:`extract_rows` to fish it out without the caller having to guess.
* Authentication is preferentially via the ``X-API-Key`` header (the
  query-string form leaks the key into ``velneo_api_access.log``).
* Field projection (``?fields=col1,col2``) and arbitrary
  ``?<field>=value`` filters are supported by the server.

Pagination: Velneo default page size is 1000; we keep our own cap via
``settings.velneo_default_page_size`` and walk pages until ``count <
page_size`` or ``velneo_max_pages`` is reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from mcp_theos.config import settings
from mcp_theos.tenant_resolver import TenantVelneoConfig


class VelneoError(Exception):
    """Raised when Velneo returns a non-empty ``errors[]`` array."""

    def __init__(self, status: str, message: str, raw: dict[str, Any]):
        self.status = status
        self.message = message
        self.raw = raw
        super().__init__(f"velneo {status}: {message}")


@dataclass
class VelneoResponse:
    body: dict[str, Any]
    rows: list[dict[str, Any]]
    count: int
    total_count: int


def _headers(cfg: TenantVelneoConfig) -> dict[str, str]:
    return {
        "X-API-Key": cfg.api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _check_errors(body: dict[str, Any]) -> None:
    errors = body.get("errors") or []
    if not errors:
        return
    first = errors[0] if isinstance(errors, list) else {}
    status = str(first.get("status") or "??")
    message = str(first.get("message") or "unknown velneo error")
    raise VelneoError(status, message, body)


def extract_rows(body: dict[str, Any], *table_keys: str) -> list[dict[str, Any]]:
    """Pull the row list from a Velneo list response.

    Tries each ``table_keys`` candidate (in lowercase) and falls back to
    the first list-valued top-level key that isn't ``errors`` /
    ``count`` / ``total_count``.
    """
    for key in table_keys:
        v = body.get(key.lower())
        if isinstance(v, list):
            return v
    for k, v in body.items():
        if k in {"errors", "count", "total_count"}:
            continue
        if isinstance(v, list):
            return v
    return []


class VelneoClient:
    def __init__(self, cfg: TenantVelneoConfig):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers=_headers(cfg),
            timeout=settings.velneo_http_timeout,
        )

    async def __aenter__(self) -> "VelneoClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        table: str,
        *,
        record_id: int | str | None = None,
        params: dict[str, Any] | None = None,
        fields: list[str] | None = None,
    ) -> VelneoResponse:
        """GET a list or a single record."""
        path = table if record_id is None else f"{table}/{quote(str(record_id))}"
        q: dict[str, Any] = dict(params or {})
        if fields:
            q["fields"] = ",".join(fields)
        resp = await self._client.get(path, params=q)
        resp.raise_for_status()
        body = resp.json()
        _check_errors(body)

        if record_id is not None:
            row = body.get(table.lower()) if isinstance(body.get(table.lower()), dict) else body
            rows = [row] if isinstance(row, dict) and row else []
            return VelneoResponse(body=body, rows=rows, count=len(rows), total_count=len(rows))

        rows = extract_rows(body, table)
        return VelneoResponse(
            body=body,
            rows=rows,
            count=int(body.get("count") or len(rows)),
            total_count=int(body.get("total_count") or len(rows)),
        )

    async def get_all(
        self,
        table: str,
        *,
        params: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET with pagination — concatenates pages up to caps."""
        ps = page_size or settings.velneo_default_page_size
        mp = max_pages or settings.velneo_max_pages
        out: list[dict[str, Any]] = []
        page = 1
        while page <= mp:
            q: dict[str, Any] = dict(params or {})
            q.update({"page": page, "pagesize": ps})
            resp = await self.get(table, params=q, fields=fields)
            if not resp.rows:
                break
            out.extend(resp.rows)
            if len(resp.rows) < ps:
                break
            page += 1
        return out

    async def post(self, table: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST a single record. Returns the created row (Velneo echoes it)."""
        resp = await self._client.post(table, json=body)
        resp.raise_for_status()
        data = resp.json()
        _check_errors(data)
        rows = extract_rows(data, table)
        if rows:
            return rows[0]
        sub = data.get(table.lower())
        if isinstance(sub, dict):
            return sub
        return data

    async def process(
        self,
        name: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke a Velneo process: ``GET /_process/<name>?param[...]=...``."""
        q: dict[str, Any] = {}
        for k, v in (params or {}).items():
            q[f"param[{k}]"] = v
        resp = await self._client.get(f"_process/{quote(name)}", params=q)
        resp.raise_for_status()
        return resp.json()
