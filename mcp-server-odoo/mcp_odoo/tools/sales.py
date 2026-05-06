"""Sales tools — quotations, sale orders, customer queries."""

import logging
import time
import traceback
from typing import Any

from mcp_odoo.tools.generic import odoo_search, odoo_read, odoo_create, odoo_write, odoo_call_method

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
    # Normalize: accept "product_id" or "template_id" interchangeably.
    normalized_lines = []
    for line in lines:
        pid = line.get("product_id") or line.get("template_id")
        if pid:
            normalized_lines.append({**line, "product_id": pid})
    if not normalized_lines:
        err = "Ninguna línea tiene product_id válido. Pasa product_id (template_id numérico) por línea."
        _log_call("create_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "no_valid_product_ids", "error_detail": err}
    lines = normalized_lines
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

    # Dry-runs (confirmed=False) pass through so the LLM can show a preview
    # and the user can decide.
    #
    # Actual writes (confirmed=True) gate:
    # - If the orchestrator already promoted a quotation to active in the
    #   user's session (X-Active-Quotation-Id header), the order_id MUST
    #   match — that is the strict path that prevents the LLM from
    #   hopping between quotations the user did not pick.
    # - If the session has NO active quotation (header missing — happens
    #   on the first turn after /new when get_latest_quotation and
    #   add_to_quotation run in the same batch and the orchestrator
    #   couldn't reload the MCP client mid-turn), we still allow the
    #   write because the editability check below already rejects any
    #   order that isn't in draft/sent state and tenant scoping is
    #   enforced at the connection level by tenant_id. The risk surface
    #   is "modify another customer's draft order in the same tenant",
    #   which is bounded by the LLM only seeing orders it pulled via
    #   tenant-scoped tools.
    if confirmed and session_active_quotation_id is not None:
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
    # Normalize: accept "product_id" or "template_id" interchangeably.
    add_normalized = [{**ln, "product_id": ln.get("product_id") or ln.get("template_id")}
                      for ln in lines if ln.get("product_id") or ln.get("template_id")]
    if add_normalized:
        lines = add_normalized
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

    # Read existing lines so we can MERGE quantities into the matching
    # variant rather than create duplicate lines for the same product.
    # User intent "agregar 2 unidades más al mouse" must increment the
    # existing line, not append a second line with qty=2 of the same SKU.
    # We only merge when price_unit and discount are not overridden — if
    # the caller passed a custom price/discount the safest choice is a new
    # line so the original record stays untouched.
    try:
        existing_lines = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order.line",
            [["order_id", "=", order_id]],
            ["id", "product_id", "product_uom_qty", "price_unit", "discount"],
        )
    except Exception as e:
        # Non-fatal: fall back to the original "always create" behaviour.
        logger.warning("add_to_quotation: could not read existing lines, will create new: %s", e)
        existing_lines = []

    qty_by_variant: dict[int, dict] = {}
    for el in existing_lines or []:
        pid = el.get("product_id")
        if isinstance(pid, list) and pid:
            pid = pid[0]
        if isinstance(pid, int):
            qty_by_variant[pid] = el  # last write wins; merging into the most recent

    increments: list[tuple[int, float]] = []  # (line_id, new_qty)
    new_line_cmds = []
    for line in lines:
        tmpl_id = line["product_id"]
        variant_pid = variant_by_template[tmpl_id]
        qty_to_add = float(line.get("quantity", 1) or 1)
        has_overrides = "price_unit" in line or "discount" in line
        existing = qty_by_variant.get(variant_pid)
        if existing and not has_overrides:
            # Merge: same product already on the order — bump its qty.
            new_qty = float(existing.get("product_uom_qty") or 0) + qty_to_add
            increments.append((int(existing["id"]), new_qty))
            continue
        line_vals = {
            "order_id": order_id,
            "product_id": variant_pid,
            "product_uom_qty": qty_to_add,
            "product_uom": uom_by_variant.get(variant_pid, 1),
        }
        if "price_unit" in line:
            line_vals["price_unit"] = line["price_unit"]
        if "discount" in line:
            line_vals["discount"] = line["discount"]
        new_line_cmds.append(line_vals)

    # Apply increments first, then create the genuinely new lines.
    try:
        for line_id, new_qty in increments:
            odoo_write(
                tenant_id, url, db, user, password,
                "sale.order.line", [line_id], {"product_uom_qty": new_qty},
            )
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


