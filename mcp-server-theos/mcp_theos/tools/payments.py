"""Velneo customer payments (VENT_COBR_DEUD + DETALLE_COBROS)."""

from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError

_COBR_FIELDS = [
    "ID", "NAME", "FECHA", "FECHA_CONTA",
    "ENT_ERP_CLI", "CAJERO",
    "VALOR", "VALOR_USADO", "SALDO",
    "VALOR_EFECTIVO", "VALOR_CHEQUE", "VALOR_TARJETA",
    "VALOR_RET", "VALOR_NC", "VALOR_TRANSFERENCIA",
    "SERIE", "SECUENCIA", "REFERENCIA",
    "OFF", "OFF_MOTIVO",
]

_DETALLE_FIELDS = [
    "ID", "NAME", "COBROS", "VENT_DEUD_CLIE",
    "VALOR", "SALDO_ACTUAL", "NUEVO_SALDO",
    "CONTADO", "NRO_PAGOS",
]


async def get_customer_payments(
    client: VelneoClient,
    *,
    client_id: int,
    limit: int = 30,
    include_detail: bool = False,
) -> dict[str, Any]:
    try:
        resp = await client.get(
            "VENT_COBR_DEUD",
            params={"ENT_ERP_CLI": client_id, "pagesize": limit},
            fields=_COBR_FIELDS,
        )
    except VelneoError as exc:
        return {"success": False, "error": f"velneo {exc.status} {exc.message}"}

    payments = resp.rows[:limit]
    if include_detail:
        for p in payments:
            pid = p.get("ID")
            if pid is None:
                continue
            try:
                d = await client.get(
                    "DETALLE_COBROS",
                    params={"COBROS": pid, "pagesize": 100},
                    fields=_DETALLE_FIELDS,
                )
                p["applied_to"] = d.rows
            except VelneoError as exc:
                p["_detail_error"] = f"velneo {exc.status} {exc.message}"

    return {
        "success": True,
        "client_id": client_id,
        "count": len(payments),
        "total_count": resp.total_count,
        "payments": payments,
    }
