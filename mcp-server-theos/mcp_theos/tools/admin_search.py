"""Generic Velneo lookup with FK / child-collection expansion.

This is the catch-all tool the internal support agent reaches for when
the dedicated tools (inspect_partner, list_pending_invoices, etc.) do
not cover the question. It enforces a *whitelist* of entry-point tables
— the agent cannot use this to read arbitrary Velneo tables — and lets
the agent ask for related rows (forward FK or child collection) in one
go instead of chaining N tool calls.

Whitelist (entry points the LLM can pass as ``table=``):

* ``PRODUCTOS``
* ``VENT_FACT_VENT``
* ``VENT_ORDEN_VENTA``
* ``VENT_DEUD_CLIE``
* ``VENT_COBR_DEUD``

Allowed expansions (``expand=`` list), per entry point:

* ``PRODUCTOS``:        ``existencias``
* ``VENT_FACT_VENT``:   ``client``, ``lines``, ``debts``
* ``VENT_ORDEN_VENTA``: ``client``, ``lines``
* ``VENT_DEUD_CLIE``:   ``client``, ``invoice``, ``payments``
* ``VENT_COBR_DEUD``:   ``client``, ``applications``, ``forms``

Filters are exact-match (Velneo's REST has no LIKE / contains operator).
For free-text product search the agent should use ``search_products``
which fans out through the WORDS index + pgvector RAG.
"""

from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError

# Reuse the field projections from admin_ops to keep payloads consistent.
from mcp_theos.tools.admin_ops import (
    _COBR_FIELDS,
    _DEUD_FIELDS,
    _ENT_ERP_CLI_FIELDS,
    _ENT_FIELDS,
    _FACT_FIELDS,
    _INV_MOV_FIELDS,
    _ORDEN_FIELDS,
)

_PRODUCTOS_FIELDS = [
    "ID", "CODIGO", "NAME", "INV_FAMI", "VENDIBLE", "OFF",
]

_VENT_ORDEN_MOV_FIELDS = [
    "ID", "VENT_ORDEN_VENTA", "NUM_LINEA",
    "PRODUCTOS", "NAME",
    "CAN", "FACTOR",
    "PVP", "PVP_LINEA", "PRECIO_NETO_LINEA",
    "INV_BODEGA",
]

_DETALLE_COBROS_FIELDS = [
    "ID", "NAME", "COBROS", "VENT_DEUD_CLIE",
    "VALOR", "SALDO_ACTUAL", "NUEVO_SALDO", "FECHA_CONTA",
]

_DETALLE_COBROS_FORMAS_FIELDS = [
    "ID", "NAME", "COBROS", "TIPO_DE_COBRO",
    "VALOR", "VALOR_USADO", "PAGADO", "DISPONIBLE",
    "NRO_RECIBO",
]

_EXISTENCIAS_FIELDS = [
    "ID", "PRODUCTOS", "INV_BODEGA",
    "STOCK", "STOCK_RESERVADO",
    "STOCK_DISPONIBLE", "STOCK_PEDIDO",
    "COSTO_PROM",
]


