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
    """Pull the full product card by code or barcode.

    Two-step lookup:

    1. ``visor_datos(codbar=<code>)`` is the fast path — works whenever
       ``<code>`` is registered as a barcode in ``INV_PRESENT_PRODUCTO``.
    2. If that returns ``NO_ENCONTRADO`` we fall back to a direct
       ``PRODUCTOS?filter[CODIGO]=<code>`` lookup. That catches the
       case where the LLM has the internal CODIGO (not a barcode) —
       typically a discontinued or non-sale product without any
       presentation registered. We surface the row with a clear note
       so the bot can tell the customer instead of inventing.
    """
    q = (code or "").strip()
    if not q:
        return {"success": False, "error": "empty code"}
    raw = await _visor_datos(client, q, include_image=include_image)
    if raw is not None:
        return _shape_product(raw)

    # Fallback — code may be a CODIGO that has no barcode entry.
    try:
        resp = await client.get(
            "PRODUCTOS",
            params={"CODIGO": q, "pagesize": 1},
            fields=["ID", "CODIGO", "NAME", "INV_FAMI", "VENDIBLE", "OFF"],
        )
    except Exception:
        resp = None

    rows = resp.rows if resp is not None else []
    if not rows:
        return {
            "success": False,
            "error_code": "not_found",
            "error": f"product {q!r} not found in catalog",
        }

    row = rows[0]
    is_vendible = row.get("VENDIBLE") is not False  # None or True both count as sellable
    is_off = bool(row.get("OFF"))
    if is_off:
        status_note = "El producto está marcado como eliminado en el sistema."
    elif not is_vendible:
        status_note = (
            "Este producto está registrado pero NO está habilitado para "
            "venta directa (vendible=false). No tiene precio ni "
            "presentaciones disponibles en este momento."
        )
    else:
        status_note = (
            "El producto existe pero no tiene presentaciones registradas "
            "en INV_PRESENT_PRODUCTO — no se puede cotizar hasta que se "
            "agregue al menos una presentación con código de barras."
        )

    return {
        "success": True,
        "id": row.get("ID"),
        "code": row.get("CODIGO"),
        "name": row.get("NAME"),
        "family": None,
        "pvp_main": None,
        "presentations": [],
        "not_sellable": True,
        "_note": status_note,
    }


