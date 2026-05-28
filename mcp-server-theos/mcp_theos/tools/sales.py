"""Velneo quotations (VENT_ORDEN_VENTA + VENT_ORDEN_MOVIMIENTOS)."""

from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError

_ORDEN_FIELDS = [
    "ID", "NAME", "FECHA", "ENT_ERP_CLI", "VENDEDOR",
    "EMP", "SUC", "INV_TARIFAS", "INV_BODEGA",
    "SUBTOTAL", "BASE_IVA", "BASE0", "IVA", "DCTO", "TOTAL",
    "PAGADO", "SALDO",
    "ESTADO", "FACTURADO", "OFF", "OFF_MOTIVO",
    "NUM_LINEA", "NUM_PROD",
    "REF", "REF2",
]

_MOV_FIELDS = [
    "ID", "VENT_ORDEN_VENTA", "NUM_LINEA", "PRODUCTOS",
    "NAME", "CAN", "FACTOR",
    "PVP", "PVP_LINEA",
    "PRECIO_BRUTO_LINEA", "DCTO_VTAS_LINEA", "PRECIO_NETO_LINEA", "IVA_LINEA",
    "INV_BODEGA",
]


async def create_quotation(
    client: VelneoClient,
    *,
    client_id: int,
    lines: list[dict[str, Any]],
    salesperson_id: int | None = None,
    company_id: int | None = None,
    branch_id: int | None = None,
    tariff_id: int | None = None,
    warehouse_id: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a sales order header + N line records.

    Each ``line`` dict needs: ``product_id`` (PRODUCTOS.ID), ``quantity``
    (CAN). Optional: ``unit_price`` (PVP), ``factor``, ``warehouse_id``.
    """
    if not client_id:
        return {"success": False, "error": "client_id required"}
    if not lines:
        return {"success": False, "error": "lines must be non-empty"}

    header: dict[str, Any] = {"ENT_ERP_CLI": client_id}
    if salesperson_id is not None:
        header["VENDEDOR"] = salesperson_id
    if company_id is not None:
        header["EMP"] = company_id
    if branch_id is not None:
        header["SUC"] = branch_id
    if tariff_id is not None:
        header["INV_TARIFAS"] = tariff_id
    if warehouse_id is not None:
        header["INV_BODEGA"] = warehouse_id
    if notes:
        header["NAME"] = notes.strip()[:80]

    try:
        order = await client.post("VENT_ORDEN_VENTA", header)
    except VelneoError as exc:
        return {
            "success": False,
            "error": f"create header failed: velneo {exc.status} {exc.message}",
        }

    order_id = order.get("ID")
    if order_id is None:
        return {"success": False, "error": "header created but no ID returned", "raw": order}

    created_lines: list[dict[str, Any]] = []
    line_errors: list[str] = []
    for i, line in enumerate(lines, start=1):
        product_id = line.get("product_id") or line.get("PRODUCTOS")
        quantity = line.get("quantity") if line.get("quantity") is not None else line.get("CAN")
        if product_id is None or quantity is None:
            line_errors.append(f"line {i}: missing product_id/quantity")
            continue
        body: dict[str, Any] = {
            "VENT_ORDEN_VENTA": order_id,
            "NUM_LINEA": i,
            "PRODUCTOS": product_id,
            "CAN": quantity,
        }
        if line.get("unit_price") is not None or line.get("PVP") is not None:
            body["PVP"] = line.get("unit_price") or line.get("PVP")
        if line.get("factor") is not None:
            body["FACTOR"] = line["factor"]
        if line.get("warehouse_id") is not None:
            body["INV_BODEGA"] = line["warehouse_id"]
        try:
            created = await client.post("VENT_ORDEN_MOVIMIENTOS", body)
            created_lines.append(created)
        except VelneoError as exc:
            line_errors.append(f"line {i}: velneo {exc.status} {exc.message}")

    return {
        "success": not line_errors,
        "order_id": order_id,
        "order": order,
        "lines_created": len(created_lines),
        "lines": created_lines,
        "errors": line_errors,
    }


async def get_quotation(
    client: VelneoClient,
    *,
    order_id: int,
) -> dict[str, Any]:
    try:
        header = await client.get("VENT_ORDEN_VENTA", record_id=order_id, fields=_ORDEN_FIELDS)
    except VelneoError as exc:
        return {"success": False, "error": f"velneo {exc.status} {exc.message}"}
    if not header.rows:
        return {"success": False, "error": f"order {order_id} not found"}

    lines = await client.get(
        "VENT_ORDEN_MOVIMIENTOS",
        params={"VENT_ORDEN_VENTA": order_id, "pagesize": 500},
        fields=_MOV_FIELDS,
    )
    return {
        "success": True,
        "order": header.rows[0],
        "lines": lines.rows,
        "line_count": len(lines.rows),
    }


async def list_quotations(
    client: VelneoClient,
    *,
    client_id: int | None = None,
    salesperson_id: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    params: dict[str, Any] = {"pagesize": limit}
    if client_id is not None:
        params["ENT_ERP_CLI"] = client_id
    if salesperson_id is not None:
        params["VENDEDOR"] = salesperson_id
    try:
        resp = await client.get("VENT_ORDEN_VENTA", params=params, fields=_ORDEN_FIELDS)
    except VelneoError as exc:
        return {"success": False, "error": f"velneo {exc.status} {exc.message}"}
    return {
        "success": True,
        "count": len(resp.rows),
        "total_count": resp.total_count,
        "orders": resp.rows[:limit],
    }