# Each entry-point spec: default field projection and the set of allowed
# expansions. An expansion is one of three shapes:
#
#  * ``fk``       — a column on this row whose value is the ID of another
#                   row. We pull that row by record_id.
#  * ``child``    — rows in another table whose FK column points back to
#                   this row's ID. We pull those rows by filter.
#  * ``ent_join`` — special case for ENT_ERP_CLI: pull both the master
#                   (ENT) and the customer extension (ENT_ERP_CLI) and
#                   merge them. Used wherever the agent wants "el
#                   cliente" — gives one block with name/CIF/email
#                   /saldo/cupo.
ENTRY_POINTS: dict[str, dict[str, Any]] = {
    "PRODUCTOS": {
        "default_fields": _PRODUCTOS_FIELDS,
        "expand": {
            "existencias": {
                "kind": "child",
                "table": "EXISTENCIAS",
                "fk_field": "PRODUCTOS",
                "fields": _EXISTENCIAS_FIELDS,
                "limit": 50,
            },
        },
    },
    "VENT_FACT_VENT": {
        "default_fields": _FACT_FIELDS,
        "expand": {
            "client": {
                "kind": "ent_join",
                "source_field": "ENT_ERP_CLI",
            },
            "lines": {
                "kind": "child",
                "table": "INV_MOVIMIENTOS",
                "fk_field": "VENT_FACT_VENT",
                "fields": _INV_MOV_FIELDS,
                "limit": 500,
            },
            "debts": {
                "kind": "child",
                "table": "VENT_DEUD_CLIE",
                "fk_field": "VENT_FACT_VENT",
                "fields": _DEUD_FIELDS,
                "limit": 100,
            },
        },
    },
    "VENT_ORDEN_VENTA": {
        "default_fields": _ORDEN_FIELDS,
        "expand": {
            "client": {
                "kind": "ent_join",
                "source_field": "ENT_ERP_CLI",
            },
            "lines": {
                "kind": "child",
                "table": "VENT_ORDEN_MOVIMIENTOS",
                "fk_field": "VENT_ORDEN_VENTA",
                "fields": _VENT_ORDEN_MOV_FIELDS,
                "limit": 500,
            },
        },
    },
    "VENT_DEUD_CLIE": {
        "default_fields": _DEUD_FIELDS,
        "expand": {
            "client": {
                "kind": "ent_join",
                "source_field": "ENT_ERP_CLI",
            },
            "invoice": {
                "kind": "fk",
                "source_field": "VENT_FACT_VENT",
                "table": "VENT_FACT_VENT",
                "fields": _FACT_FIELDS,
            },
            "payments": {
                "kind": "child",
                "table": "DETALLE_COBROS",
                "fk_field": "VENT_DEUD_CLIE",
                "fields": _DETALLE_COBROS_FIELDS,
                "limit": 100,
            },
        },
    },
    "VENT_COBR_DEUD": {
        "default_fields": _COBR_FIELDS,
        "expand": {
            "client": {
                "kind": "ent_join",
                "source_field": "ENT_ERP_CLI",
            },
            "applications": {
                "kind": "child",
                "table": "DETALLE_COBROS",
                "fk_field": "COBROS",
                "fields": _DETALLE_COBROS_FIELDS,
                "limit": 200,
            },
            "forms": {
                "kind": "child",
                "table": "DETALLE_COBROS_FORMAS",
                "fk_field": "COBROS",
                "fields": _DETALLE_COBROS_FORMAS_FIELDS,
                "limit": 100,
            },
        },
    },
}

# Filter columns the agent is allowed to pass per entry table. Anything
# else gets dropped with a warning so the LLM does not silently pull
# unfiltered pages (Velneo would return the global first page).
ALLOWED_FILTERS: dict[str, set[str]] = {
    "PRODUCTOS": {"ID", "CODIGO", "INV_FAMI", "VENDIBLE", "OFF", "words"},
    "VENT_FACT_VENT": {
        "ID", "NRO_FAC", "ENT_ERP_CLI", "VENDEDOR",
        "ESTADO", "OFF", "FECHA",
    },
    "VENT_ORDEN_VENTA": {
        "ID", "ENT_ERP_CLI", "VENDEDOR",
        "ESTADO", "FACTURADO", "OFF", "FECHA",
    },
    "VENT_DEUD_CLIE": {
        "ID", "ENT_ERP_CLI", "VENT_FACT_VENT",
        "CON_SALDO", "POR_VENCER", "COBRADO", "OFF",
        "FECHA", "VENCIMIENTO",
    },
    "VENT_COBR_DEUD": {
        "ID", "ENT_ERP_CLI", "CAJERO",
        "SERIE", "SECUENCIA", "OFF",
        "FECHA", "FECHA_CONTA",
    },
}


async def _resolve_ent_join(
    client: VelneoClient, source_id: int,
) -> dict[str, Any] | None:
    """Pull ENT + ENT_ERP_CLI for a customer and return the merged view."""
    if not source_id:
        return None
    out: dict[str, Any] = {"id": source_id}
    try:
        ent = await client.get("ENT", record_id=source_id, fields=_ENT_FIELDS)
        if ent.rows:
            out["ent"] = ent.rows[0]
    except VelneoError:
        pass
    try:
        ext = await client.get(
            "ENT_ERP_CLI", record_id=source_id, fields=_ENT_ERP_CLI_FIELDS,
        )
        if ext.rows:
            out["ent_erp_cli"] = ext.rows[0]
    except VelneoError:
        pass
    if "ent" not in out and "ent_erp_cli" not in out:
        return None
    return out