def odoo_find_quotation_by_name(
    tenant_id: str, url: str, db: str, user: str, password: str,
    name: str,
) -> dict:
    """Resolve an Odoo sale.order by its human-readable ``name``.

    The LLM gets the ``name`` (e.g. "VENTA122172", "S0001234") from the
    user, but every mutation tool (update/remove_line, transition, etc.)
    requires the numeric primary key (``order_id``). This tool does the
    one-shot translation so the LLM never has to guess.

    Returns
    -------
    {
      "success": True,
      "order_id": <int>,
      "name": <str>,
      "state": <str>,
      "partner": <str>,
      "amount_total": <float>,
      "lines_count": <int>,
    }
    or
    {"success": False, "error_code": "not_found"|"ambiguous", ...}
    """
    started = time.time()
    log_args = {"name": name}
    if not name or not isinstance(name, str):
        return {"success": False, "error_code": "invalid_name",
                "error_detail": "name requerido y debe ser texto"}
    name_clean = name.strip()
    if not name_clean:
        return {"success": False, "error_code": "invalid_name",
                "error_detail": "name no puede estar vacio"}
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order", [["name", "=", name_clean]],
            ["id", "name", "state", "partner_id", "amount_total", "order_line"],
            limit=2,
        )
    except Exception as e:
        err = f"Error buscando sale.order por name={name_clean!r}: {e}"
        _log_call("find_quotation_by_name", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "search_failed",
                "error_detail": err}

    if not rows:
        # Fallback: case-insensitive exact match (handles 'venta122172'
        # vs 'VENTA122172'). Plain Odoo `=` is case-sensitive on PG.
        try:
            rows = odoo_search(
                tenant_id, url, db, user, password,
                "sale.order", [["name", "=ilike", name_clean]],
                ["id", "name", "state", "partner_id", "amount_total", "order_line"],
                limit=2,
            )
        except Exception:
            rows = []
    if not rows:
        result = {
            "success": False,
            "error_code": "not_found",
            "error_detail": f"No existe sale.order con name={name_clean!r}.",
            "name": name_clean,
        }
        _log_call(
            "find_quotation_by_name", tenant_id, log_args, result, None,
            int((time.time() - started) * 1000),
        )
        return result
    if len(rows) > 1:
        result = {
            "success": False,
            "error_code": "ambiguous",
            "error_detail": (
                f"Hay {len(rows)} cotizaciones con name parecido a "
                f"{name_clean!r}. Pregunta al usuario por el name exacto "
                f"(con mayusculas y prefijo VENTA/S0/SO)."
            ),
            "candidates": [
                {"order_id": r["id"], "name": r.get("name")}
                for r in rows
            ],
        }
        _log_call(
            "find_quotation_by_name", tenant_id, log_args, result, None,
            int((time.time() - started) * 1000),
        )
        return result

    row = rows[0]
    partner = row.get("partner_id")
    partner_name = (
        partner[1] if isinstance(partner, list) and len(partner) >= 2 else ""
    )
    lines = row.get("order_line") or []
    result = {
        "success": True,
        "order_id": int(row["id"]),
        "name": row.get("name"),
        "state": row.get("state"),
        "partner": partner_name,
        "amount_total": float(row.get("amount_total", 0) or 0),
        "lines_count": len(lines) if isinstance(lines, list) else 0,
    }
    _log_call(
        "find_quotation_by_name", tenant_id, log_args, result, None,
        int((time.time() - started) * 1000),
    )
    return result


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


# ---------------------------------------------------------------------------
# Order edition tools — sprint "C" (full coverage, agosto 2026)
# ---------------------------------------------------------------------------
#
# Diseño común:
#   * Toda función valida el estado de la sale.order ANTES de escribir y
#     devuelve {success: False, error_code: 'order_not_editable', state: ...}
#     cuando la operación no aplica para el estado actual.
#   * confirmed=False emite un preview (read-only); confirmed=True ejecuta.
#   * Devuelven un diff explícito con old/new y los totales recomputados.
#   * No tocan partner_id, payment_term_id ni pricelist_id sin propagar
#     el resto de campos derivados — vide odoo_change_quotation_customer.
#
# Restricciones de estado por defecto:
#   - Cabecera mutable:   draft, sent, waiting_approval, approved
#   - Cabecera light:     sale (algunos campos)
#   - Líneas mutables:    draft, sent, approved, sale (Odoo lo permite con
#                         registro automático en chatter)
#   - Líneas eliminables: solo draft/sent (en sale el override Tecnosmart
#                         bloquea unlink — caemos a qty=0).
#   - NUNCA en done/cancel/collection/rejected.

_STATES_HEADER_FULL = {"draft", "sent", "waiting_approval", "approved"}
_STATES_HEADER_LIGHT = {"draft", "sent", "waiting_approval", "approved", "sale"}
_STATES_LINE_EDITABLE = {"draft", "sent", "approved", "sale"}
_STATES_LINE_HARD_DELETE = {"draft", "sent"}


def _read_sale_order(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    fields: list[str],
) -> dict | None:
    """Helper: read a single sale.order row or return None when absent."""
    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id], fields,
        )
    except Exception as e:
        logger.warning("sale.order read failed for %s: %s", order_id, e)
        return None
    return rows[0] if rows else None


def _read_sale_order_line(
    tenant_id: str, url: str, db: str, user: str, password: str,
    line_id: int,
    fields: list[str],
) -> dict | None:
    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order.line", [line_id], fields,
        )
    except Exception as e:
        logger.warning("sale.order.line read failed for %s: %s", line_id, e)
        return None
    return rows[0] if rows else None


def _state_error(order: dict, allowed: set[str], action: str) -> dict:
    return {
        "success": False,
        "error_code": "order_not_editable",
        "error_detail": (
            f"La cotización {order.get('name', '?')} está en estado "
            f"'{order.get('state', '?')}' y no acepta '{action}'. "
            f"Estados permitidos: {sorted(allowed)}."
        ),
        "order_id": order.get("id"),
        "name": order.get("name"),
        "state": order.get("state"),
    }


def _ok_diff(
    order_id: int, name: str, old: dict, new: dict, recomputed: dict,
) -> dict:
    return {
        "success": True,
        "order_id": order_id,
        "name": name,
        "old": old,
        "new": new,
        "recomputed": recomputed,
    }


