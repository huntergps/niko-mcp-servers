"""Sales tools — quotations, sale orders, customer queries."""

import logging
import time
import traceback

from mcp_odoo.tools.generic import odoo_search, odoo_read, odoo_create, odoo_call_method

logger = logging.getLogger("mcp_odoo.sales")


def _log_call(tool: str, tenant_id: str, args: dict, result: dict | None, error: str | None, elapsed_ms: int):
    """Structured log of every Odoo tool call. Goes to stdout (captured by docker logs)."""
    payload = {
        "evt": "odoo_tool_call",
        "tool": tool,
        "tenant_id": tenant_id,
        "args": args,
        "elapsed_ms": elapsed_ms,
        "ok": error is None,
    }
    if error:
        payload["error"] = error
    else:
        payload["result_keys"] = list((result or {}).keys())
    if error:
        logger.error("ODOO_CALL %s", payload)
    else:
        logger.info("ODOO_CALL %s", payload)


def _build_card(order_data: dict) -> dict:
    """Build a standard _card dict from any order response.

    ERP-agnostic: the orchestrator reads _card instead of ERP-specific fields
    to build OrderCard objects for Telegram/WhatsApp inline keyboards.
    """
    partner = order_data.get("partner") or {}
    return {
        "order_id": order_data.get("order_id") or order_data.get("id"),
        "order_name": order_data.get("name", ""),
        "partner_name": (
            partner.get("name", "") if isinstance(partner, dict) else str(partner)
        ),
        "state_label": order_data.get("state_label", order_data.get("state", "")),
        "total": float(order_data.get("total", order_data.get("amount_total", 0))),
        "lines_count": len(order_data.get("lines", [])),
    }


