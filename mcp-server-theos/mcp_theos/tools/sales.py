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


def _resolve_default_warehouse(cfg: Any) -> dict[str, Any] | None:
    """Pick the sellable warehouse to default the quotation to.

    Reads ``tenants.erp_api_extra.warehouses`` (loaded into ``cfg.extra``
    by the tenant resolver). Each entry has ``id``, ``suc``, ``emp`` and
    optionally ``default: true``.

    Order of preference:
      1. the entry flagged ``default: true``;
      2. otherwise the first entry as listed.

    Future upgrade — once the API key gets GET on EXISTENCIAS, this
    helper should aggregate stock across the lines and return the
    bodega with the highest total. The picker contract (returns one
    of the configured entries) stays the same, so callers don't
    change.
    """
    if cfg is None:
        return None
    extra = getattr(cfg, "extra", None) or {}
    warehouses = extra.get("warehouses") if isinstance(extra, dict) else None
    if not warehouses:
        return None
    for w in warehouses:
        if isinstance(w, dict) and w.get("default"):
            return w
    first = warehouses[0]
    return first if isinstance(first, dict) else None


def _resolve_quotation_defaults(cfg: Any) -> dict[str, Any]:
    """Return ``tenants.erp_api_extra.quotation_defaults`` (or empty)."""
    if cfg is None:
        return {}
    extra = getattr(cfg, "extra", None) or {}
    qd = extra.get("quotation_defaults") if isinstance(extra, dict) else None
    return qd if isinstance(qd, dict) else {}