def _recompute_summary(
    tenant_id: str, url: str, db: str, user: str, password: str, order_id: int,
) -> dict:
    """Read amount fields after a write to surface them in the response."""
    fields = [
        "name", "state", "amount_untaxed", "amount_tax", "amount_total",
    ]
    o = _read_sale_order(tenant_id, url, db, user, password, order_id, fields) or {}
    return {
        "amount_untaxed": o.get("amount_untaxed", 0),
        "amount_tax": o.get("amount_tax", 0),
        "amount_total": o.get("amount_total", 0),
        "state": o.get("state"),
    }


def _card_for_order(
    tenant_id: str, url: str, db: str, user: str, password: str, order_id: int,
) -> dict | None:
    """Build a `_card` envelope for an existing order_id.

    Used by mutation tools so Telegram/WhatsApp render the OrderCard
    automatically after every modification. Single read of sale.order
    with the fields _build_card consumes.
    """
    fields = [
        "name", "state", "partner_id", "amount_total", "order_line",
    ]
    o = _read_sale_order(tenant_id, url, db, user, password, order_id, fields)
    if not o:
        return None
    partner = o.get("partner_id")
    if isinstance(partner, list) and len(partner) >= 2:
        partner_name = partner[1]
    else:
        partner_name = ""
    lines = o.get("order_line") or []
    state = o.get("state") or ""
    state_label = _STATE_LABEL.get(state, state) if "_STATE_LABEL" in globals() else state
    return {
        "order_id": order_id,
        "order_name": o.get("name", ""),
        "partner_name": partner_name,
        "state_label": state_label,
        "total": float(o.get("amount_total", 0) or 0),
        "lines_count": len(lines) if isinstance(lines, list) else 0,
    }


# ---- 1) update_quotation_line ---------------------------------------------