async def get_product_image(
    client: VelneoClient,
    *,
    code: str,
) -> dict[str, Any]:
    """Fetch the product image as base64 (PNG), cached on disk.

    Mirrors the Theos visor app's two-step strategy (see
    :mod:`mcp_theos.image_cache`):

    1. Light probe via ``visor_datos(dar_imagen=0)`` to recover the
       header + ``fecha_mod_imagen`` (the image version tag).
    2. If our on-disk cache has the same version → serve from disk
       and skip the heavy ~400KB base64 transfer entirely.
    3. Otherwise, second call with ``dar_imagen=1`` to refetch and
       refresh the cache.

    The cache is shared across all clients of a tenant and persists
    across container restarts (volume-mounted in docker-compose).
    Concurrent requests for the same product are deduplicated: only
    the first one hits Velneo, the others await the same Future.
    """
    import base64

    from mcp_theos.image_cache import get_cache

    q = (code or "").strip()
    if not q:
        return {"success": False, "error": "empty code"}

    tenant_id = client.cfg.tenant_id
    cache = get_cache()

    # Step 1 — light probe to learn the server's current version tag.
    light = await _visor_datos(client, q, include_image=False)
    if light is None:
        return {
            "success": False,
            "error_code": "not_found",
            "error": f"product {q!r} not found (visor_datos NO_ENCONTRADO)",
        }
    server_version = (light.get("fecha_mod_imagen") or "").strip()
    base_card = {
        "id": light.get("id"),
        "code": light.get("codigo") or q,
        "name": light.get("name"),
        "family": light.get("familia"),
        "image_mtime": server_version or None,
        "image_filename": f"{light.get('codigo') or q}.png",
    }

    # Step 2 — cache lookup. If we have a copy AND the version matches
    # (or the server has no version, in which case we fall back to TTL),
    # serve from disk.
    cached_bytes, cached_version = cache.get_cached(tenant_id, q)
    if cached_bytes:
        fresh = (
            (server_version and cached_version == server_version)
            or not server_version
        )
        if fresh:
            return {
                **base_card,
                "success": True,
                "from_cache": True,
                "image_base64": base64.b64encode(cached_bytes).decode("ascii"),
                "image_bytes": len(cached_bytes),
            }

    # Step 3 — in-flight dedup. If another request is already fetching
    # this image, await its result instead of issuing a second call.
    fut, is_leader = await cache.begin_fetch(tenant_id, q)
    if not is_leader:
        try:
            shared = await fut
        except Exception:  # noqa: BLE001
            shared = None
        if shared:
            return {
                **base_card,
                "success": True,
                "from_cache": True,
                "shared_with_inflight": True,
                "image_base64": base64.b64encode(shared).decode("ascii"),
                "image_bytes": len(shared),
            }
        return {
            **base_card,
            "success": False,
            "error_code": "fetch_failed_shared",
            "error": "Otra solicitud paralela falló al traer la imagen.",
        }

    # We're the leader — do the heavy fetch and resolve the future.
    img_bytes: bytes | None = None
    try:
        full = await _visor_datos(client, q, include_image=True)
        if full is None:
            return {
                **base_card,
                "success": False,
                "error_code": "not_found_full",
                "error": "El producto desapareció entre el light probe y el fetch full.",
            }
        img64 = (full.get("imagen64") or "").strip()
        img_url = (full.get("imagen") or "").strip()
        if not img64 and not img_url:
            return {
                **base_card,
                "success": False,
                "error_code": "no_image",
                "error": "El producto existe pero no tiene imagen registrada en Theos.",
            }
        if img64:
            try:
                img_bytes = base64.b64decode(img64)
            except Exception:  # noqa: BLE001
                img_bytes = None
            if img_bytes:
                cache.save(tenant_id, q, img_bytes, server_version or None)
            return {
                **base_card,
                "success": True,
                "from_cache": False,
                "image_base64": img64,
                "image_url": img_url or None,
                "image_bytes": len(img_bytes) if img_bytes else 0,
            }
        # URL-only branch — Theos rarely uses this for Mepriga but
        # support it for completeness.
        return {
            **base_card,
            "success": True,
            "from_cache": False,
            "image_url": img_url,
        }
    finally:
        await cache.finish_fetch(tenant_id, q, fut, img_bytes)