def odoo_create_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
    lines: list[dict],
    notes: str = "",
    end_customer_name: str | None = None,
    end_customer_phone: str | None = None,
    end_customer_email: str | None = None,
) -> dict:
    """Create a sale order (quotation/proforma) in draft state.

    Args:
        partner_id: res.partner ID
        lines: List of dicts with {product_id, quantity, price_unit (optional)}
        notes: Optional notes for the order
        end_customer_name: Name of the end customer (consumidor final)
        end_customer_phone: Phone of the end customer
        end_customer_email: Email of the end customer

    Returns the created order details (read-after-write).
    On failure returns {success: False, error_code, error_detail, partner_id, lines}.
    """
    started = time.time()
    log_args = {
        "partner_id": partner_id,
        "lines_count": len(lines),
        "first_line": lines[0] if lines else None,
    }

    # ── Pre-flight validation ────────────────────────────────────────────
    if not partner_id or not isinstance(partner_id, int):
        err = "partner_id requerido y debe ser entero"
        _log_call("create_quotation", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_partner_id",
            "error_detail": err,
        }
    if not lines:
        err = "lines vacio: se requiere al menos un producto"
        _log_call("create_quotation", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "no_lines",
            "error_detail": err,
        }

    # Verify partner exists in Odoo before attempting create
    try:
        partner_check = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [partner_id], ["id", "name", "vat"],
        )
        if not partner_check:
            err = f"Partner id={partner_id} no existe en Odoo (res.partner)"
            _log_call("create_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
            return {
                "success": False,
                "error_code": "partner_not_found",
                "error_detail": err,
                "partner_id": partner_id,
            }
    except Exception as e:
        err = f"Error verificando partner: {e}"
        _log_call("create_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "partner_check_failed",
            "error_detail": err,
            "partner_id": partner_id,
        }

    # The entire RAG/search/details pipeline canonicalizes on `product.template`
    # IDs (that's what _fetch_products_live, get_product_details and the
    # embedding store all use, matching what the Odoo UI shows). But
    # sale.order.line needs the `product.product` (variant) ID — Odoo enforces
    # this at the ORM level. So at THIS boundary (and only here) we resolve
    # template_id → first active variant. We also capture uom_id because
    # TecnoSmart's flex_erp override KeyError's on missing 'product_uom'.
    template_ids = list({line["product_id"] for line in lines})
    try:
        variants = odoo_search(
            tenant_id, url, db, user, password,
            "product.product",
            [["product_tmpl_id", "in", template_ids], ["active", "=", True]],
            ["id", "product_tmpl_id", "uom_id"],
        )
    except Exception as e:
        err = f"Error resolviendo variantes de templates {template_ids}: {e}"
        _log_call("create_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "variant_lookup_failed",
            "error_detail": err,
            "partner_id": partner_id,
        }

    # First active variant per template + its uom
    variant_by_template: dict[int, int] = {}
    uom_by_variant: dict[int, int] = {}
    for v in variants:
        tmpl_id = v["product_tmpl_id"][0] if isinstance(v.get("product_tmpl_id"), list) else v.get("product_tmpl_id")
        if tmpl_id in variant_by_template:
            continue
        variant_by_template[tmpl_id] = v["id"]
        uom_by_variant[v["id"]] = v["uom_id"][0] if isinstance(v.get("uom_id"), list) and v["uom_id"] else 1

    unresolved = [tid for tid in template_ids if tid not in variant_by_template]
    if unresolved:
        err = f"product.template ids sin variante activa: {unresolved}"
        _log_call("create_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "template_no_variant",
            "error_detail": err,
            "unresolved_ids": unresolved,
            "partner_id": partner_id,
        }

    order_lines = []
    for line in lines:
        tmpl_id = line["product_id"]  # treated as template_id throughout
        variant_pid = variant_by_template[tmpl_id]
        line_vals = {
            "product_id": variant_pid,
            "product_uom_qty": line.get("quantity", 1),
            "product_uom": uom_by_variant.get(variant_pid, 1),
        }
        if "price_unit" in line:
            line_vals["price_unit"] = line["price_unit"]
        if "discount" in line:
            line_vals["discount"] = line["discount"]
        order_lines.append((0, 0, line_vals))

    values = {
        "partner_id": partner_id,
        "order_line": order_lines,
    }
    if notes:
        values["note"] = notes

    # End customer fields (consumidor final)
    if end_customer_name:
        values["end_customer_name"] = end_customer_name
    if end_customer_phone:
        values["end_customer_phone"] = end_customer_phone
    if end_customer_email:
        values["end_customer_email"] = end_customer_email

    # ── Create the sale.order ────────────────────────────────────────────
    try:
        order_id = odoo_create(
            tenant_id, url, db, user, password,
            "sale.order", values,
        )
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("ODOO_CALL create_quotation FAILED tenant=%s partner=%s err=%s\n%s",
                     tenant_id, partner_id, e, tb)
        _log_call("create_quotation", tenant_id, log_args, None, str(e), elapsed)
        return {
            "success": False,
            "error_code": "odoo_create_failed",
            "error_detail": str(e),
            "partner_id": partner_id,
            "lines_attempted": len(lines),
        }

    # Read-after-write: order header
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["name", "state", "partner_id", "amount_untaxed", "amount_tax",
             "amount_total", "order_line", "date_order", "share_link_so"],
        )
    except Exception as e:
        err = f"Order created (id={order_id}) but read-after-write failed: {e}"
        _log_call("create_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "read_after_write_failed",
            "error_detail": err,
            "order_id": order_id,
        }

    if not orders:
        return {"success": False, "error_code": "not_found", "error_detail": "Order created but not found", "order_id": order_id}

    order = orders[0]

    # Read order lines for detailed response
    line_ids = order.get("order_line", [])
    order_lines_detail = []
    if line_ids:
        raw_lines = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order.line", line_ids,
            ["product_id", "name", "product_uom_qty", "price_unit",
             "discount", "price_subtotal", "price_tax", "price_total"],
        )
        for ln in (raw_lines or []):
            product_name = ln["product_id"][1] if isinstance(ln.get("product_id"), list) else ln.get("name", "")
            order_lines_detail.append({
                "product": product_name,
                "quantity": ln.get("product_uom_qty", 1),
                "price_unit": ln.get("price_unit", 0),
                "discount": ln.get("discount", 0),
                "subtotal": ln.get("price_subtotal", 0),
                "tax": ln.get("price_tax", 0),
                "total": ln.get("price_total", 0),
            })

    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else str(order.get("partner_id", ""))

    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "partner": partner_name,
        "lines": order_lines_detail,
        "subtotal": order["amount_untaxed"],
        "tax": order["amount_tax"],
        "total": order["amount_total"],
        "share_link": order.get("share_link_so") or "",
    }
    result["_card"] = _build_card(result)
    _log_call("create_quotation", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
    return result


def odoo_add_to_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    lines: list[dict],
    confirmed: bool = False,
    session_active_quotation_id: int | None = None,
) -> dict:
    """Append product lines to an existing sale.order in draft state.

    Use this when the customer is already chatting about an existing
    quotation and wants to add another product to it. The order must be
    in 'draft' or 'sent' state — confirmed orders are immutable.

    IMPORTANT: Call first with confirmed=False (default) to get a preview.
    Show the preview to the user. Only call with confirmed=True after receiving
    explicit confirmation ('sí', 'confirmo', 'dale').

    Args:
        order_id: existing sale.order ID
        lines: [{product_id (template_id), quantity}, ...]
        confirmed: False = dry-run preview only; True = execute the write

    Returns the updated order summary (same shape as create_quotation) when
    confirmed=True, or a preview dict when confirmed=False.
    """
    started = time.time()
    log_args = {"order_id": order_id, "lines_count": len(lines), "confirmed": confirmed}

    if not isinstance(order_id, int) or order_id <= 0:
        err = "order_id requerido y debe ser entero positivo"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_order_id", "error_detail": err}
    if not lines:
        err = "lines vacio"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "no_lines", "error_detail": err}

    # Validate that the order_id matches the quotation the user explicitly
    # selected in this session (forwarded via X-Active-Quotation-Id header).
    # This prevents the LLM from autonomously acting on a quotation it inferred
    # via get_latest_quotation without the user having confirmed the selection.
    if session_active_quotation_id is None:
        return {
            "success": False,
            "error_code": "no_active_quotation",
            "error_detail": (
                "No hay cotización activa en la sesión del usuario. "
                "Pregunta al usuario qué cotización quiere modificar, o si desea crear una nueva."
            ),
        }
    if order_id != session_active_quotation_id:
        return {
            "success": False,
            "error_code": "quotation_not_in_session",
            "error_detail": (
                f"La cotización {order_id} no fue seleccionada por el usuario en esta sesión. "
                f"La cotización activa en sesión es {session_active_quotation_id}. "
                "Pregunta al usuario cuál cotización quiere modificar antes de proceder."
            ),
        }

    # Verify order exists and is editable (draft/sent)
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id], ["id", "state", "partner_id", "name", "amount_total"],
        )
    except Exception as e:
        err = f"Error leyendo sale.order {order_id}: {e}"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "order_read_failed", "error_detail": err}

    if not orders:
        err = f"sale.order {order_id} no existe"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "order_not_found", "error_detail": err}

    order = orders[0]
    if order["state"] not in ("draft", "sent"):
        err = f"sale.order {order['name']} esta en estado '{order['state']}', no editable"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "order_not_editable",
            "error_detail": err,
            "order_id": order_id,
            "state": order["state"],
        }

    # Dry-run / preview — return what would be done without writing to Odoo
    if not confirmed:
        line_descriptions = []
        for ln in lines:
            pid = ln.get("product_id", "?")
            qty = ln.get("quantity", 1)
            line_descriptions.append(f"product_id={pid} x{qty}")
        order_name = order.get("name", f"orden {order_id}")
        current_total = order.get("amount_total", 0)
        preview_msg = (
            f"Agregaras {', '.join(line_descriptions)} a {order_name} "
            f"(total actual: USD {current_total:.2f}). "
            "Llama de nuevo con confirmed=true para proceder."
        )
        result = {
            "success": False,
            "requires_confirmation": True,
            "preview": preview_msg,
            "action": "add_to_quotation",
            "order_id": order_id,
            "order_name": order_name,
            "lines_to_add": line_descriptions,
        }
        _log_call("add_to_quotation", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
        return result

    # Resolve template_ids → variant + uom (same logic as create_quotation)
    template_ids = list({line["product_id"] for line in lines})
    try:
        variants = odoo_search(
            tenant_id, url, db, user, password,
            "product.product",
            [["product_tmpl_id", "in", template_ids], ["active", "=", True]],
            ["id", "product_tmpl_id", "uom_id"],
        )
    except Exception as e:
        err = f"Error resolviendo variantes: {e}"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "variant_lookup_failed", "error_detail": err}

    variant_by_template: dict[int, int] = {}
    uom_by_variant: dict[int, int] = {}
    for v in variants:
        tmpl_id = v["product_tmpl_id"][0] if isinstance(v.get("product_tmpl_id"), list) else v.get("product_tmpl_id")
        if tmpl_id in variant_by_template:
            continue
        variant_by_template[tmpl_id] = v["id"]
        uom_by_variant[v["id"]] = v["uom_id"][0] if isinstance(v.get("uom_id"), list) and v["uom_id"] else 1

    unresolved = [tid for tid in template_ids if tid not in variant_by_template]
    if unresolved:
        err = f"product.template ids sin variante activa: {unresolved}"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "template_no_variant", "error_detail": err, "unresolved_ids": unresolved}

    # Build new sale.order.line records and write to existing order via (0,0,vals) commands
    new_line_cmds = []
    for line in lines:
        tmpl_id = line["product_id"]
        variant_pid = variant_by_template[tmpl_id]
        line_vals = {
            "order_id": order_id,
            "product_id": variant_pid,
            "product_uom_qty": line.get("quantity", 1),
            "product_uom": uom_by_variant.get(variant_pid, 1),
        }
        if "price_unit" in line:
            line_vals["price_unit"] = line["price_unit"]
        if "discount" in line:
            line_vals["discount"] = line["discount"]
        new_line_cmds.append(line_vals)

    # Create lines directly attached to order_id (more reliable than write+(0,0))
    try:
        for vals in new_line_cmds:
            odoo_create(
                tenant_id, url, db, user, password,
                "sale.order.line", vals,
            )
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("ODOO_CALL add_to_quotation FAILED order=%s err=%s\n%s", order_id, e, tb)
        _log_call("add_to_quotation", tenant_id, log_args, None, str(e), elapsed)
        return {
            "success": False,
            "error_code": "line_create_failed",
            "error_detail": str(e),
            "order_id": order_id,
        }

    # Read-after-write the updated order
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["name", "state", "partner_id", "amount_untaxed", "amount_tax",
             "amount_total", "order_line", "share_link_so"],
        )
        order = orders[0]
        line_ids = order.get("order_line", [])
        raw_lines = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order.line", line_ids,
            ["product_id", "product_uom_qty", "price_unit", "price_subtotal", "price_total"],
        ) if line_ids else []

        order_lines_detail = []
        for ln in raw_lines:
            product_name = ln["product_id"][1] if isinstance(ln.get("product_id"), list) else ""
            order_lines_detail.append({
                "product": product_name,
                "quantity": ln.get("product_uom_qty", 1),
                "price_unit": ln.get("price_unit", 0),
                "subtotal": ln.get("price_subtotal", 0),
                "total": ln.get("price_total", 0),
            })
    except Exception as e:
        err = f"Lines created but read-after-write failed: {e}"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "read_after_write_failed", "error_detail": err, "order_id": order_id}

    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else ""
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "partner": partner_name,
        "lines": order_lines_detail,
        "lines_added": len(new_line_cmds),
        "subtotal": order["amount_untaxed"],
        "tax": order["amount_tax"],
        "total": order["amount_total"],
        "share_link": order.get("share_link_so") or "",
    }
    result["_card"] = _build_card(result)
    _log_call("add_to_quotation", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
    return result


def odoo_get_active_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
) -> dict:
    """Find the most recent draft sale.order for a partner, if any.

    Use this BEFORE create_quotation when the customer adds another
    product to "the same quote" — to avoid creating a duplicate.
    Returns {success, order_id, name, state, total, lines} or
    {success: False, error_code: 'no_active_quote'}.
    """
    started = time.time()
    log_args = {"partner_id": partner_id}

    if not isinstance(partner_id, int):
        return {"success": False, "error_code": "invalid_partner_id"}

    try:
        # Find most recent draft order for this partner
        order_ids = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order",
            [["partner_id", "=", partner_id], ["state", "in", ["draft", "sent"]]],
            ["id"],
            limit=1,
            order="create_date desc",
        )
    except Exception as e:
        err = f"Error buscando cotizaciones activas: {e}"
        _log_call("get_active_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "search_failed", "error_detail": err}

    if not order_ids:
        _log_call("get_active_quotation", tenant_id, log_args, {"found": False}, None, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "no_active_quote", "partner_id": partner_id}

    order_id = order_ids[0]["id"] if isinstance(order_ids[0], dict) else order_ids[0]
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["name", "state", "amount_total", "order_line", "create_date"],
        )
        order = orders[0]
        line_ids = order.get("order_line", [])
        raw_lines = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order.line", line_ids,
            ["product_id", "product_uom_qty", "price_total"],
        ) if line_ids else []

        lines_detail = [
            {
                "product": ln["product_id"][1] if isinstance(ln.get("product_id"), list) else "",
                "quantity": ln.get("product_uom_qty", 1),
                "total": ln.get("price_total", 0),
            }
            for ln in raw_lines
        ]
    except Exception as e:
        err = f"Error leyendo cotizacion: {e}"
        _log_call("get_active_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "read_failed", "error_detail": err}

    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "total": order["amount_total"],
        "lines": lines_detail,
    }
    _log_call("get_active_quotation", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
    return result


_STATE_LABEL = {
    "draft": "borrador",
    "sent": "enviada",
    "sale": "confirmada",
    "done": "facturada",
    "cancel": "cancelada",
}


def odoo_list_quotations(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
    limit: int = 10,
    states: list[str] | None = None,
) -> dict:
    """List recent sale.order records for a partner — COMPACT format.

    Returns ONLY header + totals + report URLs per order (NO line items).
    Use `get_quotation(order_id)` if the customer asks for the detail of a
    specific quotation.

    Use this when the customer asks "muestrame mis cotizaciones",
    "que cotizaciones tengo", "mis ultimas compras", etc. ALWAYS call
    this — never answer from conversation memory because the model
    hallucinates line items.

    Args:
        partner_id: res.partner ID
        limit: max orders to return (default 10)
        states: filter by sale.order.state. Defaults to all states
                ['draft','sent','sale','done','cancel']. Pass
                ['draft','sent'] to get only editable quotations.

    Returns {success, count, orders: [{order_id, name, state, state_label,
    total, subtotal, date_order, lines_count, report_url{html,pdf}}]}
    """
    started = time.time()
    log_args = {"partner_id": partner_id, "limit": limit, "states": states}

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id"}

    states = states or ["draft", "sent", "sale", "done"]
    domain = [["partner_id", "=", partner_id], ["state", "in", states]]

    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order",
            domain,
            ["id", "name", "state", "amount_total", "amount_untaxed",
             "date_order", "create_date", "order_line", "share_link_so"],
            limit=limit,
            order="create_date desc",
        )
    except Exception as e:
        err = f"Error listando cotizaciones: {e}"
        _log_call("list_quotations", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "search_failed", "error_detail": err}

    if not rows:
        result = {"success": True, "count": 0, "orders": [], "partner_id": partner_id}
        result["display_type"] = "list_data"
        _log_call("list_quotations", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
        return result

    orders_summary = []
    for r in rows:
        line_ids = r.get("order_line", []) or []
        orders_summary.append({
            "order_id": r["id"],
            "name": r["name"],
            "state": r["state"],
            "state_label": _STATE_LABEL.get(r["state"], r["state"]),
            "total": r["amount_total"],
            "subtotal": r["amount_untaxed"],
            "date_order": r.get("date_order") or r.get("create_date"),
            "lines_count": len(line_ids),
            "share_link": r.get("share_link_so") or "",
        })

    result = {
        "success": True,
        "count": len(orders_summary),
        "partner_id": partner_id,
        "orders": orders_summary,
    }
    result["display_type"] = "list_data"
    _log_call("list_quotations", tenant_id, log_args, {"count": len(orders_summary)}, None, int((time.time() - started) * 1000))
    return result


def odoo_get_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
) -> dict:
    """Read full detail (header + ALL lines) of one sale.order by id.

    Use this ONLY when the customer asks for the detail of a specific
    quotation ("muestrame la VENTA120704", "que tiene esa cotizacion").
    For listing recent quotations use `list_quotations` instead — it's
    much cheaper in tokens.

    Returns {success, order_id, name, state, state_label, partner,
    total, subtotal, tax, date_order, report_url, lines: [{product, code,
    quantity, price_unit, discount, subtotal, total}]}
    """
    started = time.time()
    log_args = {"order_id": order_id}

    if not isinstance(order_id, int) or order_id <= 0:
        return {"success": False, "error_code": "invalid_order_id"}

    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["id", "name", "state", "partner_id", "amount_total",
             "amount_untaxed", "amount_tax", "date_order", "create_date",
             "order_line"],
        )
    except Exception as e:
        err = f"Error leyendo cotizacion {order_id}: {e}"
        _log_call("get_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "read_failed", "error_detail": err}

    if not orders:
        return {"success": False, "error_code": "order_not_found", "order_id": order_id}

    order = orders[0]
    line_ids = order.get("order_line", []) or []
    lines_detail = []
    if line_ids:
        try:
            raw_lines = odoo_read(
                tenant_id, url, db, user, password,
                "sale.order.line", line_ids,
                ["product_id", "name", "product_uom_qty", "price_unit",
                 "discount", "price_subtotal", "price_tax", "price_total"],
            )
            for ln in (raw_lines or []):
                pname = ln["product_id"][1] if isinstance(ln.get("product_id"), list) else ln.get("name", "")
                lines_detail.append({
                    "product": pname,
                    "quantity": ln.get("product_uom_qty", 1),
                    "price_unit": ln.get("price_unit", 0),
                    "discount": ln.get("discount", 0),
                    "subtotal": ln.get("price_subtotal", 0),
                    "tax": ln.get("price_tax", 0),
                    "total": ln.get("price_total", 0),
                })
        except Exception as e:
            err = f"Order {order['name']} read OK but lines failed: {e}"
            _log_call("get_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
            return {"success": False, "error_code": "lines_read_failed", "error_detail": err}

    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else ""

    result = {
        "success": True,
        "order_id": order["id"],
        "name": order["name"],
        "state": order["state"],
        "state_label": _STATE_LABEL.get(order["state"], order["state"]),
        "partner": partner_name,
        "subtotal": order["amount_untaxed"],
        "tax": order["amount_tax"],
        "total": order["amount_total"],
        "date_order": order.get("date_order") or order.get("create_date"),
        "lines_count": len(lines_detail),
        "lines": lines_detail,
    }
    result["_card"] = _build_card(result)
    _log_call("get_quotation", tenant_id, log_args, {"name": order["name"], "lines": len(lines_detail)}, None, int((time.time() - started) * 1000))
    return result


def odoo_render_quotation_pdf(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
) -> dict:
    """Download an Odoo sale.order as a PDF file (the official Odoo report).

    Flow:
      1. Authenticate to Odoo via /web/session/authenticate (HTTP, gets a
         session cookie)
      2. GET /report/pdf/sale.report_saleorder/<order_id> with the cookie —
         this is the SAME endpoint Odoo's UI uses to render reports, so we
         get the official PDF with company logo, header, line table, totals,
         taxes, payment terms, etc.
      3. Save the PDF bytes to /files/quotations/<name>.pdf inside this
         container — that path is bind-mounted to /data/agents/tecnosmart/files
         on the host, which Hermes sees as /root/.hermes/files/quotations/
      4. Return the Hermes-side path so the LLM can include it in its
         assistant message verbatim. Hermes' extract_local_files() (patched
         to include .pdf in _LOCAL_MEDIA_EXTS) will detect it and call
         send_document → the customer receives the real PDF as a Telegram
         or WhatsApp document attachment.

    Args:
        order_id: sale.order id (NOT template id)

    Returns {success, order_id, order_name, file_path, size_bytes}
    where file_path is the absolute path AS HERMES SEES IT
    (/root/.hermes/files/quotations/<name>.pdf).
    """
    import os
    import requests as _rq

    started = time.time()
    log_args = {"order_id": order_id}

    if not isinstance(order_id, int) or order_id <= 0:
        return {"success": False, "error_code": "invalid_order_id"}

    # ── 1. Resolve order name (used for filename) ───────────────────────
    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id], ["name"],
        )
    except Exception as e:
        return {"success": False, "error_code": "order_read_failed", "error_detail": str(e)}
    if not rows:
        return {"success": False, "error_code": "order_not_found", "order_id": order_id}
    order_name = rows[0]["name"]

    # ── 2. Authenticate to Odoo via web session to get cookie ──────────
    base_url = url.rstrip("/")
    session = _rq.Session()
    try:
        auth_resp = session.post(
            f"{base_url}/web/session/authenticate",
            json={
                "jsonrpc": "2.0",
                "params": {"db": db, "login": user, "password": password},
            },
            timeout=15,
        )
        auth_resp.raise_for_status()
        auth_json = auth_resp.json()
        if "result" not in auth_json or not auth_json["result"].get("uid"):
            err = f"Auth fallida: {auth_json}"
            _log_call("render_quotation_pdf", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
            return {"success": False, "error_code": "auth_failed", "error_detail": err}
    except Exception as e:
        err = f"Error autenticando a Odoo: {e}"
        _log_call("render_quotation_pdf", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "auth_failed", "error_detail": err}

    # ── 3. GET the report PDF with session cookie ──────────────────────
    pdf_url = f"{base_url}/report/pdf/sale.report_saleorder/{order_id}"
    try:
        pdf_resp = session.get(pdf_url, timeout=30)
        pdf_resp.raise_for_status()
        pdf_bytes = pdf_resp.content
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            err = f"Respuesta no es un PDF valido (len={len(pdf_bytes)})"
            _log_call("render_quotation_pdf", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
            return {"success": False, "error_code": "invalid_pdf_response", "error_detail": err}
    except Exception as e:
        err = f"Error descargando PDF de Odoo: {e}"
        _log_call("render_quotation_pdf", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "pdf_download_failed", "error_detail": err}

    # ── 4. Save the PDF to the shared volume ──────────────────────────
    out_dir_container = "/files/quotations"
    out_dir_hermes = "/root/.hermes/files/quotations"
    try:
        os.makedirs(out_dir_container, exist_ok=True)
    except Exception as e:
        err = f"Error creando directorio de salida: {e}"
        _log_call("render_quotation_pdf", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "mkdir_failed", "error_detail": err}

    safe_name = order_name.replace("/", "_").replace(" ", "_")
    fname = f"{safe_name}.pdf"
    container_path = f"{out_dir_container}/{fname}"
    hermes_path = f"{out_dir_hermes}/{fname}"

    try:
        with open(container_path, "wb") as fh:
            fh.write(pdf_bytes)
    except Exception as e:
        err = f"Error guardando PDF: {e}"
        _log_call("render_quotation_pdf", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "save_failed", "error_detail": err}

    result = {
        "success": True,
        "order_id": order_id,
        "order_name": order_name,
        "file_path": container_path,
        "file_path_hermes": hermes_path,
        "size_bytes": len(pdf_bytes),
    }
    _log_call("render_quotation_pdf", tenant_id, log_args, {"name": order_name, "size": len(pdf_bytes)}, None, int((time.time() - started) * 1000))
    return result


def odoo_send_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    confirmed: bool = False,
    session_active_quotation_id: int | None = None,
) -> dict:
    """Send quotation by email to the customer (action_quotation_send).

    This triggers Odoo's built-in email template for quotations.
    The order state changes from 'draft' to 'sent'.

    IMPORTANT: Sending an email is irreversible. Call first with confirmed=False
    (default) to get a preview. Show the preview to the user. Only call with
    confirmed=True after receiving explicit confirmation.

    Args:
        order_id: sale.order ID to send
        confirmed: False = dry-run preview only; True = execute the send
        session_active_quotation_id: active quotation ID from the user's session
    """
    # Validate that the order_id matches the quotation the user explicitly
    # selected in this session.
    if session_active_quotation_id is None:
        return {
            "success": False,
            "error_code": "no_active_quotation",
            "error_detail": (
                "No hay cotización activa en la sesión del usuario. "
                "Pregunta al usuario qué cotización quiere enviar."
            ),
        }
    if order_id != session_active_quotation_id:
        return {
            "success": False,
            "error_code": "quotation_not_in_session",
            "error_detail": (
                f"La cotización {order_id} no fue seleccionada por el usuario en esta sesión. "
                f"La cotización activa en sesión es {session_active_quotation_id}. "
                "Pregunta al usuario cuál cotización quiere enviar antes de proceder."
            ),
        }

    # Read order info for preview or send
    orders = odoo_read(
        tenant_id, url, db, user, password,
        "sale.order", [order_id],
        ["name", "state", "partner_id", "amount_total"],
    )
    order = orders[0] if orders else {}
    partner_raw = order.get("partner_id")
    partner_name = partner_raw[1] if isinstance(partner_raw, list) else str(partner_raw or "")
    order_name = order.get("name", f"orden {order_id}")

    # Dry-run / preview — return what would be done without sending
    if not confirmed:
        preview_msg = (
            f"Enviaras la cotizacion {order_name} por correo a {partner_name} "
            f"(total: USD {order.get('amount_total', 0):.2f}). "
            "Esta accion es irreversible. "
            "Llama de nuevo con confirmed=true para enviar."
        )
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": preview_msg,
            "action": "send_quotation",
            "order_id": order_id,
            "order_name": order_name,
            "partner": partner_name,
        }

    try:
        # Use action_quotation_send which marks as sent and sends email
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "action_quotation_send", [order_id],
        )
    except Exception:
        # Fallback: try force_quotation_send (Odoo 13 compat)
        try:
            odoo_call_method(
                tenant_id, url, db, user, password,
                "sale.order", "force_quotation_send", [order_id],
            )
        except Exception as e2:
            return {"success": False, "error": f"No se pudo enviar: {e2}"}

    # Re-read state after send
    orders = odoo_read(
        tenant_id, url, db, user, password,
        "sale.order", [order_id],
        ["name", "state", "partner_id"],
    )
    order = orders[0] if orders else {}
    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else partner_name
    return {
        "success": True,
        "order_id": order_id,
        "name": order.get("name"),
        "state": order.get("state"),
        "partner": partner_name,
        "message": f"Cotizacion {order.get('name', '')} enviada por correo a {partner_name}.",
    }