def odoo_update_quotation_line(
    tenant_id: str, url: str, db: str, user: str, password: str,
    line_id: int,
    *,
    quantity: float | None = None,
    price_unit: float | None = None,
    discount: float | None = None,
    name: str | None = None,
    product_id: int | None = None,
    confirmed: bool = False,
) -> dict:
    """Modify an existing sale.order.line.

    Cambia ``product_uom_qty``, ``price_unit``, ``discount``, ``name`` o
    ``product_id`` de una línea ya existente. Para eliminar una línea
    úsa ``odoo_remove_quotation_line`` en lugar de ``quantity=0`` aquí
    (esa lógica vive en remove para mantener separados los contratos).

    El descuento queda topado por ``partner_max_sale_discount`` cuando
    Tecnosmart tiene ese flag activo. ``product_id`` requiere reenviar
    también ``name`` y ``price_unit`` ideal — Odoo no dispara el onchange
    desde la API, así que el caller asume responsabilidad si los omite.
    """
    started = time.time()
    log_args = {
        "line_id": line_id, "quantity": quantity, "price_unit": price_unit,
        "discount": discount, "product_id": product_id, "confirmed": confirmed,
    }

    if not isinstance(line_id, int) or line_id <= 0:
        err = "line_id requerido y debe ser entero positivo"
        _log_call("update_quotation_line", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_line_id", "error_detail": err}

    if quantity is None and price_unit is None and discount is None and name is None and product_id is None:
        err = "Debes especificar al menos un campo a actualizar"
        _log_call("update_quotation_line", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "no_changes", "error_detail": err}

    line = _read_sale_order_line(
        tenant_id, url, db, user, password, line_id,
        ["id", "order_id", "product_id", "product_uom_qty", "price_unit",
         "discount", "name"],
    )
    if not line:
        err = f"sale.order.line {line_id} no existe"
        _log_call("update_quotation_line", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "line_not_found", "error_detail": err}

    order_id = line["order_id"][0] if isinstance(line.get("order_id"), list) else line.get("order_id")
    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state", "partner_id"],
    )
    if not order:
        err = f"sale.order de la línea no existe"
        _log_call("update_quotation_line", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "order_not_found", "error_detail": err}

    if order["state"] not in _STATES_LINE_EDITABLE:
        result = _state_error(order, _STATES_LINE_EDITABLE, "update_line")
        _log_call("update_quotation_line", tenant_id, log_args, result, None,
                  int((time.time() - started) * 1000))
        return result

    # Validate discount against partner max if available.
    if discount is not None:
        if discount < 0 or discount > 100:
            return {
                "success": False,
                "error_code": "discount_out_of_range",
                "error_detail": f"discount debe estar entre 0 y 100, recibido {discount}",
            }
        try:
            partner_id = order["partner_id"][0] if isinstance(order.get("partner_id"), list) else order.get("partner_id")
            if partner_id:
                p = odoo_read(
                    tenant_id, url, db, user, password,
                    "res.partner", [partner_id], ["partner_max_sale_discount"],
                )
                if p:
                    cap = p[0].get("partner_max_sale_discount") or 0
                    if cap and discount > cap:
                        return {
                            "success": False,
                            "error_code": "discount_exceeds_partner_cap",
                            "error_detail": (
                                f"El descuento solicitado ({discount}%) supera el "
                                f"máximo permitido para este cliente ({cap}%)."
                            ),
                            "partner_max": cap,
                        }
        except Exception as e:
            # Field may not exist on this tenant — log and proceed.
            logger.debug("partner_max_sale_discount lookup skipped: %s", e)

    # Build write dict.
    vals: dict = {}
    if quantity is not None:
        vals["product_uom_qty"] = float(quantity)
    if price_unit is not None:
        vals["price_unit"] = float(price_unit)
    if discount is not None:
        vals["discount"] = float(discount)
    if name is not None:
        vals["name"] = name
    if product_id is not None:
        # Resolve product.template -> product.product variant.
        try:
            variants = odoo_search(
                tenant_id, url, db, user, password,
                "product.product",
                [["product_tmpl_id", "=", int(product_id)], ["active", "=", True]],
                ["id", "uom_id"], limit=1,
            )
            if not variants:
                return {
                    "success": False,
                    "error_code": "template_no_variant",
                    "error_detail": f"product.template {product_id} sin variante activa",
                }
            vals["product_id"] = variants[0]["id"]
            vals["product_uom"] = (
                variants[0]["uom_id"][0] if isinstance(variants[0].get("uom_id"), list)
                else variants[0].get("uom_id") or 1
            )
        except Exception as e:
            return {
                "success": False,
                "error_code": "variant_lookup_failed",
                "error_detail": str(e),
            }

    old = {
        "product_uom_qty": line.get("product_uom_qty"),
        "price_unit": line.get("price_unit"),
        "discount": line.get("discount"),
        "name": line.get("name"),
    }

    if not confirmed:
        result = {
            "success": False,
            "requires_confirmation": True,
            "preview": {
                "line_id": line_id,
                "order_name": order["name"],
                "old": old,
                "proposed": vals,
            },
            "action": "update_quotation_line",
        }
        _log_call("update_quotation_line", tenant_id, log_args, result, None,
                  int((time.time() - started) * 1000))
        return result

    try:
        odoo_write(tenant_id, url, db, user, password,
                   "sale.order.line", [line_id], vals)
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("update_quotation_line write failed line=%s err=%s\n%s", line_id, e, tb)
        _log_call("update_quotation_line", tenant_id, log_args, None, str(e), elapsed)
        return {"success": False, "error_code": "write_failed", "error_detail": str(e)}

    # Re-read to capture computed price_subtotal.
    new_line = _read_sale_order_line(
        tenant_id, url, db, user, password, line_id,
        ["product_uom_qty", "price_unit", "discount", "name", "price_subtotal", "price_total"],
    ) or {}

    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    result = _ok_diff(order_id, order["name"], old, new_line, summary)
    _log_call("update_quotation_line", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


# ---- 2) remove_quotation_line ---------------------------------------------

def odoo_remove_quotation_line(
    tenant_id: str, url: str, db: str, user: str, password: str,
    line_id: int,
    *,
    mode: str = "auto",
    confirmed: bool = False,
) -> dict:
    """Remove a line from an existing sale.order.

    ``mode='auto'`` (default): unlink físico cuando el estado es
    draft/sent; en sale/approved cae a ``write({'product_uom_qty': 0})``
    para no chocar con el override `_check_line_unlink` del módulo
    `l10n_ec_sri` que bloquea unlink si la orden ya tiene factura.

    ``mode='unlink'`` fuerza unlink y deja que Odoo decida (lanza
    UserError si está bloqueado). ``mode='qty_zero'`` solo setea qty=0.
    """
    started = time.time()
    log_args = {"line_id": line_id, "mode": mode, "confirmed": confirmed}

    if not isinstance(line_id, int) or line_id <= 0:
        err = "line_id requerido y debe ser entero positivo"
        _log_call("remove_quotation_line", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_line_id", "error_detail": err}

    if mode not in ("auto", "unlink", "qty_zero"):
        err = f"mode debe ser auto/unlink/qty_zero (recibido: {mode!r})"
        return {"success": False, "error_code": "invalid_mode", "error_detail": err}

    line = _read_sale_order_line(
        tenant_id, url, db, user, password, line_id,
        ["id", "order_id", "product_id", "product_uom_qty", "name"],
    )
    if not line:
        err = f"sale.order.line {line_id} no existe"
        return {"success": False, "error_code": "line_not_found", "error_detail": err}

    order_id = line["order_id"][0] if isinstance(line.get("order_id"), list) else line.get("order_id")
    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state"],
    )
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": "sale.order de la línea no existe"}

    if order["state"] not in _STATES_LINE_EDITABLE:
        return _state_error(order, _STATES_LINE_EDITABLE, "remove_line")

    use_unlink = (
        mode == "unlink"
        or (mode == "auto" and order["state"] in _STATES_LINE_HARD_DELETE)
    )

    if not confirmed:
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": {
                "line_id": line_id,
                "order_name": order["name"],
                "approach": "unlink" if use_unlink else "qty_zero",
                "current_qty": line.get("product_uom_qty"),
            },
            "action": "remove_quotation_line",
        }

    try:
        if use_unlink:
            odoo_call_method(
                tenant_id, url, db, user, password,
                "sale.order.line", "unlink", [line_id],
            )
        else:
            odoo_write(tenant_id, url, db, user, password,
                       "sale.order.line", [line_id], {"product_uom_qty": 0})
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("remove_quotation_line failed line=%s mode=%s err=%s\n%s",
                     line_id, mode, e, tb)
        _log_call("remove_quotation_line", tenant_id, log_args, None, str(e), elapsed)
        return {"success": False, "error_code": "remove_failed", "error_detail": str(e)}

    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "approach": "unlink" if use_unlink else "qty_zero",
        "removed_line_id": line_id,
        "recomputed": summary,
    }
    _log_call("remove_quotation_line", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


# ---- 3) change_quotation_customer -----------------------------------------

def odoo_change_quotation_customer(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    partner_id: int,
    *,
    propagate_pricelist: bool = True,
    propagate_payment_term: bool = True,
    propagate_addresses: bool = True,
    reprice_lines: bool = False,
    confirmed: bool = False,
) -> dict:
    """Reasignar el cliente de una cotización.

    Odoo no dispara `_onchange_partner_id` cuando se llama a `write`
    desde la API, así que `pricelist_id`, `payment_term_id`,
    `partner_invoice_id`, `partner_shipping_id` y `fiscal_position_id`
    quedan stale. Este helper LEE los `property_*` del nuevo partner
    y los propaga en un solo `write`.

    ``reprice_lines=True`` recalcula `price_unit` de cada línea usando
    `product.template._get_partner_pricelist` — útil cuando se cambia
    de cliente con tarifa diferente.
    """
    started = time.time()
    log_args = {
        "order_id": order_id, "partner_id": partner_id,
        "propagate_pricelist": propagate_pricelist,
        "propagate_payment_term": propagate_payment_term,
        "propagate_addresses": propagate_addresses,
        "reprice_lines": reprice_lines, "confirmed": confirmed,
    }

    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state", "partner_id", "pricelist_id",
         "payment_term_id", "partner_invoice_id", "partner_shipping_id"],
    )
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"sale.order {order_id} no existe"}

    # Allow customer change in draft/sent only — once confirmed (sale)
    # or post-confirmation (collection/done/cancel) it has accounting
    # impact (facturas linked) and must NOT be silently rewritten.
    allowed = {"draft", "sent"}
    if order["state"] not in allowed:
        return _state_error(order, allowed, "change_partner")

    # Read the new partner's defaults.
    p = odoo_read(
        tenant_id, url, db, user, password,
        "res.partner", [partner_id],
        ["id", "name", "property_product_pricelist",
         "property_payment_term_id"],
    )
    if not p:
        return {"success": False, "error_code": "partner_not_found",
                "error_detail": f"res.partner {partner_id} no existe"}
    new_partner = p[0]

    # Resolve invoice + shipping addresses via address_get.
    addr_ids: dict = {}
    if propagate_addresses:
        try:
            addr_ids = odoo_call_method(
                tenant_id, url, db, user, password,
                "res.partner", "address_get", [partner_id], [["delivery", "invoice"]],
            ) or {}
        except Exception as e:
            logger.debug("address_get skipped: %s", e)
            addr_ids = {}

    vals: dict = {"partner_id": partner_id}
    if propagate_pricelist:
        pl = new_partner.get("property_product_pricelist")
        if isinstance(pl, list) and pl:
            vals["pricelist_id"] = pl[0]
    if propagate_payment_term:
        pt = new_partner.get("property_payment_term_id")
        if isinstance(pt, list) and pt:
            vals["payment_term_id"] = pt[0]
    if propagate_addresses:
        if addr_ids.get("invoice"):
            vals["partner_invoice_id"] = addr_ids["invoice"]
        if addr_ids.get("delivery"):
            vals["partner_shipping_id"] = addr_ids["delivery"]

    old = {
        "partner_id": order.get("partner_id"),
        "pricelist_id": order.get("pricelist_id"),
        "payment_term_id": order.get("payment_term_id"),
        "partner_invoice_id": order.get("partner_invoice_id"),
        "partner_shipping_id": order.get("partner_shipping_id"),
    }

    if not confirmed:
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": {
                "order_name": order["name"],
                "old": old,
                "proposed": vals,
                "reprice_lines": reprice_lines,
            },
            "action": "change_quotation_customer",
        }

    try:
        odoo_write(tenant_id, url, db, user, password,
                   "sale.order", [order_id], vals)
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("change_customer write failed order=%s err=%s\n%s",
                     order_id, e, tb)
        _log_call("change_quotation_customer", tenant_id, log_args, None, str(e), elapsed)
        return {"success": False, "error_code": "write_failed", "error_detail": str(e)}

    # Optional: reprice lines via pricelist.get_product_price.
    repriced_lines: list = []
    if reprice_lines and propagate_pricelist and "pricelist_id" in vals:
        try:
            line_rows = odoo_search(
                tenant_id, url, db, user, password,
                "sale.order.line",
                [["order_id", "=", order_id]],
                ["id", "product_id", "product_uom_qty", "price_unit"],
            )
            for ln in line_rows or []:
                pid = ln["product_id"][0] if isinstance(ln.get("product_id"), list) else ln.get("product_id")
                if not pid:
                    continue
                try:
                    new_price = odoo_call_method(
                        tenant_id, url, db, user, password,
                        "product.pricelist", "get_product_price",
                        [vals["pricelist_id"]],
                        [pid, ln.get("product_uom_qty") or 1, partner_id],
                    )
                    if new_price and abs(float(new_price) - float(ln.get("price_unit") or 0)) > 0.001:
                        odoo_write(tenant_id, url, db, user, password,
                                   "sale.order.line", [ln["id"]],
                                   {"price_unit": float(new_price)})
                        repriced_lines.append({
                            "line_id": ln["id"], "old": ln.get("price_unit"),
                            "new": float(new_price),
                        })
                except Exception as e:
                    logger.debug("reprice line %s skipped: %s", ln.get("id"), e)
        except Exception as e:
            logger.warning("reprice_lines failed for order %s: %s", order_id, e)

    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "old": old,
        "new": vals,
        "repriced_lines": repriced_lines,
        "recomputed": summary,
    }
    _log_call("change_quotation_customer", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


# ---- 4) apply_global_discount ---------------------------------------------

def odoo_apply_global_discount(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    discount_type: str,
    discount_rate: float,
    *,
    confirmed: bool = False,
) -> dict:
    """Aplicar un descuento a TODA la cotización.

    Tecnosmart implementa esto via `discount_type` + `discount_rate` y
    luego `calculate_discount()` que propaga a las líneas. ``percent``
    aplica un % parejo, ``amount`` un monto fijo distribuido y ``cost``
    pone un margen sobre costo.
    """
    started = time.time()
    log_args = {"order_id": order_id, "discount_type": discount_type,
                "discount_rate": discount_rate, "confirmed": confirmed}

    if discount_type not in ("percent", "amount", "cost"):
        return {"success": False, "error_code": "invalid_discount_type",
                "error_detail": f"discount_type debe ser percent/amount/cost (recibido: {discount_type!r})"}
    if discount_type == "percent" and (discount_rate < 0 or discount_rate > 100):
        return {"success": False, "error_code": "discount_out_of_range",
                "error_detail": f"discount_rate debe estar entre 0 y 100 para type=percent"}

    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state", "partner_id", "amount_untaxed"],
    )
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"sale.order {order_id} no existe"}

    allowed = _STATES_HEADER_FULL
    if order["state"] not in allowed:
        return _state_error(order, allowed, "apply_global_discount")

    # Validate against partner cap when type=percent.
    if discount_type == "percent":
        try:
            partner_id = order["partner_id"][0] if isinstance(order.get("partner_id"), list) else order.get("partner_id")
            if partner_id:
                p = odoo_read(
                    tenant_id, url, db, user, password,
                    "res.partner", [partner_id], ["partner_max_sale_discount"],
                )
                if p:
                    cap = p[0].get("partner_max_sale_discount") or 0
                    if cap and discount_rate > cap:
                        return {"success": False,
                                "error_code": "discount_exceeds_partner_cap",
                                "error_detail": (
                                    f"Descuento {discount_rate}% supera el "
                                    f"máximo del cliente ({cap}%)."
                                ),
                                "partner_max": cap}
        except Exception as e:
            logger.debug("partner cap lookup skipped: %s", e)

    if not confirmed:
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": {
                "order_name": order["name"],
                "current_subtotal": order.get("amount_untaxed"),
                "discount_type": discount_type,
                "discount_rate": discount_rate,
            },
            "action": "apply_global_discount",
        }

    try:
        odoo_write(tenant_id, url, db, user, password,
                   "sale.order", [order_id],
                   {"discount_type": discount_type, "discount_rate": float(discount_rate)})
        odoo_call_method(tenant_id, url, db, user, password,
                         "sale.order", "calculate_discount", [order_id])
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("apply_global_discount failed order=%s err=%s\n%s", order_id, e, tb)
        _log_call("apply_global_discount", tenant_id, log_args, None, str(e), elapsed)
        return {"success": False, "error_code": "discount_failed", "error_detail": str(e)}

    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    # Read the discount-related fields after the call.
    after = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["amount_discount"],
    ) or {}
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "discount_type": discount_type,
        "discount_rate": discount_rate,
        "amount_discount": after.get("amount_discount", 0),
        "recomputed": summary,
    }
    _log_call("apply_global_discount", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


# ---- 5) set_quotation_header ----------------------------------------------

_HEADER_FIELDS = {
    "date_order": str,
    "validity_date": str,
    "payment_term_id": int,
    "pricelist_id": int,
    "user_id": int,
    "note": str,
    "client_order_ref": str,
    "invoice_date": str,
}


def odoo_set_quotation_header(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    *,
    confirmed: bool = False,
    **fields: Any,
) -> dict:
    """Update header fields on a sale.order.

    Acepta kwargs con cualquiera de: date_order, validity_date,
    payment_term_id, pricelist_id, user_id, note, client_order_ref,
    invoice_date. Valida estado y devuelve diff + recompute.

    NOTE: cambiar `pricelist_id` aquí NO recalcula precios de líneas
    existentes — usá `change_quotation_customer(reprice_lines=True)`
    si necesitas re-precificar al cambiar tarifa.
    """
    started = time.time()

    # Filter known fields and drop any unrelated kwargs.
    write_vals = {k: v for k, v in fields.items() if k in _HEADER_FIELDS and v is not None}
    log_args = {"order_id": order_id, "fields": list(write_vals.keys()),
                "confirmed": confirmed}

    if not write_vals:
        return {"success": False, "error_code": "no_changes",
                "error_detail": (
                    "No se especificaron campos válidos. Acepta: "
                    + ", ".join(_HEADER_FIELDS.keys())
                )}

    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state"] + list(_HEADER_FIELDS.keys()),
    )
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"sale.order {order_id} no existe"}

    allowed = _STATES_HEADER_FULL
    if order["state"] not in allowed:
        return _state_error(order, allowed, "set_header")

    old = {k: order.get(k) for k in write_vals.keys()}

    if not confirmed:
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": {
                "order_name": order["name"],
                "old": old,
                "proposed": write_vals,
            },
            "action": "set_quotation_header",
        }

    try:
        odoo_write(tenant_id, url, db, user, password,
                   "sale.order", [order_id], write_vals)
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("set_quotation_header failed order=%s err=%s\n%s",
                     order_id, e, tb)
        _log_call("set_quotation_header", tenant_id, log_args, None, str(e), elapsed)
        return {"success": False, "error_code": "write_failed",
                "error_detail": str(e)}

    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "old": old,
        "new": write_vals,
        "recomputed": summary,
    }
    _log_call("set_quotation_header", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


# ---- 6) add_quotation_line (mejorado: una sola línea, sin batching) ------

def odoo_add_quotation_line(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    product_id: int,
    quantity: float = 1.0,
    *,
    price_unit: float | None = None,
    discount: float | None = None,
    name: str | None = None,
    confirmed: bool = False,
) -> dict:
    """Agregar UNA sola línea a una cotización existente.

    Mejor que ``add_to_quotation`` cuando solo es 1 producto — no hace
    merge con líneas existentes (eso es un side-effect de batching que
    confunde al LLM). Si quieres mergear, llama esta tool con qty
    delta y luego `update_quotation_line` para consolidar; o usa
    add_to_quotation con explicit merge.
    """
    started = time.time()
    log_args = {"order_id": order_id, "product_id": product_id,
                "quantity": quantity, "confirmed": confirmed}

    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state"],
    )
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"sale.order {order_id} no existe"}

    if order["state"] not in _STATES_LINE_EDITABLE:
        return _state_error(order, _STATES_LINE_EDITABLE, "add_line")

    # Resolve template -> variant.
    try:
        variants = odoo_search(
            tenant_id, url, db, user, password,
            "product.product",
            [["product_tmpl_id", "=", int(product_id)], ["active", "=", True]],
            ["id", "uom_id", "name", "lst_price"], limit=1,
        )
    except Exception as e:
        return {"success": False, "error_code": "variant_lookup_failed",
                "error_detail": str(e)}
    if not variants:
        return {"success": False, "error_code": "template_no_variant",
                "error_detail": f"product.template {product_id} sin variante activa"}

    v = variants[0]
    line_vals = {
        "order_id": order_id,
        "product_id": v["id"],
        "product_uom_qty": float(quantity),
        "product_uom": v["uom_id"][0] if isinstance(v.get("uom_id"), list) else v.get("uom_id") or 1,
    }
    if price_unit is not None:
        line_vals["price_unit"] = float(price_unit)
    if discount is not None:
        line_vals["discount"] = float(discount)
    if name is not None:
        line_vals["name"] = name

    if not confirmed:
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": {
                "order_name": order["name"],
                "product_id": product_id,
                "product_name": v.get("name"),
                "default_price": v.get("lst_price"),
                "proposed": line_vals,
            },
            "action": "add_quotation_line",
        }

    try:
        new_line_id = odoo_create(
            tenant_id, url, db, user, password,
            "sale.order.line", line_vals,
        )
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("add_quotation_line failed order=%s err=%s\n%s",
                     order_id, e, tb)
        _log_call("add_quotation_line", tenant_id, log_args, None, str(e), elapsed)
        return {"success": False, "error_code": "create_failed",
                "error_detail": str(e)}

    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "new_line_id": new_line_id,
        "recomputed": summary,
    }
    _log_call("add_quotation_line", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


# ---- 7) recalculate_quotation ---------------------------------------------

def odoo_recalculate_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
) -> dict:
    """Forzar recalcular totales (`action_recalculate`) — útil tras
    cambios masivos cuando se sospecha que ``amount_total`` quedó
    desincronizado del subtotal de líneas."""
    started = time.time()
    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state"],
    )
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"sale.order {order_id} no existe"}

    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "action_recalculate", [order_id],
        )
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        logger.error("recalculate_quotation failed order=%s err=%s", order_id, e)
        _log_call("recalculate_quotation", tenant_id, {"order_id": order_id},
                  None, str(e), elapsed)
        # Fallback: read+write triggers _amount_all if action_recalculate
        # is not exposed on this Odoo build.
        try:
            odoo_write(tenant_id, url, db, user, password,
                       "sale.order", [order_id], {})
        except Exception as e2:
            return {"success": False, "error_code": "recalc_failed",
                    "error_detail": f"{e}; fallback: {e2}"}

    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    return {"success": True, "order_id": order_id,
            "name": order["name"], "recomputed": summary}


