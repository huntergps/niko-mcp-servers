"""Velneo product search."""

from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient

# Fields we project from PRODUCTOS — keep tight, the table has 60+ cols.
_PRODUCTOS_FIELDS = [
    "ID", "CODIGO", "NAME", "NAME_CORTO", "DESCRIPCION",
    "EXS", "STOCK_MAXIMO", "STOCK_MINIMO",
    "INV_FAMI", "INV_MARCAS", "MODELO",
    "IMP_FIS_IMPUESTOS_VTA",
    "VENDIBLE", "INCLUIR_CATALOGO", "OFF",
    "URL_IMAGEN",
]

_PRECIOS_FIELDS = [
    "ID", "INV_PRODUCTOS", "INV_TARIFAS", "INV_PRESENTACIONES",
    "PRECIO1", "IVA1", "PVP1", "PRECIO2", "PVP2",
    "DESCUENTO", "PORCENTAJE_DSCTO",
]


def _looks_like_code(q: str) -> bool:
    """Heuristic: a Velneo CODIGO is alphanumeric (sometimes with dashes
    and underscores), no spaces, max ~15 chars, AND must contain at
    least one digit. The digit requirement is what separates ``"01S1"``
    or ``"109950"`` from ordinary product names like ``"ARROZ"`` or
    ``"LECHE"`` — both meet the no-space / short-length test but neither
    is a SKU. Without this guard the tool would route every short query
    through ``?filter[CODIGO]=`` and silently return zero for any
    natural-language search.
    """
    if not q or " " in q or len(q) > 20:
        return False
    if not any(ch.isdigit() for ch in q):
        return False
    return all(ch.isalnum() or ch in "-_" for ch in q)


async def search_products(
    client: VelneoClient,
    *,
    query: str,
    limit: int = 20,
    include_prices: bool = True,
    tarifa_id: int | None = None,
) -> dict[str, Any]:
    """Look products up in PRODUCTOS.

    Velneo's REST API only supports EXACT filtering — there is no LIKE
    or contains operator. So this tool does two things:

    * If ``query`` looks like a SKU/code (alphanumeric, no spaces),
      we issue ``?filter[CODIGO]=<query>`` — that is fast and accurate.
    * Otherwise (natural-language text like "arroz Gustadina 1kg") we
      try ``?filter[NAME]=<query>`` (which Velneo only matches when the
      whole name equals the query verbatim) and we annotate the response
      with ``hint`` so the caller knows to fall back to RAG search
      against ``tenant_<slug>.product_embeddings``.

    PVP is NOT in PRODUCTOS; when ``include_prices=True`` we fan out a
    second call to INV_PRECIOS_PRODUCTO filtered by the returned IDs (or
    tarifa_id when given) and merge ``PVP1`` onto each product row.
    """
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "empty query", "products": []}

    if _looks_like_code(q):
        params: dict[str, Any] = {"CODIGO": q, "pagesize": limit}
        match_field = "CODIGO"
    else:
        params = {"NAME": q, "pagesize": limit}
        match_field = "NAME"

    resp = await client.get("PRODUCTOS", params=params, fields=_PRODUCTOS_FIELDS)
    rows = resp.rows[:limit]

    if include_prices and rows:
        ids = [r["ID"] for r in rows if r.get("ID") is not None]
        if ids:
            price_params: dict[str, Any] = {
                "INV_PRODUCTOS": ",".join(str(i) for i in ids),
                "pagesize": max(limit * 4, 100),
            }
            if tarifa_id is not None:
                price_params["INV_TARIFAS"] = tarifa_id
            try:
                price_resp = await client.get(
                    "INV_PRECIOS_PRODUCTO",
                    params=price_params,
                    fields=_PRECIOS_FIELDS,
                )
                price_by_product: dict[int, dict[str, Any]] = {}
                for p in price_resp.rows:
                    pid = p.get("INV_PRODUCTOS")
                    if pid is None:
                        continue
                    cur = price_by_product.get(pid)
                    if cur is None or (p.get("INV_TARIFAS") or 0) < (cur.get("INV_TARIFAS") or 99):
                        price_by_product[pid] = p
                for r in rows:
                    pinfo = price_by_product.get(r.get("ID"))
                    if pinfo:
                        r["PVP1"] = pinfo.get("PVP1")
                        r["PRECIO1"] = pinfo.get("PRECIO1")
                        r["IVA1"] = pinfo.get("IVA1")
                        r["INV_TARIFAS"] = pinfo.get("INV_TARIFAS")
            except Exception as exc:
                # Don't fail the whole search — products still returned without PVP.
                for r in rows:
                    r.setdefault("_price_lookup_error", type(exc).__name__)

    out: dict[str, Any] = {
        "success": True,
        "query": q,
        "match_field": match_field,
        "count": len(rows),
        "total_count": resp.total_count,
        "products": rows,
    }
    if match_field == "NAME" and not rows:
        # Velneo only does exact NAME match; the niko backend should
        # then route the original query through the pgvector RAG.
        out["hint"] = (
            "velneo NAME filter is exact-match; for partial / semantic "
            "search use the RAG index in tenant_<slug>.product_embeddings"
        )
    return out