async def _resolve_line_pricing(
    client: VelneoClient,
    *,
    product_id: int,
    tariff_id: int,
    presentation_codbar: str | None = None,
) -> dict[str, Any] | None:
    """Resolve the FK chain Velneo needs to compute PVP on a line.

    A VENT_ORDEN_MOVIMIENTOS row whose ``PVP`` and ``PVP_LINEA`` columns
    are *formulas* — Velneo evaluates them from
    ``COSTO_EMPAQUE × (1 + PORCENTAJE_UTILIDAD1 − PORCENTAJE_DSCTO_VTA)``.
    The formula needs all four inputs PLUS the FK to ``INV_PRECIOS_PRODUCTO``
    (the price-tier row) and ``INV_PRESENT_PRODUCTO`` (the codbar of the
    empaque). Without them every POSTed line lands with PVP=0.

    We don't try to *be* the formula — we look up the same source rows
    Velneo's own ``AGREGAR_LINEA_ORDEN`` proceso looks up, then let
    Velneo recompute. Path:

      1. ``INV_PRESENT_PRODUCTO`` filtered by ``INV_PRODUCTOS=product_id``
         → list of presentations for the product. Each row's ``ID`` IS
         the codbar string (e.g. ``"01PP"``, ``"01PPQ"``) and carries
         ``INV_PRESENTACIONES`` (FK to the global empaque dict) +
         ``FACTOR``.
      2. Pick the row whose codbar matches ``presentation_codbar`` (LLM
         override); otherwise default to the one with ``FACTOR=1`` (the
         unit empaque, what humans usually mean by "1 producto"); else
         the first listed.
      3. ``INV_PRECIOS_PRODUCTO`` filtered by
         ``(INV_PRODUCTOS, INV_PRESENTACIONES, INV_TARIFAS)``
         → the cost / utility / discount triple for that empaque on the
         requested tariff.

    Returns ``None`` if any lookup fails or yields no row — caller should
    fall back to its previous behaviour and let the LLM/operator see
    PVP=0 (instead of silently fabricating a price).
    """
    if not product_id or not tariff_id:
        return None

    try:
        presents = await client.get(
            "INV_PRESENT_PRODUCTO",
            params={"INV_PRODUCTOS": product_id, "pagesize": 50},
        )
    except VelneoError:
        return None
    if not presents.rows:
        return None

    chosen = None
    if presentation_codbar:
        chosen = next(
            (p for p in presents.rows if p.get("ID") == presentation_codbar),
            None,
        )
    if chosen is None:
        # Prefer the unit empaque (factor=1) — that is what humans mean
        # by "uno" when they don't specify a packaging.
        chosen = next(
            (p for p in presents.rows if p.get("FACTOR") == 1),
            presents.rows[0],
        )

    codbar = chosen.get("ID")
    inv_presentaciones = chosen.get("INV_PRESENTACIONES")
    factor = chosen.get("FACTOR") or 1
    if not codbar or not inv_presentaciones:
        return None

    # Velneo only indexes INV_PRODUCTOS + INV_TARIFAS on this table —
    # combining filter[INV_PRESENTACIONES] silently returns zero rows
    # even when matching data exists (verified empirically). So we
    # fetch the (product, tariff) tuple and pick the empaque row in
    # memory. Typical product has 1-3 prices per tariff so the cost
    # is negligible.
    try:
        precios = await client.get(
            "INV_PRECIOS_PRODUCTO",
            params={
                "INV_PRODUCTOS": product_id,
                "INV_TARIFAS": tariff_id,
                "pagesize": 20,
            },
        )
    except VelneoError:
        return None
    p = next(
        (r for r in precios.rows if r.get("INV_PRESENTACIONES") == inv_presentaciones),
        None,
    )
    if p is None:
        return None

    return {
        "INV_PRESENT_PRODUCTO": codbar,
        "COD_BAR": codbar,
        "INV_PRECIOS_PRODUCTO": p.get("ID"),
        "FACTOR": factor,
        "COSTO_EMPAQUE": p.get("COSTO_EMPAQUE"),
        "PORCENTAJE_UTILIDAD1": p.get("PORCENTAJE_UTILIDAD1"),
        "PORCENTAJE_DSCTO_VTA": p.get("PORCENTAJE_DSCTO") or 0,
        "INV_TIPO_COSTE": p.get("INV_TIPO_COSTE") or "1",
    }


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

    # If the caller didn't pick a warehouse / company / branch, fall back
    # to the tenant's default sellable warehouse (configured in
    # ``tenants.erp_api_extra.warehouses``). Without these three the
    # Velneo header lands without a fiscal context and the order can't
    # be turned into an invoice from the cashier's session.
    cfg = getattr(client, "cfg", None)
    defaults = None
    if warehouse_id is None and company_id is None and branch_id is None:
        defaults = _resolve_default_warehouse(cfg)
    qdefaults = _resolve_quotation_defaults(cfg)

    header: dict[str, Any] = {"ENT_ERP_CLI": client_id}
    if salesperson_id is not None:
        header["VENDEDOR"] = salesperson_id

    eff_emp = company_id if company_id is not None else (defaults or {}).get("emp")
    eff_suc = branch_id if branch_id is not None else (defaults or {}).get("suc")
    eff_bod = warehouse_id if warehouse_id is not None else (defaults or {}).get("id")
    eff_tariff = tariff_id if tariff_id is not None else qdefaults.get("tariff_id")

    if eff_emp is not None:
        header["EMP"] = eff_emp
    if eff_suc is not None:
        header["SUC"] = eff_suc
    if eff_tariff is not None:
        header["INV_TARIFAS"] = eff_tariff
    if eff_bod is not None:
        header["INV_BODEGA"] = eff_bod

    # Payment + fiscal-customer defaults — only filled when the tenant
    # config provides them; never invented from thin air. Callers can
    # still override by passing custom header fields via the future
    # ``header_extra`` arg (not exposed yet).
    pay_method = qdefaults.get("payment_method_id")
    if pay_method is not None:
        header["CAJ_FORM_PAGO1"] = pay_method
        # PORC + NRO_PAGOS travel together: a single-installment plan
        # (the only one the Niko bot will ever issue) is "100% en 1 pago".
        header["PORC_PAGO1"] = qdefaults.get("payment_percent", 100)
        header["NRO_PAGOS"] = qdefaults.get("payment_count", 1)
    vte = qdefaults.get("vta_tipo_ent")
    if vte is not None:
        header["VTA_TIPO_ENT"] = vte

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

        # Resolve INV_PRESENT_PRODUCTO + INV_PRECIOS_PRODUCTO + the
        # cost/utility/discount triple so Velneo's PVP formula has
        # everything it needs. The LLM can opt out by passing
        # ``presentation_codbar`` (a specific empaque). Without this
        # lookup the line saves but PVP stays at 0 — useless for the
        # cashier downstream.
        pricing = None
        if eff_tariff is not None:
            try:
                pricing = await _resolve_line_pricing(
                    client,
                    product_id=product_id,
                    tariff_id=eff_tariff,
                    presentation_codbar=line.get("presentation_codbar"),
                )
            except Exception as exc:  # noqa: BLE001 — never let pricing kill the line
                line_errors.append(
                    f"line {i}: pricing lookup failed ({type(exc).__name__}: {exc})"
                )
        if pricing:
            body.update(pricing)

        # Per-line override only after pricing — caller's explicit values
        # win over the resolved defaults (matches Velneo's manual-price
        # path: PRECIO_BRUTO_EMPAQUE + PRECIO_ACORDADO=true).
        if line.get("factor") is not None:
            body["FACTOR"] = line["factor"]
        unit_price = line.get("unit_price") or line.get("PVP")
        if unit_price is not None:
            body["PRECIO_BRUTO_EMPAQUE"] = unit_price
            body["PRECIO_ACORDADO"] = True

        line_wh = line.get("warehouse_id")
        if line_wh is None:
            line_wh = eff_bod
        if line_wh is not None:
            body["INV_BODEGA"] = line_wh
        # Each VENT_ORDEN_MOVIMIENTOS row also carries EMP — Velneo
        # denormalises it from the header so the cashier's reports
        # roll up by company without joining back to the order.
        if eff_emp is not None:
            body["EMP"] = eff_emp
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
        "applied": {
            "company_id": eff_emp,
            "branch_id": eff_suc,
            "warehouse_id": eff_bod,
            "tariff_id": eff_tariff,
            "payment_method_id": pay_method,
            "vta_tipo_ent": vte,
            "warehouse_source": "explicit" if defaults is None else "tenant_default",
        },
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