# ---- 8) get_quotation_state_summary ---------------------------------------

def odoo_get_quotation_state_summary(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
) -> dict:
    """Devolver un resumen del estado para que el LLM razone antes de
    modificar: estado, totales, lineas con flags de editabilidad,
    facturas vinculadas, pickings."""
    started = time.time()
    fields = [
        "id", "name", "state", "amount_untaxed", "amount_tax", "amount_total",
        "partner_id", "user_id", "date_order", "validity_date",
        "invoice_ids", "picking_ids",
    ]
    # Tecnosmart custom — read defensively (some fields may not exist).
    custom = ["state_ec", "collection_state", "estado_despacho", "amount_discount",
              "discount_type", "discount_rate", "is_cash_sale"]
    try:
        order = (odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id], fields + custom,
        ) or [None])[0]
    except Exception:
        order = (odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id], fields,
        ) or [None])[0]
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"sale.order {order_id} no existe"}

    # Lines.
    line_rows: list = []
    try:
        line_rows = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order.line",
            [["order_id", "=", order_id]],
            ["id", "product_id", "name", "product_uom_qty", "price_unit",
             "discount", "price_subtotal", "price_total", "qty_invoiced",
             "qty_delivered", "product_updatable"],
        ) or []
    except Exception as e:
        logger.warning("state_summary lines read failed: %s", e)

    state = order.get("state") or ""
    is_locked = state in ("done", "cancel")
    can_edit_header = state in _STATES_HEADER_FULL
    can_edit_lines = state in _STATES_LINE_EDITABLE
    can_hard_delete_lines = state in _STATES_LINE_HARD_DELETE

    invoices = order.get("invoice_ids") or []
    pickings = order.get("picking_ids") or []

    result = {
        "success": True,
        "order_id": order["id"],
        "name": order.get("name"),
        "state": state,
        "state_ec": order.get("state_ec"),
        "collection_state": order.get("collection_state"),
        "estado_despacho": order.get("estado_despacho"),
        "is_cash_sale": order.get("is_cash_sale"),
        "amount_untaxed": order.get("amount_untaxed"),
        "amount_tax": order.get("amount_tax"),
        "amount_total": order.get("amount_total"),
        "amount_discount": order.get("amount_discount"),
        "discount_type": order.get("discount_type"),
        "discount_rate": order.get("discount_rate"),
        "partner_id": order.get("partner_id"),
        "user_id": order.get("user_id"),
        "date_order": order.get("date_order"),
        "validity_date": order.get("validity_date"),
        "lines_count": len(line_rows),
        "lines": [
            {
                "line_id": ln["id"],
                "product_id": ln.get("product_id"),
                "name": ln.get("name"),
                "qty": ln.get("product_uom_qty"),
                "qty_invoiced": ln.get("qty_invoiced", 0),
                "qty_delivered": ln.get("qty_delivered", 0),
                "price_unit": ln.get("price_unit"),
                "discount": ln.get("discount"),
                "subtotal": ln.get("price_subtotal"),
                "total": ln.get("price_total"),
                "product_updatable": ln.get("product_updatable", True),
            } for ln in line_rows
        ],
        "invoices_count": len(invoices),
        "invoice_ids": list(invoices),
        "pickings_count": len(pickings),
        "picking_ids": list(pickings),
        "permissions": {
            "can_edit_header": can_edit_header,
            "can_edit_lines": can_edit_lines,
            "can_hard_delete_lines": can_hard_delete_lines,
            "is_locked": is_locked,
        },
    }
    _log_call("get_quotation_state_summary", tenant_id, {"order_id": order_id},
              {"lines": len(line_rows)}, None,
              int((time.time() - started) * 1000))
    return result