def odoo_confirm_sale_order(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    confirmed: bool = False,
    session_active_quotation_id: int | None = None,
) -> dict:
    """Confirm a draft sale order (quotation → sale order). IRREVERSIBLE.

    IMPORTANT: Confirming a sale order is irreversible — it cannot be reverted
    to draft easily and triggers stock reservations and billing flows.
    Call first with confirmed=False (default) to get a preview.
    Show the preview to the user. Only call with confirmed=True after receiving
    explicit confirmation ('sí', 'confirmo', 'dale').

    Args:
        order_id: sale.order ID to confirm
        confirmed: False = dry-run preview only; True = execute the confirmation
        session_active_quotation_id: active quotation ID from the user's session
    """
    # Validate that the order_id matches the quotation the user explicitly
    # selected in this session.
    if session_active_quotation_id is None:
        return {
            "success": False,
            "error_code": "no_active_quotation",
            "error_detail": (
                "No hay cotización activa en la sesión del usuario. "
                "Pregunta al usuario qué cotización quiere confirmar."
            ),
        }
    if order_id != session_active_quotation_id:
        return {
            "success": False,
            "error_code": "quotation_not_in_session",
            "error_detail": (
                f"La cotización {order_id} no fue seleccionada por el usuario en esta sesión. "
                f"La cotización activa en sesión es {session_active_quotation_id}. "
                "Pregunta al usuario cuál cotización quiere confirmar antes de proceder."
            ),
        }

    # Read order info needed for preview or post-confirm verification
    orders = odoo_read(
        tenant_id, url, db, user, password,
        "sale.order", [order_id],
        ["name", "state", "amount_total", "partner_id"],
    )
    order = orders[0] if orders else {}
    order_name = order.get("name", f"orden {order_id}")
    partner_raw = order.get("partner_id")
    partner_name = partner_raw[1] if isinstance(partner_raw, list) else str(partner_raw or "")

    # Dry-run / preview — return what would be done without confirming
    if not confirmed:
        preview_msg = (
            f"Confirmaras la cotizacion {order_name} de {partner_name} "
            f"(total: USD {order.get('amount_total', 0):.2f}). "
            "Esta accion es IRREVERSIBLE: convierte la proforma en orden de venta. "
            "Llama de nuevo con confirmed=true para confirmar."
        )
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": preview_msg,
            "action": "confirm_quotation",
            "order_id": order_id,
            "order_name": order_name,
            "partner": partner_name,
            "total": order.get("amount_total", 0),
        }

    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "action_confirm", [order_id],
        )
    except Exception as e:
        msg = str(e)
        # Odoo 13 action_confirm returns None; XML-RPC raises marshal error
        # but the order IS confirmed in the DB — treat as success and verify.
        if "cannot marshal None" not in msg and "allow_none" not in msg:
            return {"success": False, "error": msg}

    orders = odoo_read(
        tenant_id, url, db, user, password,
        "sale.order", [order_id],
        ["name", "state", "amount_total"],
    )
    order = orders[0] if orders else {}
    return {
        "success": True,
        "order_id": order_id,
        "name": order.get("name"),
        "state": order.get("state"),
        "total": order.get("amount_total"),
    }


