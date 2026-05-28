"""Product lookup for tenants on Theos / Velneo.

Three paths:

* :func:`get_product_details` — direct ``_process/visor_datos`` call.
  One round-trip returns everything a quotation card needs: the
  product header, its family, every presentation with its own price /
  factor / barcode / discount, the IVA description, and the image
  (base64 PNG) when requested.

* :func:`search_products` — dual path. If the query *looks like a
  code* (alphanumeric with at least one digit) we go straight to
  ``visor_datos``. Otherwise we embed the query and fan out a top-K
  pgvector search against ``tenant_<slug>.product_embeddings``; each
  hit is then enriched with ``visor_datos`` so the LLM sees the same
  rich payload regardless of which path matched.

The Theos ``visor_datos`` process keeps keys in lowercase (``pvp``,
``codbar``, ``factor``) — different from the table-generic REST
endpoints. We pass that shape through unchanged; the LLM sees one
consistent dictionary per product.
"""

from __future__ import annotations

from typing import Any

from mcp_theos.config import settings
from mcp_theos.velneo_http import VelneoClient


def _looks_like_code(q: str) -> bool:
    """Alphanumeric, no spaces, ≤20 chars, contains at least one digit."""
    if not q or " " in q or len(q) > 20:
        return False
    if not any(ch.isdigit() for ch in q):
        return False
    return all(ch.isalnum() or ch in "-_" for ch in q)


async def _visor_datos(
    client: VelneoClient,
    codbar: str,
    *,
    include_image: bool = False,
) -> dict[str, Any] | None:
    """Single ``visor_datos`` call. Returns ``None`` if not found."""
    body = await client.process(
        "visor_datos",
        params={
            "codbar": codbar,
            "dar_imagen": "1" if include_image else "0",
        },
    )
    if not isinstance(body, dict) or not body.get("ok"):
        return None
    return body


def _shape_product(raw: dict[str, Any]) -> dict[str, Any]:
    """Project the ``visor_datos`` payload to a stable shape for the LLM.

    Filter the noise (per_efectivo / para_cheque / para_tc are POS
    discount toggles; the bot does not care) and surface the fields
    that matter for quoting and answering "do you carry this?".
    """
    presentations = []
    main_price = None
    main_codbar = None
    for p in raw.get("precios") or []:
        if not isinstance(p, dict):
            continue
        try:
            pvp = float(p.get("pvp") or 0)
        except (TypeError, ValueError):
            pvp = 0.0
        try:
            factor = float(p.get("factor") or 0)
        except (TypeError, ValueError):
            factor = 0.0
        try:
            iva_pct = float(p.get("iva_porcentaje") or 0)
        except (TypeError, ValueError):
            iva_pct = 0.0
        entry = {
            "id": str(p.get("id") or ""),
            "name": p.get("name") or "",
            "factor": factor,
            "pvp": round(pvp, 4),
            "codbar": (p.get("codbar") or "").strip() or None,
            "descuento_pct": float(p.get("descuento") or 0),
            "descuento_monto": float(p.get("descuento_monto") or 0),
            "iva": p.get("iva") or "",
            "iva_pct": iva_pct,
            # Velneo emits the typo ``costo_emapaque`` (sic — see the
            # visor_datos source); accept the clean spelling too in
            # case the ERP team ever fixes it.
            "costo_empaque": float(
                p.get("costo_emapaque") or p.get("costo_empaque") or 0
            ),
            "utilidad_pct": float(p.get("utilidad") or 0),
        }
        presentations.append(entry)
        # The "main" unit is the one with factor 1 (e.g. LIBRA X 1).
        if main_price is None and abs(factor - 1.0) < 1e-6:
            main_price = entry["pvp"]
            main_codbar = entry["codbar"]

    out = {
        "success": True,
        "id": raw.get("id"),
        "code": raw.get("codigo"),
        "name": raw.get("name"),
        "family": raw.get("familia"),
        "barcode_main": main_codbar,
        "pvp_main": main_price,
        "presentations": presentations,
    }
    img = raw.get("imagen64")
    img_url = raw.get("imagen")
    if img:
        out["image_base64"] = img
    if img_url:
        out["image_url"] = img_url
    if raw.get("fecha_mod_imagen"):
        out["image_mtime"] = raw["fecha_mod_imagen"]
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def get_product_details(
    client: VelneoClient,
    *,
    code: str,
    include_image: bool = False,
) -> dict[str, Any]:
    """Pull the full product card by code or barcode."""
    q = (code or "").strip()
    if not q:
        return {"success": False, "error": "empty code"}
    raw = await _visor_datos(client, q, include_image=include_image)
    if raw is None:
        return {
            "success": False,
            "error": f"product {q!r} not found",
        }
    return _shape_product(raw)


async def search_products(
    client: VelneoClient,
    *,
    query: str,
    limit: int = 10,
    include_image: bool = False,
    include_prices: bool = True,  # kept for backward-compat
    tarifa_id: int | None = None,  # accepted but ignored — visor_datos
                                     # returns the configured tariff
) -> dict[str, Any]:
    """Search products by code (direct) or by natural-language (RAG)."""
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "empty query", "products": []}

    # Path A — exact code lookup
    if _looks_like_code(q):
        raw = await _visor_datos(client, q, include_image=include_image)
        if raw is None:
            return {
                "success": True,
                "query": q,
                "match_field": "code",
                "count": 0,
                "products": [],
            }
        return {
            "success": True,
            "query": q,
            "match_field": "code",
            "count": 1,
            "products": [_shape_product(raw)],
        }

    # Path B — RAG: embed → pgvector top-K → enrich each with visor_datos
    from mcp_theos.rag import product_codes_by_similarity

    try:
        hits = await product_codes_by_similarity(
            client.cfg.slug, q, limit=min(limit, settings.rag_max_enrich),
        )
    except Exception as exc:
        return {
            "success": False,
            "query": q,
            "match_field": "rag",
            "error": f"RAG search failed: {type(exc).__name__}: {exc}",
            "products": [],
        }

    products: list[dict[str, Any]] = []
    for h in hits:
        code = (h.get("code") or "").strip()
        sim = h.get("similarity")
        if not code:
            continue
        try:
            raw = await _visor_datos(client, code, include_image=include_image)
        except Exception:
            raw = None
        if raw is None:
            # Fallback: surface just the RAG metadata so the LLM still
            # has a name to mention.
            products.append({
                "success": True,
                "id": h.get("odoo_id"),
                "code": code,
                "name": h.get("name"),
                "family": None,
                "presentations": [],
                "similarity": float(sim) if sim is not None else None,
                "_note": "enrichment from Theos failed; partial data",
            })
            continue
        shaped = _shape_product(raw)
        shaped["similarity"] = float(sim) if sim is not None else None
        products.append(shaped)

    return {
        "success": True,
        "query": q,
        "match_field": "rag",
        "count": len(products),
        "products": products,
    }