# ---- 9) transition_quotation ----------------------------------------------

# Mapping de acción → (método Odoo, estados de origen permitidos).
# Algunos métodos son custom de Tecnosmart (l10n_ec_sri).
_TRANSITIONS = {
    "confirm": ("action_confirm", {"draft", "sent", "approved"}),
    "cancel": ("action_cancel", {"draft", "sent", "sale", "approved", "waiting_approval"}),
    "draft": ("action_draft", {"sent", "cancel", "approved"}),
    "approve": ("approve_transfer", {"waiting_approval"}),
    "reject": ("reject_transfer", {"waiting_approval"}),
    "done": ("action_done", {"sale"}),
    "unlock": ("action_unlock", {"done"}),
    "generar_despacho": ("action_generar_despacho", {"sale", "approved"}),
    "generar_factura": ("action_generar_factura", {"sale", "approved"}),
    "procesar_venta": ("action_procesar_venta_vendedor", {"approved"}),
    "aprobar": ("action_aprobar", {"draft", "sent", "approved"}),
}


def odoo_transition_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    action: str,
    *,
    confirmed: bool = False,
) -> dict:
    """Disparar una transición de estado en la sale.order.

    ``action`` ∈ confirm/cancel/draft/approve/reject/done/unlock/
    generar_despacho/generar_factura/procesar_venta/aprobar.
    Operaciones IRREVERSIBLES requieren confirmed=True explícito;
    las consultas (es decir, ninguna aquí) no aplican.
    """
    started = time.time()
    log_args = {"order_id": order_id, "action": action, "confirmed": confirmed}

    if action not in _TRANSITIONS:
        return {"success": False, "error_code": "invalid_action",
                "error_detail": (
                    f"action debe estar en {sorted(_TRANSITIONS.keys())}, "
                    f"recibido: {action!r}"
                )}

    method, allowed = _TRANSITIONS[action]

    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state"],
    )
    if not order:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"sale.order {order_id} no existe"}

    if order["state"] not in allowed:
        return _state_error(order, allowed, action)

    if not confirmed:
        return {
            "success": False,
            "requires_confirmation": True,
            "preview": {
                "order_name": order["name"],
                "action": action,
                "method": method,
                "current_state": order["state"],
                "allowed_origin_states": sorted(allowed),
                "warning": (
                    "Esta operación es IRREVERSIBLE en algunos casos "
                    "(confirm/done/cancel). Confirma con el usuario antes."
                ),
            },
            "action": "transition_quotation",
        }

    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", method, [order_id],
        )
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error("transition_quotation failed order=%s action=%s err=%s\n%s",
                     order_id, action, e, tb)
        _log_call("transition_quotation", tenant_id, log_args, None, str(e), elapsed)
        return {"success": False, "error_code": "transition_failed",
                "error_detail": str(e), "action": action}

    after = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["id", "name", "state"],
    ) or {}
    summary = _recompute_summary(tenant_id, url, db, user, password, order_id)
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "action": action,
        "from_state": order["state"],
        "to_state": after.get("state"),
        "recomputed": summary,
    }
    _log_call("transition_quotation", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result
