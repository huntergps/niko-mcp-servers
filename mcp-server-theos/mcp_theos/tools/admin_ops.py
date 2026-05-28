"""Operational read-only tools for internal support agents.

These tools serve the *internal* team running on the same MCP as the
customer-facing tools. They are deliberately read-only — no writes, no
SRI side-effects (SRI is handled by Datil / iFactura externally, not
Theos). Tool gating is the agent's responsibility (``enabled_tools``);
this module simply makes the surface available.

Naming convention: every tool here is purely diagnostic / inspection-
oriented and operates against the Velneo REST API only. Tables touched
follow the whitelist agreed for ``search_velneo``:

* Entry points: PRODUCTOS, VENT_FACT_VENT, VENT_ORDEN_VENTA,
  VENT_DEUD_CLIE, VENT_COBR_DEUD.
* Related (FK-followed only): ENT, ENT_ERP_CLI, VENT_ORDEN_MOVIMIENTOS,
  INV_MOVIMIENTOS, DETALLE_COBROS, DETALLE_COBROS_FORMAS, EXISTENCIAS,
  INV_BODEGA, and assorted catalogs.
"""

from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError

# ---------------------------------------------------------------------------
# Field projections — kept short to keep payloads small. Each tool adds
# whichever extra columns it needs on top of these via ``fields=[...]``.
# ---------------------------------------------------------------------------

_ENT_FIELDS = [
    "ID", "NAME", "NOM_COM", "CIF",
    "MAIL_PRINCIPAL", "TFN_PRI", "DIR_PRI",
    "SIN_CREDITO", "SIN_CREDITO_RAZON",
    "OFF",
]

_ENT_ERP_CLI_FIELDS = [
    "ID", "NAME", "CIF",
    "SALDO", "DEUDASC", "CUPOC", "DISPONIBLE_CUPOC",
    "DIAS_VENCIDOS", "FACTVENCIDAS", "NO_VENDER",
    "TIPO_CONTRIBUYENTE", "SRI_TIPO_IDENTIFICACION",
    "TIPO_CLIENTE", "DESCUENTOC",
    "OFF",
]

_FACT_FIELDS = [
    "ID", "NAME", "FECHA", "FECHA_FACT",
    "ENT_ERP_CLI", "VENDEDOR",
    "SUBTOTAL", "BASE_IVA", "BASE0", "IVA", "TOTAL",
    "PAGADO", "SALDO",
    "ESTADO", "OFF", "OFF_MOTIVO",
    "NRO_FAC",
]

_ORDEN_FIELDS = [
    "ID", "NAME", "FECHA", "ENT_ERP_CLI", "VENDEDOR",
    "TOTAL", "PAGADO", "SALDO",
    "ESTADO", "FACTURADO", "OFF",
]

_DEUD_FIELDS = [
    "ID", "NAME", "FECHA", "VENCIMIENTO",
    "ENT_ERP_CLI", "EGRESOS",
    "TOTAL_DEUDA", "PAGADO", "SALDO",
    "DIAS", "DIAS_VENCIDOS",
    "CON_SALDO", "POR_VENCER", "COBRADO", "OFF",
    "REFERENCIA", "TIPO",
]

_COBR_FIELDS = [
    "ID", "NAME", "FECHA", "FECHA_CONTA",
    "ENT_ERP_CLI", "CAJERO",
    "VALOR", "VALOR_USADO", "SALDO",
    "SERIE", "SECUENCIA", "REFERENCIA",
    "OFF", "OFF_MOTIVO",
]

_INV_MOV_FIELDS = [
    "ID", "NUM_LINEA",
    "VENT_FACT_VENT", "VENT_ORDEN_VENTA",
    "PRODUCTOS", "NAME",
    "CAN", "FACTOR",
    "PVP", "PVP_LINEA", "PRECIO_NETO_LINEA",
    "INV_BODEGA",
]


def _short_date(value: Any) -> str:
    """``2026-05-16T00:00:00.000Z`` → ``2026-05-16`` (matches invoices.py)."""
    if not value or value == "Invalid Date":
        return ""
    s = str(value)
    return s[:10] if "T" in s else s


def _err(exc: VelneoError) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"velneo {exc.status}: {exc.message}",
        "velneo_status": exc.status,
    }


# ---------------------------------------------------------------------------
# inspect_partner — 360° view of a client for support diagnosis.
# ---------------------------------------------------------------------------