def odoo_search_partner(
    tenant_id: str, url: str, db: str, user: str, password: str,
    vat: str | None = None,
    name: str | None = None,
    phone: str | None = None,
) -> list[dict]:
    """Search for a partner (customer/supplier) by VAT, name, or phone."""
    domain = []
    if vat:
        domain.append(["vat", "=", vat])
    if name:
        domain.append(["name", "ilike", name])
    if phone:
        domain.append("|")
        domain.append(["phone", "=", phone])
        domain.append(["mobile", "=", phone])

    if not domain:
        return []

    return odoo_search(
        tenant_id, url, db, user, password,
        "res.partner", domain,
        fields=["name", "vat", "email", "phone", "mobile",
                "street", "city", "country_id", "customer", "supplier"],
        limit=10,
    )


def odoo_check_balance(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
) -> dict:
    """Check customer balance (accounts receivable).

    Queries account.move.line for unreconciled receivable entries.
    Returns exact amounts — never round or estimate.
    """
    lines = odoo_search(
        tenant_id, url, db, user, password,
        "account.move.line",
        [
            ["partner_id", "=", partner_id],
            ["account_id.user_type_id.type", "=", "receivable"],
            ["full_reconcile_id", "=", False],
            ["parent_state", "=", "posted"],
        ],
        fields=["move_id", "date_maturity", "debit", "credit",
                "amount_residual", "ref"],
        limit=100,
    )

    total_due = sum(line.get("amount_residual", 0) for line in lines)
    overdue = [
        line for line in lines
        if line.get("date_maturity") and str(line["date_maturity"]) < str(__import__("datetime").date.today())
    ]
    total_overdue = sum(line.get("amount_residual", 0) for line in overdue)

    return {
        "partner_id": partner_id,
        "total_due": round(total_due, 2),
        "total_overdue": round(total_overdue, 2),
        "invoices_count": len(lines),
        "overdue_count": len(overdue),
        "details": [
            {
                "invoice": line.get("move_id", [None, ""])[1] if isinstance(line.get("move_id"), list) else "",
                "due_date": str(line.get("date_maturity", "")),
                "amount": round(line.get("amount_residual", 0), 2),
                "ref": line.get("ref", ""),
            }
            for line in lines
        ],
    }


def get_latest_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
    states: list[str] | None = None,
) -> dict:
    """Fetch the most recent quotation for a partner (limit=1, newest first).

    Returns full order detail (same format as get_quotation) plus _card metadata.
    Use when the customer asks for 'mi última proforma', 'la más reciente', etc.
    """
    states = states or ["draft", "sent"]
    # Step 1: get the most recent order_id
    list_result = odoo_list_quotations(
        tenant_id, url, db, user, password,
        partner_id=partner_id, limit=1, states=states,
    )
    if not list_result.get("success") or not list_result.get("orders"):
        return {"success": False, "error_code": "no_quotations",
                "error_detail": "No se encontraron cotizaciones para este cliente."}

    order_id = list_result["orders"][0]["order_id"]
    # Step 2: get full detail
    detail = odoo_get_quotation(
        tenant_id, url, db, user, password, order_id=order_id,
    )
    return detail  # already has _card from odoo_get_quotation
