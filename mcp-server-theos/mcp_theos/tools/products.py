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


async def search_products(
    client: VelneoClient,
    *,
    query: str,
    limit: int = 20,
    include_prices: bool = True,
    tarifa_id: int | None = None,
) -> dict[str, Any]:
    """Search PRODUCTOS by NAME (LIKE-ish) — Velneo also supports CODIGO and BUSQUEDA.

    PVP is NOT in PRODUCTOS; when ``include_prices=True`` we fan out a
    second call to INV_PRECIOS_PRODUCTO filtered by the returned IDs (or
    tarifa_id when given) and merge ``PVP1`` onto each product row.
    """
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "empty query", "products": []}

    params: dict[str, Any] = {"NAME": q, "pagesize": limit}
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

    return {
        "success": True,
        "query": q,
        "count": len(rows),
        "total_count": resp.total_count,
        "products": rows,
    }