async def _resolve_fk(
    client: VelneoClient, target_table: str, fk_value: Any,
    fields: list[str],
) -> dict[str, Any] | None:
    if fk_value in (None, 0, "", "0"):
        return None
    try:
        resp = await client.get(
            target_table, record_id=fk_value, fields=fields,
        )
    except VelneoError as exc:
        return {"_error": f"velneo {exc.status}: {exc.message}"}
    return resp.rows[0] if resp.rows else None


async def _resolve_child(
    client: VelneoClient,
    child_table: str,
    fk_field: str,
    parent_id: Any,
    fields: list[str],
    limit: int,
) -> dict[str, Any]:
    if parent_id in (None, 0, "", "0"):
        return {"rows": [], "count": 0}
    try:
        resp = await client.get(
            child_table,
            params={fk_field: parent_id, "pagesize": limit},
            fields=fields,
        )
    except VelneoError as exc:
        return {"rows": [], "count": 0, "_error": f"velneo {exc.status}: {exc.message}"}
    return {"rows": resp.rows[:limit], "count": len(resp.rows)}


async def search_velneo(
    client: VelneoClient,
    *,
    table: str,
    filters: dict[str, Any] | None = None,
    expand: list[str] | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Whitelisted lookup against a single Velneo entry-point table.

    See module docstring for the allowed tables and expansions. Filters
    are exact-match; pass ``filters={"words": "TOKEN"}`` on tables that
    expose the WORDS index (PRODUCTOS) for token-level search.
    """
    table_u = (table or "").upper().strip()
    spec = ENTRY_POINTS.get(table_u)
    if spec is None:
        return {
            "success": False,
            "error_code": "table_not_whitelisted",
            "error": f"table {table!r} not whitelisted",
            "allowed_tables": sorted(ENTRY_POINTS),
        }

    # Filter sanitization: drop anything not in the allow-list, but
    # record the drop so the agent gets feedback instead of silent
    # mis-filtering.
    allowed = ALLOWED_FILTERS.get(table_u, set())
    safe_filters: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in (filters or {}).items():
        if v is None:
            continue
        if k in allowed:
            safe_filters[k] = v
        else:
            dropped.append(k)

    params: dict[str, Any] = dict(safe_filters)
    params["pagesize"] = max(1, min(int(limit) if limit else 20, 200))

    field_projection = fields or spec["default_fields"]

    try:
        resp = await client.get(table_u, params=params, fields=field_projection)
    except VelneoError as exc:
        return {
            "success": False,
            "error": f"velneo {exc.status}: {exc.message}",
            "velneo_status": exc.status,
        }
    rows = list(resp.rows[:limit])

    # Expansion validation: drop unknown aliases, record them.
    allowed_expand = spec["expand"]
    expand_unknown: list[str] = []
    expand_use: list[str] = []
    for e in (expand or []):
        if e in allowed_expand:
            expand_use.append(e)
        else:
            expand_unknown.append(e)

    if expand_use:
        for row in rows:
            row["_expanded"] = {}
            for alias in expand_use:
                spec_e = allowed_expand[alias]
                kind = spec_e["kind"]
                if kind == "ent_join":
                    src_field = spec_e["source_field"]
                    src_id = row.get(src_field)
                    expanded = await _resolve_ent_join(client, int(src_id) if src_id else 0)
                elif kind == "fk":
                    expanded = await _resolve_fk(
                        client,
                        spec_e["table"],
                        row.get(spec_e["source_field"]),
                        spec_e["fields"],
                    )
                elif kind == "child":
                    expanded = await _resolve_child(
                        client,
                        spec_e["table"],
                        spec_e["fk_field"],
                        row.get("ID"),
                        spec_e["fields"],
                        spec_e.get("limit", 100),
                    )
                else:
                    expanded = None
                row["_expanded"][alias] = expanded

    out: dict[str, Any] = {
        "success": True,
        "table": table_u,
        "count": len(rows),
        "total_count": resp.total_count,
        "rows": rows,
        "filters_applied": safe_filters,
    }
    if dropped:
        out["filters_dropped"] = dropped
        out["filters_dropped_reason"] = (
            f"not in allow-list for {table_u}; "
            f"allowed: {sorted(allowed)}"
        )
    if expand_unknown:
        out["expand_unknown"] = expand_unknown
        out["expand_allowed"] = sorted(allowed_expand)
    return out
