"""Product lookup for tenants on Theos (Velneo platform).

Three paths, in order of preference:

* :func:`get_product_details` — direct ``_process/visor_datos`` call.
  One round-trip returns everything a quotation card needs.

* :func:`search_products` — dual path:

  1. If the query *looks like a code* (alphanumeric with ≥1 digit, no
     spaces) → straight to ``visor_datos``.
  2. Otherwise → native Velneo ``filter[words]=...`` (the WORDS
     index that the ERP itself uses for product lookups in its UI).
     For multi-word queries we pick the most-distinctive token and
     post-filter in memory by the rest. RAG pgvector is kept as a
     fallback only when WORDS returns zero rows.

  Each hit is then enriched with ``visor_datos`` (capped by
  ``RAG_MAX_ENRICH``) so the LLM gets the same rich shape regardless
  of which path matched.

The Theos ``visor_datos`` process keeps keys in lowercase (``pvp``,
``codbar``, ``factor``) — different from the table-generic REST
endpoints. We pass that shape through unchanged; the LLM sees one
consistent dictionary per product.
"""

from __future__ import annotations

from typing import Any

from mcp_theos.config import settings
from mcp_theos.velneo_http import VelneoClient


# Stopwords (Spanish + measurement units) we drop before picking the
# primary token for a multi-word query. The list intentionally stays
# tiny — Velneo's WORDS index already tolerates these as full tokens,
# but they're so common they kill the relevance signal.
_STOPWORDS = frozenset({
    "de", "del", "la", "el", "los", "las", "y", "con", "en", "para",
    "un", "una", "unos", "unas", "al", "lo", "por", "o", "u",
    "kg", "g", "gr", "ml", "lt", "l", "cm", "mm", "m",
    "x", "pack", "und", "u", "unidad", "unidades",
})


def _tokenize(q: str) -> list[str]:
    """Split a free-text query into search-friendly tokens.

    * Uppercases everything (Velneo's WORDS index is case-insensitive
      either way but consistency helps when we post-filter in Python).
    * A token is dropped if, after stripping leading digits/punctuation,
      its lowercase form is in :data:`_STOPWORDS` — that catches both
      "kg" and "1kg" / "500g".
    * Tokens shorter than 3 chars are also dropped.
    * If everything gets filtered we fall back to the raw tokens so the
      caller still has something to search with.
    """
    raw = [t for t in q.upper().split() if t]
    keep: list[str] = []
    for t in raw:
        alpha = t.lstrip("0123456789.,-").lower()
        if not alpha or alpha in _STOPWORDS:
            continue
        if len(t) < 3:
            continue
        keep.append(t)
    return keep or raw


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

    # Path B — Velneo native WORDS search (same index the ERP UI uses
    # for product lookup). Vastly cheaper and more accurate than the
    # pgvector RAG for catalog text matches; that path is kept only
    # as a fallback when WORDS returns zero rows.
    tokens = _tokenize(q)
    if not tokens:
        return {
            "success": True, "query": q, "match_field": "words",
            "count": 0, "products": [],
        }
    # Use the longest token as the primary key — usually the most
    # distinctive one. The rest are applied as post-filters in memory.
    primary = max(tokens, key=len)
    other_tokens = [t for t in tokens if t != primary]
    max_pull = min(max(limit * 4, 20), 200)  # pull extra so the post-filter has headroom

    try:
        resp = await client.get(
            "PRODUCTOS",
            params={"words": primary, "pagesize": max_pull},
            fields=["ID", "CODIGO", "NAME", "OFF"],
        )
        hits = resp.rows
    except Exception as exc:
        return {
            "success": False, "query": q, "match_field": "words",
            "error": f"{type(exc).__name__}: {exc}",
            "products": [],
        }

    # In-memory post-filter: drop OFF and require every other token to
    # appear in NAME (case-insensitive substring).
    filtered: list[dict[str, Any]] = []
    for r in hits:
        if r.get("OFF"):
            continue
        name = (r.get("NAME") or "").upper()
        if other_tokens and not all(t in name for t in other_tokens):
            continue
        filtered.append(r)
        if len(filtered) >= limit:
            break

    # Path B.2 fallback — pgvector RAG when WORDS came back empty.
    used_fallback = False
    if not filtered:
        try:
            from mcp_theos.rag import product_codes_by_similarity
            schema = f"tenant_{client.cfg.slug}"
            sim_hits = await product_codes_by_similarity(
                schema, q, limit=min(limit, settings.rag_max_enrich),
            )
            for h in sim_hits:
                filtered.append({
                    "ID": h.get("odoo_id"),
                    "CODIGO": (h.get("code") or "").strip(),
                    "NAME": h.get("name") or "",
                    "_similarity": h.get("similarity"),
                })
            used_fallback = True
        except Exception:
            pass  # silent — RAG is best-effort fallback

    # Enrich each hit with visor_datos (cap = rag_max_enrich so we
    # don't drag the LLM with N round-trips).
    products: list[dict[str, Any]] = []
    enrich_cap = min(len(filtered), settings.rag_max_enrich)
    for r in filtered[:enrich_cap]:
        code = (r.get("CODIGO") or "").strip()
        if not code:
            continue
        try:
            raw = await _visor_datos(client, code, include_image=include_image)
        except Exception:
            raw = None
        if raw is None:
            # Cannot reach the product card — surface what we have so
            # the LLM still has the name to mention.
            products.append({
                "success": True,
                "id": r.get("ID"),
                "code": code,
                "name": r.get("NAME"),
                "family": None,
                "presentations": [],
                "_note": "enrichment from Theos failed; partial data",
            })
            continue
        shaped = _shape_product(raw)
        if "_similarity" in r:
            shaped["similarity"] = r["_similarity"]
        products.append(shaped)

    return {
        "success": True,
        "query": q,
        "match_field": "rag" if used_fallback else "words",
        "count": len(products),
        "total_matched": len(filtered),
        "products": products,
    }