async def inspect_partner(
    client: VelneoClient,
    *,
    partner_id: int | None = None,
    cif: str | None = None,
) -> dict[str, Any]:
    """Pull ENT + ENT_ERP_CLI + recent activity snapshot for diagnosis.

    Use this when the user asks "qué pasa con el cliente X" or similar —
    it returns enough context to spot SIN_CREDITO, NO_VENDER, overdue
    debt, recent invoices and recent payments without forcing the agent
    to chain five tool calls.

    Identify the client by ``partner_id`` (ENT.ID == ENT_ERP_CLI.ID) or
    by ``cif`` (RUC / cédula). When both are passed ``partner_id`` wins.
    """
    if not partner_id and not cif:
        return {"success": False, "error": "partner_id or cif required"}

    # Resolve ENT row.
    if partner_id:
        try:
            ent = await client.get("ENT", record_id=partner_id, fields=_ENT_FIELDS)
        except VelneoError as exc:
            return _err(exc)
        ent_rows = ent.rows
    else:
        try:
            ent = await client.get(
                "ENT",
                params={"CIF": cif.strip(), "pagesize": 5},
                fields=_ENT_FIELDS,
            )
        except VelneoError as exc:
            return _err(exc)
        ent_rows = ent.rows

    if not ent_rows:
        return {
            "success": False,
            "error_code": "not_found",
            "error": f"partner not found (partner_id={partner_id} cif={cif!r})",
        }
    ent_row = ent_rows[0]
    pid = int(ent_row.get("ID") or 0)
    if not pid:
        return {"success": False, "error": "ENT row missing ID"}

    # ENT_ERP_CLI extension.
    erp_ext: dict[str, Any] | None = None
    try:
        ext = await client.get(
            "ENT_ERP_CLI", record_id=pid, fields=_ENT_ERP_CLI_FIELDS,
        )
        erp_ext = ext.rows[0] if ext.rows else None
    except VelneoError:
        erp_ext = None

    # Recent activity snapshot (small pages — diagnostic-grade, not for
    # full listing).
    snapshot: dict[str, Any] = {}
    for label, table, fields in (
        ("recent_invoices", "VENT_FACT_VENT", _FACT_FIELDS),
        ("recent_orders",   "VENT_ORDEN_VENTA", _ORDEN_FIELDS),
        ("open_debts",      "VENT_DEUD_CLIE", _DEUD_FIELDS),
        ("recent_payments", "VENT_COBR_DEUD", _COBR_FIELDS),
    ):
        try:
            resp = await client.get(
                table,
                params={"ENT_ERP_CLI": pid, "pagesize": 10},
                fields=fields,
            )
            rows = resp.rows
            if label == "open_debts":
                rows = [r for r in rows if not r.get("OFF")
                        and float(r.get("SALDO") or 0) > 0.01]
            snapshot[label] = rows
            snapshot[f"{label}_count"] = len(rows)
        except VelneoError as exc:
            snapshot[f"{label}_error"] = f"velneo {exc.status}: {exc.message}"

    flags: list[str] = []
    if ent_row.get("SIN_CREDITO"):
        flags.append("sin_credito")
    if ent_row.get("OFF"):
        flags.append("ent_off")
    if erp_ext:
        if erp_ext.get("NO_VENDER"):
            flags.append("no_vender")
        if erp_ext.get("OFF"):
            flags.append("erp_cli_off")
        if (erp_ext.get("DIAS_VENCIDOS") or 0):
            flags.append("dias_vencidos")
        if (erp_ext.get("FACTVENCIDAS") or 0):
            flags.append("facturas_vencidas")

    return {
        "success": True,
        "partner_id": pid,
        "ent": ent_row,
        "ent_erp_cli": erp_ext,
        "has_erp_cli": erp_ext is not None,
        "flags": flags,
        "snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# list_pending_invoices — invoices with open balance (collections view).
# ---------------------------------------------------------------------------


async def list_pending_invoices(
    client: VelneoClient,
    *,
    client_id: int | None = None,
    salesperson_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Active invoices with SALDO > 0 (collections-oriented).

    "Pending" here means *unpaid*. SRI-state filtering belongs in
    :func:`list_recent_invoices` (no static SRI-state mapping is wired
    on the MCP side until the team confirms which Velneo column carries
    it for Mepriga).
    """
    params: dict[str, Any] = {"pagesize": min(max(limit * 3, 20), 500)}
    if client_id:
        params["ENT_ERP_CLI"] = client_id
    if salesperson_id:
        params["VENDEDOR"] = salesperson_id

    try:
        resp = await client.get("VENT_FACT_VENT", params=params, fields=_FACT_FIELDS)
    except VelneoError as exc:
        return _err(exc)

    df = (date_from or "").strip()
    dt = (date_to or "").strip()
    out: list[dict[str, Any]] = []
    for r in resp.rows:
        if r.get("OFF"):
            continue
        if float(r.get("SALDO") or 0) <= 0.01:
            continue
        f = _short_date(r.get("FECHA"))
        if df and f < df:
            continue
        if dt and f and f > dt:
            continue
        out.append(r)
        if len(out) >= limit:
            break

    return {
        "success": True,
        "count": len(out),
        "total_scanned": len(resp.rows),
        "filter": {
            "client_id": client_id,
            "salesperson_id": salesperson_id,
            "date_from": df or None,
            "date_to": dt or None,
        },
        "invoices": out,
    }


# ---------------------------------------------------------------------------
# list_recent_invoices — generic recent-window lister (used as the
# "rejected" / "no autorizada" diagnostic lister too: the agent reads
# the ESTADO field and decides what counts as rejected for this tenant).
# ---------------------------------------------------------------------------


async def list_recent_invoices(
    client: VelneoClient,
    *,
    client_id: int | None = None,
    salesperson_id: int | None = None,
    estado: Any | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_off: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Recent invoices, optionally filtered by ESTADO.

    Pass ``estado`` to filter by the Velneo ``ESTADO`` value (exact
    match — Velneo REST is exact-match only). The mapping of ``ESTADO``
    values to human concepts (PENDIENTE, AUTORIZADO, DEVUELTO, NO
    AUTORIZADO) is tenant-specific and lives in the agent's skill, not
    here.
    """
    params: dict[str, Any] = {"pagesize": min(max(limit * 2, 20), 500)}
    if client_id:
        params["ENT_ERP_CLI"] = client_id
    if salesperson_id:
        params["VENDEDOR"] = salesperson_id
    if estado is not None:
        params["ESTADO"] = estado

    try:
        resp = await client.get("VENT_FACT_VENT", params=params, fields=_FACT_FIELDS)
    except VelneoError as exc:
        return _err(exc)

    df = (date_from or "").strip()
    dt = (date_to or "").strip()
    out: list[dict[str, Any]] = []
    for r in resp.rows:
        if not include_off and r.get("OFF"):
            continue
        f = _short_date(r.get("FECHA"))
        if df and f < df:
            continue
        if dt and f and f > dt:
            continue
        out.append(r)
        if len(out) >= limit:
            break

    return {
        "success": True,
        "count": len(out),
        "total_scanned": len(resp.rows),
        "filter": {
            "client_id": client_id,
            "salesperson_id": salesperson_id,
            "estado": estado,
            "date_from": df or None,
            "date_to": dt or None,
            "include_off": include_off,
        },
        "invoices": out,
    }


# ---------------------------------------------------------------------------
# get_invoice_detail — header + lines + linked debts + linked payments.
# ---------------------------------------------------------------------------


async def get_invoice_detail(
    client: VelneoClient,
    *,
    invoice_id: int | None = None,
    nro_fac: str | None = None,
) -> dict[str, Any]:
    """Single invoice with movement lines, generated debt rows, and the
    payment detail rows that have been applied against those debts.

    Identify by ``invoice_id`` (VENT_FACT_VENT.ID) or by ``nro_fac``
    (SRI invoice number string).
    """
    if not invoice_id and not nro_fac:
        return {"success": False, "error": "invoice_id or nro_fac required"}

    # Resolve header.
    if invoice_id:
        try:
            head = await client.get(
                "VENT_FACT_VENT", record_id=invoice_id, fields=_FACT_FIELDS,
            )
        except VelneoError as exc:
            return _err(exc)
    else:
        try:
            head = await client.get(
                "VENT_FACT_VENT",
                params={"NRO_FAC": nro_fac.strip(), "pagesize": 1},
                fields=_FACT_FIELDS,
            )
        except VelneoError as exc:
            return _err(exc)

    if not head.rows:
        return {
            "success": False,
            "error_code": "not_found",
            "error": f"invoice not found (id={invoice_id} nro_fac={nro_fac!r})",
        }
    header = head.rows[0]
    inv_id = int(header.get("ID") or 0)

    # Lines (INV_MOVIMIENTOS where VENT_FACT_VENT = inv_id).
    lines: list[dict[str, Any]] = []
    lines_error: str | None = None
    try:
        ln = await client.get(
            "INV_MOVIMIENTOS",
            params={"VENT_FACT_VENT": inv_id, "pagesize": 500},
            fields=_INV_MOV_FIELDS,
        )
        lines = ln.rows
    except VelneoError as exc:
        lines_error = f"velneo {exc.status}: {exc.message}"

    # Debts generated by this invoice (VENT_DEUD_CLIE where EGRESOS = inv_id).
    debts: list[dict[str, Any]] = []
    debts_error: str | None = None
    try:
        d = await client.get(
            "VENT_DEUD_CLIE",
            params={"VENT_FACT_VENT": inv_id, "pagesize": 100},
            fields=_DEUD_FIELDS,
        )
        debts = d.rows
    except VelneoError as exc:
        debts_error = f"velneo {exc.status}: {exc.message}"

    # Payments applied to the debts generated by this invoice. We pull
    # DETALLE_COBROS rows for each debt id, plus the parent COBROS
    # header. Capped at 200 detail rows to keep the response sane.
    payment_lines: list[dict[str, Any]] = []
    payment_error: str | None = None
    if debts:
        debt_ids = [d.get("ID") for d in debts if d.get("ID") is not None]
        try:
            for did in debt_ids[:20]:  # sane cap
                resp = await client.get(
                    "DETALLE_COBROS",
                    params={"VENT_DEUD_CLIE": did, "pagesize": 50},
                    fields=[
                        "ID", "NAME", "COBROS", "VENT_DEUD_CLIE",
                        "VALOR", "SALDO_ACTUAL", "NUEVO_SALDO",
                        "FECHA_CONTA",
                    ],
                )
                payment_lines.extend(resp.rows)
                if len(payment_lines) >= 200:
                    break
        except VelneoError as exc:
            payment_error = f"velneo {exc.status}: {exc.message}"

    out: dict[str, Any] = {
        "success": True,
        "invoice_id": inv_id,
        "header": header,
        "lines": lines,
        "line_count": len(lines),
        "debts": debts,
        "debt_count": len(debts),
        "payment_applications": payment_lines,
        "payment_application_count": len(payment_lines),
    }
    if lines_error:
        out["lines_error"] = lines_error
    if debts_error:
        out["debts_error"] = debts_error
    if payment_error:
        out["payment_error"] = payment_error
    return out


# ---------------------------------------------------------------------------
# list_recent_stock_movements — INV_MOVIMIENTOS for a product/bodega.
# ---------------------------------------------------------------------------


async def list_recent_stock_movements(
    client: VelneoClient,
    *,
    product_id: int | None = None,
    bodega_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Most recent ``INV_MOVIMIENTOS`` rows, optionally narrowed by
    product or bodega. Used for "por qué el stock dice X" forensics.

    At least one of ``product_id`` / ``bodega_id`` is required —
    pulling every movement in the ERP without a filter would walk
    millions of rows.
    """
    if not product_id and not bodega_id:
        return {
            "success": False,
            "error": "at least one of product_id, bodega_id is required",
        }
    params: dict[str, Any] = {"pagesize": min(max(limit, 1), 500)}
    if product_id:
        params["PRODUCTOS"] = product_id
    if bodega_id:
        params["INV_BODEGA"] = bodega_id

    try:
        resp = await client.get(
            "INV_MOVIMIENTOS", params=params, fields=_INV_MOV_FIELDS,
        )
    except VelneoError as exc:
        return _err(exc)

    return {
        "success": True,
        "filter": {"product_id": product_id, "bodega_id": bodega_id},
        "count": len(resp.rows),
        "total_count": resp.total_count,
        "movements": resp.rows[:limit],
    }


# ---------------------------------------------------------------------------
# inspect_product_stock — EXISTENCIAS per bodega + recent moves.
# ---------------------------------------------------------------------------


async def inspect_product_stock(
    client: VelneoClient,
    *,
    product_id: int,
    moves_limit: int = 20,
) -> dict[str, Any]:
    """Current stock per bodega plus the latest movements for a product.

    The two views together let the support agent answer "stock says X
    but yesterday we did Y, what happened" — EXISTENCIAS gives the
    snapshot, INV_MOVIMIENTOS gives the audit trail.
    """
    if not product_id:
        return {"success": False, "error": "product_id required"}

    existencias: list[dict[str, Any]] = []
    exist_error: str | None = None
    try:
        e = await client.get(
            "EXISTENCIAS",
            params={"PRODUCTOS": product_id, "pagesize": 500},
            fields=[
                "ID", "PRODUCTOS", "INV_BODEGA",
                "STOCK", "STOCK_RESERVADO",
                "STOCK_DISPONIBLE", "STOCK_PEDIDO",
                "COSTO_PROM",
            ],
        )
        existencias = e.rows
    except VelneoError as exc:
        exist_error = f"velneo {exc.status}: {exc.message}"

    moves: list[dict[str, Any]] = []
    moves_error: str | None = None
    try:
        m = await client.get(
            "INV_MOVIMIENTOS",
            params={"PRODUCTOS": product_id,
                    "pagesize": min(max(moves_limit, 1), 200)},
            fields=_INV_MOV_FIELDS,
        )
        moves = m.rows[:moves_limit]
    except VelneoError as exc:
        moves_error = f"velneo {exc.status}: {exc.message}"

    out: dict[str, Any] = {
        "success": True,
        "product_id": product_id,
        "existencias": existencias,
        "existencias_count": len(existencias),
        "recent_movements": moves,
        "recent_movement_count": len(moves),
    }
    if exist_error:
        out["existencias_error"] = exist_error
    if moves_error:
        out["moves_error"] = moves_error
    return out
