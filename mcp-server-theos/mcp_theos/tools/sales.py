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
    "NAME", "NOMBRE", "COD_BAR", "INV_PRESENT_PRODUCTO",
    "CAN", "FACTOR",
    "PVP", "PVP_LINEA",
    "PRECIO_BRUTO_LINEA", "DCTO_VTAS_LINEA", "PRECIO_NETO_LINEA", "IVA_LINEA",
    "PORCENTAJE_DSCTO_VTA",
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


async def render_quotation_pdf(
    client: VelneoClient,
    *,
    order_id: int,
) -> dict[str, Any]:
    """Render a quotation as PDF, return base64 + filename.

    Tool name aligns with the canonical odoo name
    (``render_quotation_pdf``) so the orchestrator's forced-tool-choice
    and active-quotation logic work the same on both backends — see
    docstring of :func:`mcp_theos.tools.sales.add_quotation_line`.

    Velneo's REST API has no print endpoint (the desktop ticket prints
    via Velneo's own report engine). We render the proforma here with
    reportlab so the bot can attach it directly to a Telegram /
    WhatsApp message — same pattern as ``get_customer_statement_pdf``.
    """
    data = await get_quotation(client, order_id=order_id)
    if not data.get("success"):
        return data

    try:
        # Alias the renderer so the tool function (also called
        # render_quotation_pdf) doesn't shadow it.
        from mcp_theos.pdf import render_quotation_pdf as _render_pdf
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"PDF renderer unavailable: {type(exc).__name__}: {exc}",
        }

    import base64

    from mcp_theos.otp import _get_tenant_commercial_name
    brand = await _get_tenant_commercial_name(client.cfg.tenant_id)

    try:
        pdf_bytes = _render_pdf(data, brand=brand)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"PDF render failed: {type(exc).__name__}: {exc}",
        }

    return {
        "success": True,
        "order_id": order_id,
        "line_count": data.get("line_count"),
        "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        "pdf_filename": f"proforma_{order_id}.pdf",
    }


async def add_quotation_line(
    client: VelneoClient,
    *,
    order_id: int,
    product_id: int,
    quantity: float,
    presentation_codbar: str | None = None,
    unit_price: float | None = None,
) -> dict[str, Any]:
    """Append a line to an existing quotation (canonical odoo name).

    create_quotation builds a header + N lines in one call. Once the
    cotización is open and the customer says "agrega también X", the
    orchestrator forces ``add_quotation_line`` — that name was odoo-
    only until this commit; theos now exposes the same name so the
    forced-tool-choice path resolves on both backends without a
    backend-aware branch in :mod:`niko.agent.orchestrator`.

    Same lookup chain as create_quotation: resolves
    INV_PRESENT_PRODUCTO + INV_PRECIOS_PRODUCTO + cost/utility/discount
    via :func:`_resolve_line_pricing` so Velneo's PVP formula evaluates
    on the inserted row. ``NUM_LINEA`` is auto-incremented past the
    highest existing line in the order — Velneo doesn't enforce
    uniqueness, but the cashier UI sorts by it so collisions look
    like duplicates.
    """
    if not order_id:
        return {"success": False, "error": "order_id required"}
    if not product_id or quantity is None:
        return {"success": False, "error": "product_id + quantity required"}

    # Fetch the header to inherit EMP / INV_BODEGA / INV_TARIFAS and to
    # pick the next NUM_LINEA. If the order doesn't exist we surface a
    # clear error instead of POSTing an orphan line.
    header_resp = await get_quotation(client, order_id=order_id)
    if not header_resp.get("success"):
        return header_resp
    header = header_resp.get("order") or {}
    existing = header_resp.get("lines") or []
    next_num = (max((int(l.get("NUM_LINEA") or 0) for l in existing), default=0)) + 1

    eff_emp = header.get("EMP")
    eff_bod = header.get("INV_BODEGA")
    eff_tariff = header.get("INV_TARIFAS")

    body: dict[str, Any] = {
        "VENT_ORDEN_VENTA": order_id,
        "NUM_LINEA": next_num,
        "PRODUCTOS": product_id,
        "CAN": quantity,
    }

    pricing = None
    if eff_tariff:
        try:
            pricing = await _resolve_line_pricing(
                client,
                product_id=product_id,
                tariff_id=eff_tariff,
                presentation_codbar=presentation_codbar,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": (
                    f"pricing lookup failed ({type(exc).__name__}: {exc}); "
                    "line not added"
                ),
            }
    if pricing:
        body.update(pricing)

    if unit_price is not None:
        body["PRECIO_BRUTO_EMPAQUE"] = unit_price
        body["PRECIO_ACORDADO"] = True

    if eff_bod is not None:
        body["INV_BODEGA"] = eff_bod
    if eff_emp is not None:
        body["EMP"] = eff_emp

    try:
        created = await client.post("VENT_ORDEN_MOVIMIENTOS", body)
    except VelneoError as exc:
        return {
            "success": False,
            "error": f"velneo {exc.status} {exc.message}",
        }

    return {
        "success": True,
        "order_id": order_id,
        "line": created,
        "line_count": len(existing) + 1,
        "num_linea": next_num,
        "applied": {
            "company_id": eff_emp,
            "warehouse_id": eff_bod,
            "tariff_id": eff_tariff,
            "presentation_codbar": pricing.get("COD_BAR") if pricing else None,
        },
    }


async def update_quotation_line(
    client: VelneoClient,
    *,
    line_id: int,
    quantity: float | None = None,
    unit_price: float | None = None,
) -> dict[str, Any]:
    """Update an existing quotation line (canonical odoo name).

    The niko_saas API key on Velneo does not currently grant PATCH/PUT
    on VENT_ORDEN_MOVIMIENTOS (verified 405). Returns the verbatim
    not-supported-yet envelope the LLM is trained to forward to the
    customer ("pídele a un asesor humano que modifique la línea desde
    el ERP"), mirroring the update_partner stub. Lights up
    transparently when an admin grants the method on the API key —
    no code change needed beyond removing this guard.
    """
    _ = (client, line_id, quantity, unit_price)
    return {
        "success": False,
        "error_code": "not_supported_yet",
        "message": (
            "La modificación de líneas de cotización vía API no está "
            "habilitada todavía para Mepriga (API key niko_saas sin "
            "permiso PATCH en VENT_ORDEN_MOVIMIENTOS). Por ahora, "
            "borra la cotización con cancel_quotation y crea una nueva, "
            "o pídele a un asesor que ajuste la línea desde Theos."
        ),
    }


async def remove_quotation_line(
    client: VelneoClient,
    *,
    line_id: int,
) -> dict[str, Any]:
    """Remove a quotation line (canonical odoo name).

    Same situation as :func:`update_quotation_line`: the niko_saas API
    key does not currently grant DELETE on VENT_ORDEN_MOVIMIENTOS
    (verified 405). Returns a not-supported-yet envelope so the LLM
    surfaces an honest message instead of pretending the line was
    removed.
    """
    _ = (client, line_id)
    return {
        "success": False,
        "error_code": "not_supported_yet",
        "message": (
            "El borrado de líneas de cotización vía API no está "
            "habilitado todavía para Mepriga (API key niko_saas sin "
            "permiso DELETE en VENT_ORDEN_MOVIMIENTOS). Por ahora, "
            "borra la cotización con cancel_quotation y crea una nueva, "
            "o pídele a un asesor que quite la línea desde Theos."
        ),
    }
