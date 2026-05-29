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
    """Promote Velneo errors[] to :class:`VelneoError`.

    Velneo's REST is inconsistent about the entry shape: some endpoints
    return ``errors: [{"status": "...", "message": "..."}]``, others
    return ``errors: ["File not found"]`` (just a string). The dict
    form is what the doc shows, but the string form shows up on missing
    record lookups (e.g. ``GET /ENT_ERP_CLI/<id>`` when the id is in
    ENT but not in the customer extension). We handle both — anything
    else gets a generic "unknown" message so callers always know they
    are looking at an error envelope.
    """
    errors = body.get("errors")
    if not errors or not isinstance(errors, list):
        return
    first = errors[0]
    if isinstance(first, dict):
        status = str(first.get("status") or "??")
        message = str(first.get("message") or "unknown velneo error")
    elif isinstance(first, str):
        status = "??"
        message = first
    else:
        status = "??"
        message = f"unknown velneo error shape: {type(first).__name__}"
    raise VelneoError(status, message, body)


def _upper_keys(row: dict[str, Any]) -> dict[str, Any]:
    """Velneo returns row keys in lowercase (``id``, ``codigo``, ``name``,
    etc.) even though the table schema documents them as UPPERCASE.

    We normalize to UPPERCASE here so every call site reads the field
    using the documented name (``ID``, ``CODIGO``, ``NAME``, …) — anything
    else means we'd be sprinkling case-handling through every tool, and
    a future Velneo version that flips to uppercase would silently make
    us miss every row.
    """
    if not isinstance(row, dict):
        return row
    return {(k.upper() if isinstance(k, str) else k): v for k, v in row.items()}