async def check_stock(
    client: VelneoClient,
    *,
    product_ids: list[int] | None = None,
    codes: list[str] | None = None,
    include_warehouses: bool = False,
) -> dict[str, Any]:
    """Stock disponibility for one or many products.

    Reads PRODUCTOS.EXS (total existencia) for each id. When
    ``include_warehouses=True`` we also project EXS_BOD1..EXS_BOD12
    and INV_BODEGA1..INV_BODEGA12 so the LLM can answer "¿está
    en bodega Santa Cruz?". Each entry returns:
    ``{id, code, name, exs, vendible, off, stock_min, stock_max}``
    and optionally a ``per_warehouse`` array.

    Either ``product_ids`` (Velneo ids) or ``codes`` (CODIGO strings)
    may be passed; codes are resolved by a single
    ``filter[CODIGO]=`` lookup so a list of 10 codes costs 1 round-
    trip, not 10.
    """
    pid_list: list[int] = []
    if product_ids:
        for pid in product_ids:
            try:
                pid_list.append(int(pid))
            except (TypeError, ValueError):
                continue

    fields_base = [
        "ID", "CODIGO", "NAME", "NAME_CORTO",
        "EXS", "STOCK_MINIMO", "STOCK_MAXIMO",
        "VENDIBLE", "OFF",
    ]
    if include_warehouses:
        fields_base += [
            "INV_BODEGA1", "INV_BODEGA2", "INV_BODEGA3", "INV_BODEGA4",
            "INV_BODEGA5", "INV_BODEGA6", "INV_BODEGA7", "INV_BODEGA8",
            "INV_BODEGA9", "INV_BODEGA10", "INV_BODEGA11", "INV_BODEGA12",
            "EXS_BOD1", "EXS_BOD2", "EXS_BOD3", "EXS_BOD4",
            "EXS_BOD5", "EXS_BOD6", "EXS_BOD7", "EXS_BOD8",
            "EXS_BOD9", "EXS_BOD10", "EXS_BOD11", "EXS_BOD12",
        ]

    # Resolve codes → ids via a single batched filter call.
    if codes:
        code_list = [str(c).strip() for c in codes if c]
        if code_list:
            try:
                # Velneo equality filter doesn't accept comma-list values
                # cleanly; do one call per code (small N expected).
                for code in code_list:
                    r = await client.get(
                        "PRODUCTOS",
                        params={"CODIGO": code, "pagesize": 1},
                        fields=["ID"],
                    )
                    if r.rows:
                        pid_list.append(int(r.rows[0]["ID"]))
            except Exception as exc:
                return {"success": False, "error": f"code resolve failed: {exc}"}

    if not pid_list:
        return {"success": False, "error": "product_ids o codes son requeridos"}

    items: list[dict[str, Any]] = []
    for pid in pid_list:
        try:
            r = await client.get("PRODUCTOS", record_id=pid, fields=fields_base)
        except Exception as exc:
            items.append({"id": pid, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not r.rows:
            items.append({"id": pid, "error": "not_found"})
            continue
        row = r.rows[0]
        try:
            exs = float(row.get("EXS") or 0)
        except (TypeError, ValueError):
            exs = 0.0
        item: dict[str, Any] = {
            "id": row.get("ID"),
            "code": (row.get("CODIGO") or "").strip(),
            "name": (row.get("NAME") or row.get("NAME_CORTO") or "").strip(),
            "exs": exs,
            "available": exs > 0 and not row.get("OFF") and row.get("VENDIBLE") is not False,
            "stock_min": float(row.get("STOCK_MINIMO") or 0),
            "stock_max": float(row.get("STOCK_MAXIMO") or 0),
            "vendible": row.get("VENDIBLE") is not False,
            "off": bool(row.get("OFF")),
        }
        if include_warehouses:
            per: list[dict[str, Any]] = []
            for i in range(1, 13):
                bod_id = row.get(f"INV_BODEGA{i}")
                qty_raw = row.get(f"EXS_BOD{i}")
                try:
                    qty = float(qty_raw) if qty_raw not in (None, "") else 0.0
                except (TypeError, ValueError):
                    qty = 0.0
                if not bod_id and qty == 0:
                    continue
                per.append({"warehouse_id": bod_id, "qty": qty})
            item["per_warehouse"] = per
        items.append(item)

    return {
        "success": True,
        "count": len(items),
        "items": items,
    }


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
            fields=["ID", "CODIGO", "NAME", "OFF", "VENDIBLE"],
        )
        hits = resp.rows
    except Exception as exc:
        return {
            "success": False, "query": q, "match_field": "words",
            "error": f"{type(exc).__name__}: {exc}",
            "products": [],
        }

    # In-memory post-filter:
    # * drop OFF rows (logically deleted)
    # * drop VENDIBLE=false rows (discontinued / not for direct sale) —
    #   surfacing them produced "Precio no disponible" noise in the
    #   chat and broke the follow-up ``get_product_details`` call
    #   because those rows typically have no INV_PRESENT_PRODUCTO entry
    #   for ``visor_datos`` to match against.
    # * require every other token of the query to appear in NAME
    #   (case-insensitive substring).
    filtered: list[dict[str, Any]] = []
    for r in hits:
        if r.get("OFF"):
            continue
        if r.get("VENDIBLE") is False:
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
    # don't drag the LLM with N round-trips). Products that fail
    # enrichment — usually because they have no INV_PRESENT_PRODUCTO
    # entry (no barcode registered) — are SKIPPED entirely instead of
    # being surfaced with "Precio no disponible", which confused both
    # the bot and the customer (chat 2026-05-28 with @mepriga_ventas_bot
    # showed "Precio no disponible en este momento" for ID 143940
    # FAVORITA ACEITE 360ML, a vendible=false row).
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
            # No barcode in the ERP for this product — skip; it cannot
            # be cotizado anyway.
            continue
        shaped = _shape_product(raw)
        if not shaped.get("presentations"):
            # Defence-in-depth: a malformed visor_datos response could
            # come back with ok=true but no presentations. Skip same.
            continue
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
