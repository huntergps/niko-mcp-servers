"""Velneo invoices (VENT_FACT_VENT) + balance (ENT_ERP_CLI / VENT_DEUD_CLIE)."""

from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError

_FACT_FIELDS = [
    "ID", "NAME", "FECHA", "FECHA_FACT",
    "ENT_ERP_CLI", "VENDEDOR",
    "SUBTOTAL", "BASE_IVA", "BASE0", "IVA", "TOTAL",
    "PAGADO", "SALDO",
    "ESTADO", "OFF",
    "NRO_FAC",
]

_DEUD_FIELDS = [
    "ID", "NAME", "FECHA", "VENCIMIENTO",
    "ENT_ERP_CLI", "EGRESOS",
    "TOTAL_DEUDA", "PAGADO", "SALDO",
    "DIAS", "DIAS_VENCIDOS",
    "CON_SALDO", "POR_VENCER", "COBRADO", "OFF",
    "NRO_DEUDA", "NRO_TOTAL_DEUDAS",
]


async def get_customer_invoices(
    client: VelneoClient,
    *,
    client_id: int,
    limit: int = 30,
    include_lines: bool = False,
) -> dict[str, Any]:
    try:
        resp = await client.get(
            "VENT_FACT_VENT",
            params={"ENT_ERP_CLI": client_id, "pagesize": limit},
            fields=_FACT_FIELDS,
        )
    except VelneoError as exc:
        return {"success": False, "error": f"velneo {exc.status} {exc.message}"}

    invoices = resp.rows[:limit]
    if include_lines:
        for inv in invoices:
            inv_id = inv.get("ID")
            if inv_id is None:
                continue
            try:
                ln = await client.get(
                    "INV_MOVIMIENTOS",
                    params={"VENT_FACT_VENT": inv_id, "pagesize": 200},
                    fields=[
                        "ID", "VENT_FACT_VENT", "NUM_LINEA", "PRODUCTOS",
                        "NAME", "CAN", "PVP", "PVP_LINEA",
                        "PRECIO_NETO_LINEA", "IVA_LINEA",
                    ],
                )
                inv["lines"] = ln.rows
            except VelneoError as exc:
                inv["_lines_error"] = f"velneo {exc.status} {exc.message}"

    return {
        "success": True,
        "client_id": client_id,
        "count": len(invoices),
        "total_count": resp.total_count,
        "invoices": invoices,
    }


async def check_balance(
    client: VelneoClient,
    *,
    client_id: int,
    detailed: bool = False,
) -> dict[str, Any]:
    """Fast path: ENT_ERP_CLI.SALDO / DEUDASC / CUPOC.

    Detailed path: pull VENT_DEUD_CLIE rows with CON_SALDO=1 so the
    caller can list per-invoice ageing.
    """
    try:
        ext = await client.get(
            "ENT_ERP_CLI",
            record_id=client_id,
            fields=[
                "ID", "NAME", "CIF",
                "SALDO", "SALDOP",
                "DEUDASC", "DEUDASCP",
                "CUPOC", "DISPONIBLE_CUPOC",
                "DEUDAS_VENCIDAS", "DIAS_VENCIDOS",
                "FACTVENCIDAS", "NO_VENDER",
                "ANTICIPOS_VTAS", "ANTICIPOS_DISPO_VTAS",
            ],
        )
    except VelneoError as exc:
        return {"success": False, "error": f"velneo {exc.status} {exc.message}"}
    if not ext.rows:
        return {"success": False, "error": f"client {client_id} not found in ENT_ERP_CLI"}

    summary = ext.rows[0]
    out: dict[str, Any] = {
        "success": True,
        "client_id": client_id,
        "summary": summary,
    }

    if detailed:
        try:
            deuds = await client.get(
                "VENT_DEUD_CLIE",
                params={"ENT_ERP_CLI": client_id, "CON_SALDO": 1, "pagesize": 200},
                fields=_DEUD_FIELDS,
            )
            out["debts"] = deuds.rows
            out["debt_count"] = len(deuds.rows)
        except VelneoError as exc:
            out["debts_error"] = f"velneo {exc.status} {exc.message}"

    return out