def extract_rows(body: dict[str, Any], *table_keys: str) -> list[dict[str, Any]]:
    """Pull the row list from a Velneo list response.

    Tries each ``table_keys`` candidate (in lowercase) and falls back to
    the first list-valued top-level key that isn't ``errors`` /
    ``count`` / ``total_count``. Returned rows have their keys upper-
    cased to match the documented table schema (see :func:`_upper_keys`).
    """
    rows: list[dict[str, Any]] = []
    for key in table_keys:
        v = body.get(key.lower())
        if isinstance(v, list):
            rows = v
            break
    if not rows:
        for k, v in body.items():
            if k in {"errors", "count", "total_count"}:
                continue
            if isinstance(v, list):
                rows = v
                break
    return [_upper_keys(r) for r in rows]


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

    async def get(  # noqa: C901 — single hot path
        self,
        table: str,
        *,
        record_id: int | str | None = None,
        params: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        use_cache: bool = True,
    ) -> VelneoResponse:
        """GET a list or a single record.

        Filter encoding: Velneo uses ``?filter[FIELD]=value`` (NOT the
        naked ``?FIELD=value`` shown in the doc — that form is silently
        ignored and returns the unfiltered first page). We auto-wrap
        every entry in ``params`` into the ``filter[…]`` form except
        the API's own reserved keys (``page``, ``pagesize``, ``fields``).
        Filters are EXACT MATCH only — Velneo's REST does not expose a
        LIKE / contains operator, so semantic search lives outside the
        MCP (pgvector RAG against ``tenant_<slug>.product_embeddings``).
        """
        path = table if record_id is None else f"{table}/{quote(str(record_id))}"
        # Velneo uses JSON:API for pagination (``page[number]`` /
        # ``page[size]``). The naked ``page=`` / ``pagesize=`` form
        # the doc shows is silently ignored — exactly the same trap
        # as ``?FIELD=value`` ignored in favor of ``?filter[FIELD]=``.
        # We translate the caller-friendly names (``page``,
        # ``pagesize``, ``limit``) into JSON:API here so call sites
        # don't have to know.
        q: dict[str, Any] = {}
        for k, v in (params or {}).items():
            if v is None:
                continue
            if k in ("page", "page_number"):
                q["page[number]"] = v
            elif k in ("pagesize", "page_size", "limit"):
                q["page[size]"] = v
            elif k == "fields":
                q["fields"] = v if isinstance(v, str) else ",".join(v)
            else:
                q[f"filter[{k}]"] = v
        if fields:
            q["fields"] = ",".join(fields)

        # D3 response cache — keyed off (tenant_id, GET, path, q).
        # Skipped when use_cache=False; writes still go through .post()
        # which bypasses this path entirely.
        from mcp_theos.cache import make_response_key, response_cache
        cache_key = make_response_key(
            getattr(self.cfg, "tenant_id", "") or "",
            "GET", path, q,
        ) if use_cache else ""
        cached_body = response_cache.get(cache_key) if cache_key else None
        if cached_body is not None:
            body = cached_body
        else:
            resp = await self._client.get(path, params=q)
            resp.raise_for_status()
            body = resp.json()
            if cache_key:
                response_cache.set(cache_key, body)
        _check_errors(body)

        if record_id is not None:
            sub = body.get(table.lower())
            if isinstance(sub, list) and sub:
                raw_row: Any = sub[0]
            elif isinstance(sub, dict):
                raw_row = sub
            else:
                raw_row = body
            rows = [_upper_keys(raw_row)] if isinstance(raw_row, dict) and raw_row else []
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
            # ``get()`` translates these into ``page[number]`` /
            # ``page[size]`` — caller-friendly names stay here.
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
        """POST a single record. Returns the created row (Velneo echoes
        it), with keys upper-cased (see :func:`_upper_keys`).
        """
        resp = await self._client.post(table, json=body)
        resp.raise_for_status()
        data = resp.json()
        _check_errors(data)
        rows = extract_rows(data, table)
        if rows:
            return rows[0]
        sub = data.get(table.lower())
        if isinstance(sub, dict):
            return _upper_keys(sub)
        return _upper_keys(data)

    async def delete(self, table: str, *, record_id: int | str) -> dict[str, Any]:
        """DELETE a single record by id.

        Velneo's REST returns 200 with ``{"return": "..."}`` on success
        and 200 with ``errors[]`` (or a 405 when the API key lacks the
        method) on failure. We treat the errors[] envelope the same
        way :func:`post` does, and raise :class:`VelneoError` on any
        bad message — same boundary contract as POST so callers can
        catch a single exception type.
        """
        resp = await self._client.delete(f"{table}/{record_id}")
        resp.raise_for_status()
        data = resp.json()
        _check_errors(data)
        return data if isinstance(data, dict) else {"return": str(data)}

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

    async def query(
        self,
        name: str,
        *,
        filters: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        page_size: int | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """Invoke a Velneo ``_query/<name>`` endpoint.

        Velneo exposes its Búsquedas via two parallel URLs:

        * ``/_process/<name>`` — caller passes ``param[VAR]=value``
        * ``/_query/<name>``   — caller passes ``filter[FIELD]=value``
          and standard pagination (``page[size]`` / ``page[number]``)

        Some Búsquedas accept both shapes (verified empirically on
        Mepriga: ``_query/vent_fact_vent_busq`` works with ``param[]``
        proceso-style). This helper sends whichever the caller passes;
        when both are given they're combined.
        """
        q: dict[str, Any] = {}
        if page_size is not None:
            q["page[size]"] = int(page_size)
        if page is not None:
            q["page[number]"] = int(page)
        for k, v in (filters or {}).items():
            if v is None:
                continue
            q[f"filter[{k}]"] = v
        for k, v in (params or {}).items():
            if v is None:
                continue
            q[f"param[{k}]"] = v
        resp = await self._client.get(f"_query/{quote(name)}", params=q)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Proceso invocation helper (used by admin tools)
# ---------------------------------------------------------------------------


# Velneo returns 200 OK with a plain string body when the API key
# is not authorized to execute the proceso. Detect that pattern so we
# can degrade gracefully into the REST fallback.
_PROCESO_PERMISSION_MARKERS = (
    "No es posible ejecutar el proceso",
    "no es posible ejecutar el proceso",
)


async def call_proceso_or_message(
    client: "VelneoClient",
    name: str,
    params: dict[str, Any] | None = None,
    *,
    row_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Invoke a proceso and return ``{"ok": bool, "rows": [...], ...}``.

    Wraps :meth:`VelneoClient.process` with:

    * Detection of the "permission denied" string Velneo returns when
      the API key cannot execute the proceso (the body is a plain
      string, not the proceso's normal JSON). When detected we return
      ``{"ok": False, "permission_denied": True, "message": "..."}``
      so callers can fall back to a REST-only path.
    * Row extraction: procesos that build a Cesta of records expose
      them under a top-level key shaped like the table (``vent_fact_vent``,
      ``inv_movimientos``, ...). Pass the candidate keys via
      ``row_keys`` and we will pick the first list-valued match (upper-
      casing each row's keys to match the documented schema, just like
      :func:`extract_rows` does for table list responses).
    """
    try:
        body = await client.process(name, params=params)
    except Exception as exc:  # noqa: BLE001 — surface transport errors as data
        return {
            "ok": False,
            "transport_error": f"{type(exc).__name__}: {exc}",
        }

    # Permission-denied shape: plain string body or {"errors": [...]}
    if isinstance(body, dict) and body.get("errors"):
        first = body["errors"][0] if body["errors"] else ""
        msg = (
            first.get("message") if isinstance(first, dict)
            else str(first)
        )
        if any(m in str(msg) for m in _PROCESO_PERMISSION_MARKERS):
            return {"ok": False, "permission_denied": True, "message": msg}
    if isinstance(body, str) and any(m in body for m in _PROCESO_PERMISSION_MARKERS):
        return {"ok": False, "permission_denied": True, "message": body}

    # Extract rows from the cesta key (lowercase = table name).
    rows: list[dict[str, Any]] = []
    if isinstance(body, dict):
        for k in row_keys:
            v = body.get(k.lower())
            if isinstance(v, list):
                rows = [_upper_keys(r) for r in v if isinstance(r, dict)]
                break
        if not rows:
            # Fallback: pick the first list-valued top-level key.
            for k, v in body.items():
                if k in {"errors", "count", "total_count"}:
                    continue
                if isinstance(v, list):
                    rows = [_upper_keys(r) for r in v if isinstance(r, dict)]
                    break

    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "total_count": (
            int(body.get("total_count") or len(rows))
            if isinstance(body, dict) else len(rows)
        ),
        "raw": body,
    }
