"""Sales tools — quotations, sale orders, customer queries."""

import logging
import time
import traceback
from datetime import datetime

from mcp_odoo.tools.formatters import format_price_display
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


def _resolve_lines_to_template_ids(
    tenant_id: str, url: str, db: str, user: str, password: str,
    lines: list[dict],
) -> tuple[list[dict] | None, dict | None]:
    """Resolve each line's product_code (preferred) / product_id (legacy) into
    a canonical template_id, in batch.

    The LLM speaks SKUs (e.g. "VID0581") because they appear literally in
    every search_products result. Numeric template_ids, on the other hand,
    are easy to fabricate when the model loses context. So this function
    accepts whichever the caller sent, but PREFERS product_code when both
    are present, and resolves all SKUs in ONE Odoo query (batch search on
    default_code IN [...]).

    Returns:
        (resolved_lines, error_dict)
        - resolved_lines: same list as input, with each item now carrying a
          definite "_template_id" int field (consumed by the variant
          resolver downstream). Original keys are preserved.
        - error_dict: a {success: False, ...} payload if any SKU could not
          be resolved (or any line was missing both fields). When non-None,
          resolved_lines is None.
    """
    if not lines:
        return None, {
            "success": False,
            "error_code": "no_lines",
            "error_detail": "lines vacio: se requiere al menos un producto",
        }

    # Validate per-line shape and split by which identifier was provided.
    codes_to_resolve: set[str] = set()
    for idx, line in enumerate(lines):
        code = line.get("product_code")
        pid = line.get("product_id")
        if code and isinstance(code, str) and code.strip():
            codes_to_resolve.add(code.strip())
            if pid:
                logger.info(
                    "ODOO_CALL resolve_lines tenant=%s line=%d both product_code=%r and product_id=%r passed; preferring product_code",
                    tenant_id, idx, code, pid,
                )
        elif isinstance(pid, int) and pid > 0:
            logger.warning(
                "ODOO_CALL resolve_lines tenant=%s line=%d using legacy product_id=%d (deprecated; prefer product_code/SKU)",
                tenant_id, idx, pid,
            )
        else:
            return None, {
                "success": False,
                "error_code": "missing_product_identifier",
                "error_detail": (
                    f"Linea {idx}: cada linea debe traer 'product_code' "
                    f"(SKU del catalogo, preferido) o 'product_id' (template_id, legacy). "
                    f"Recibi: {line!r}"
                ),
                "line_index": idx,
            }

    # Batch resolve every SKU in one Odoo query.
    code_to_template_id: dict[str, int] = {}
    if codes_to_resolve:
        try:
            rows = odoo_search(
                tenant_id, url, db, user, password,
                "product.template",
                [["default_code", "in", list(codes_to_resolve)], ["active", "=", True]],
                ["id", "default_code"],
                limit=len(codes_to_resolve) * 2,  # account for multiple variants per code (rare)
            )
        except Exception as e:
            return None, {
                "success": False,
                "error_code": "sku_lookup_failed",
                "error_detail": (
                    f"Error consultando Odoo para resolver SKUs {sorted(codes_to_resolve)}: {e}"
                ),
                "missing_skus": sorted(codes_to_resolve),
            }

        # default_code SHOULD be unique on product.template, but if a tenant
        # has duplicates we keep the first and warn.
        for row in rows or []:
            code = row.get("default_code")
            if not code:
                continue
            if code in code_to_template_id:
                logger.warning(
                    "ODOO_CALL resolve_lines tenant=%s duplicate default_code=%r; keeping first match id=%d",
                    tenant_id, code, code_to_template_id[code],
                )
                continue
            code_to_template_id[code] = row["id"]

        missing = sorted(c for c in codes_to_resolve if c not in code_to_template_id)
        if missing:
            return None, {
                "success": False,
                "error_code": "sku_not_found",
                "error_detail": (
                    f"SKUs no encontrados en el catalogo (o inactivos): {missing}. "
                    f"Llama search_products primero para obtener el codigo exacto."
                ),
                "missing_skus": missing,
            }

    # Walk lines again, attach the canonical template_id.
    resolved: list[dict] = []
    for line in lines:
        code = (line.get("product_code") or "").strip() or None
        pid = line.get("product_id")
        if code:
            tmpl_id = code_to_template_id[code]
        else:
            tmpl_id = pid  # legacy path
        new_line = dict(line)
        new_line["_template_id"] = tmpl_id
        resolved.append(new_line)
    return resolved, None


def odoo_create_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
    lines: list[dict],
    notes: str = "",
    end_customer_name: str | None = None,
    end_customer_phone: str | None = None,
    end_customer_email: str | None = None,
    salesperson_user_id: int | None = None,
) -> dict:
    """Create a sale order (quotation/proforma) in draft state.

    Args:
        partner_id: res.partner ID
        lines: List of dicts. Each line MUST carry one of:
               - product_code (str): SKU as it appears in search_products
                 (PREFERRED — the LLM cannot fabricate a SKU because they
                 follow a strict format and are echoed verbatim in every
                 search result).
               - product_id (int): legacy product.template id (deprecated;
                 prone to LLM hallucination across turns).
               Optional per line: quantity (default 1), price_unit, discount.
        notes: Optional notes for the order
        end_customer_name: Name of the end customer (consumidor final)
        end_customer_phone: Phone of the end customer
        end_customer_email: Email of the end customer
        salesperson_user_id: res.users id of the salesperson that owns the
            quotation. Sets sale.order.user_id. When None, Odoo defaults to
            the connection user.

    Returns the created order details (read-after-write).
    On failure returns {success: False, error_code, error_detail, ...}.
    """
    started = time.time()
    log_args = {
        "partner_id": partner_id,
        "lines_count": len(lines),
        "first_line": lines[0] if lines else None,
        "salesperson_user_id": salesperson_user_id,
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

    # ── Resolve product_code (SKU) → template_id in batch ──────────────
    # The LLM passes SKUs ("VID0581") because they appear literally in every
    # search_products result and their format is hard to fabricate. Legacy
    # product_id (numeric template_id) is still accepted for backwards
    # compatibility, but flagged in logs.
    resolved_lines, resolve_err = _resolve_lines_to_template_ids(
        tenant_id, url, db, user, password, lines,
    )
    if resolve_err is not None:
        resolve_err.setdefault("partner_id", partner_id)
        _log_call("create_quotation", tenant_id, log_args, None,
                  resolve_err.get("error_detail"),
                  int((time.time() - started) * 1000))
        return resolve_err

    # The entire RAG/search/details pipeline canonicalizes on `product.template`
    # IDs (that's what _fetch_products_live, get_product_details and the
    # embedding store all use, matching what the Odoo UI shows). But
    # sale.order.line needs the `product.product` (variant) ID — Odoo enforces
    # this at the ORM level. So at THIS boundary (and only here) we resolve
    # template_id → first active variant. We also capture uom_id because
    # TecnoSmart's flex_erp override KeyError's on missing 'product_uom'.
    template_ids = list({line["_template_id"] for line in resolved_lines})
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
    for line in resolved_lines:
        tmpl_id = line["_template_id"]
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

    # Salesperson assignment (Sprint 2 B2B). When None, Odoo defaults to
    # the connection user — same behavior as before.
    if salesperson_user_id is not None and isinstance(salesperson_user_id, int) and salesperson_user_id > 0:
        values["user_id"] = salesperson_user_id

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
            price_unit = ln.get("price_unit", 0) or 0
            subtotal = ln.get("price_subtotal", 0) or 0
            tax = ln.get("price_tax", 0) or 0
            total = ln.get("price_total", 0) or 0
            order_lines_detail.append({
                "product": product_name,
                "quantity": ln.get("product_uom_qty", 1),
                "price_unit": price_unit,
                "price_unit_display": format_price_display(price_unit),
                "discount": ln.get("discount", 0),
                "subtotal": subtotal,
                "subtotal_display": format_price_display(subtotal),
                "tax": tax,
                "tax_display": format_price_display(tax),
                "total": total,
                "total_display": format_price_display(total),
            })

    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else str(order.get("partner_id", ""))

    subtotal_amt = order["amount_untaxed"]
    tax_amt = order["amount_tax"]
    total_amt = order["amount_total"]
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "partner": partner_name,
        "lines": order_lines_detail,
        "subtotal": subtotal_amt,
        "subtotal_display": format_price_display(subtotal_amt),
        "tax": tax_amt,
        "tax_display": format_price_display(tax_amt),
        "total": total_amt,
        "total_display": format_price_display(total_amt),
        "share_link": order.get("share_link_so") or "",
    }
    _log_call("create_quotation", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
    return result


def odoo_add_to_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    lines: list[dict],
    salesperson_user_id: int | None = None,
) -> dict:
    """Append product lines to an existing sale.order in draft state.

    Use this when the customer is already chatting about an existing
    quotation and wants to add another product to it. The order must be
    in 'draft' or 'sent' state — confirmed orders are immutable.

    Args:
        order_id: existing sale.order ID
        lines: List of dicts. Each line MUST carry one of:
               - product_code (str): SKU as it appears in search_products
                 (PREFERRED).
               - product_id (int): legacy template_id (deprecated).
               Optional per line: quantity (default 1), price_unit, discount.
        salesperson_user_id: Forwarded for API symmetry with
            create_quotation. Since this tool ONLY merges into an existing
            order, the salesperson on that order is NEVER overwritten —
            we log a no-op when this argument is supplied.

    Returns the updated order summary (same shape as create_quotation).
    """
    started = time.time()
    log_args = {
        "order_id": order_id,
        "lines_count": len(lines),
        "salesperson_user_id": salesperson_user_id,
    }
    if salesperson_user_id is not None:
        logger.info(
            "ODOO_CALL add_to_quotation tenant=%s order=%s salesperson_user_id=%s ignored "
            "(merge path never overwrites sale.order.user_id)",
            tenant_id, order_id, salesperson_user_id,
        )

    if not isinstance(order_id, int) or order_id <= 0:
        err = "order_id requerido y debe ser entero positivo"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_order_id", "error_detail": err}
    if not lines:
        err = "lines vacio"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "no_lines", "error_detail": err}

    # Verify order exists and is editable (draft/sent)
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id], ["id", "state", "partner_id", "name"],
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

    # ── Resolve product_code (SKU) → template_id in batch ──────────────
    resolved_lines, resolve_err = _resolve_lines_to_template_ids(
        tenant_id, url, db, user, password, lines,
    )
    if resolve_err is not None:
        resolve_err.setdefault("order_id", order_id)
        _log_call("add_to_quotation", tenant_id, log_args, None,
                  resolve_err.get("error_detail"),
                  int((time.time() - started) * 1000))
        return resolve_err

    # Resolve template_ids → variant + uom (same logic as create_quotation)
    template_ids = list({line["_template_id"] for line in resolved_lines})
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
    for line in resolved_lines:
        tmpl_id = line["_template_id"]
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
            price_unit = ln.get("price_unit", 0) or 0
            subtotal = ln.get("price_subtotal", 0) or 0
            total = ln.get("price_total", 0) or 0
            order_lines_detail.append({
                "product": product_name,
                "quantity": ln.get("product_uom_qty", 1),
                "price_unit": price_unit,
                "price_unit_display": format_price_display(price_unit),
                "subtotal": subtotal,
                "subtotal_display": format_price_display(subtotal),
                "total": total,
                "total_display": format_price_display(total),
            })
    except Exception as e:
        err = f"Lines created but read-after-write failed: {e}"
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "read_after_write_failed", "error_detail": err, "order_id": order_id}

    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else ""
    subtotal_amt = order["amount_untaxed"]
    tax_amt = order["amount_tax"]
    total_amt = order["amount_total"]
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "partner": partner_name,
        "lines": order_lines_detail,
        "lines_added": len(new_line_cmds),
        "subtotal": subtotal_amt,
        "subtotal_display": format_price_display(subtotal_amt),
        "tax": tax_amt,
        "tax_display": format_price_display(tax_amt),
        "total": total_amt,
        "total_display": format_price_display(total_amt),
        "share_link": order.get("share_link_so") or "",
    }
    _log_call("add_to_quotation", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
    return result


def odoo_change_quotation_customer(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    new_partner_id: int,
    salesperson_user_id: int | None = None,
) -> dict:
    """Reassign a draft sale.order to a different customer.

    Useful when the seller realises mid-quotation that they were
    cotizando para el cliente equivocado. Odoo allows reassigning
    ``partner_id`` while the order is in ``draft`` or ``sent``;
    confirmed orders are immutable.

    Validations:
    - Order exists and ``state in ('draft', 'sent')``.
    - When ``salesperson_user_id`` is provided, ``order.user_id`` must
      match (B2B sellers cannot reassign each other's quotations).
    - ``new_partner_id`` exists in res.partner.

    Returns ``{success: True, order_id, name, partner_id, partner_name,
    state}`` or an error envelope.
    """
    started = time.time()
    log_args = {"order_id": order_id, "new_partner_id": new_partner_id}

    # Read the order.
    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["state", "user_id", "partner_id", "name"],
        )
    except Exception as e:
        err = f"Error consultando sale.order id={order_id}: {e}"
        _log_call("change_quotation_customer", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "read_failed", "error_detail": err}

    if not rows:
        err = f"sale.order {order_id} no existe"
        _log_call("change_quotation_customer", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "order_not_found", "error_detail": err}

    order = rows[0]
    state = order.get("state") or ""
    if state not in ("draft", "sent"):
        err = (
            f"La cotización {order.get('name') or order_id} está en estado "
            f"'{state}'. Solo se puede cambiar el cliente mientras está en "
            "borrador o enviada."
        )
        _log_call("change_quotation_customer", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False, "error_code": "order_not_editable",
            "error_detail": err, "state": state,
        }

    # Guard: same_partner — the LLM passed the CURRENT partner_id again
    # (typically because it reused the active_customer_partner_id from
    # the seller_context header instead of reading odoo_id from the new
    # search_partner result). Block with a clear instruction.
    current_partner = order.get("partner_id")
    current_partner_id = (
        current_partner[0] if isinstance(current_partner, list) and current_partner
        else current_partner if isinstance(current_partner, int) else None
    )
    if current_partner_id and current_partner_id == new_partner_id:
        _log_call("change_quotation_customer", tenant_id, log_args, None,
                  "same_partner_blocked",
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "same_partner",
            "llm_action": (
                "INTERNA: el new_partner_id que pasaste es el cliente "
                "ACTUAL de la cotización (no es un cambio). Para reasignar, "
                "primero llama search_partner con el nombre/empresa del "
                "NUEVO cliente, toma el campo `odoo_id` del primer match "
                "y úsalo como new_partner_id. NO uses el partner_id del "
                "seller_context ni el del cliente actual."
            ),
        }

    if salesperson_user_id is not None:
        order_user = order.get("user_id")
        order_user_id = (
            order_user[0] if isinstance(order_user, list) and order_user
            else order_user if isinstance(order_user, int) else None
        )
        if order_user_id and order_user_id != salesperson_user_id:
            err = (
                f"La cotización pertenece a otro vendedor (user_id={order_user_id}); "
                f"no puedes reasignar el cliente de cotizaciones que no son tuyas."
            )
            _log_call("change_quotation_customer", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {
                "success": False, "error_code": "not_your_quotation",
                "error_detail": err,
            }

    # Validate new_partner_id exists.
    try:
        partner_rows = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [new_partner_id], ["name", "vat"],
        )
    except Exception as e:
        err = f"Error consultando res.partner id={new_partner_id}: {e}"
        _log_call("change_quotation_customer", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_lookup_failed", "error_detail": err}
    if not partner_rows:
        err = f"res.partner {new_partner_id} no existe"
        _log_call("change_quotation_customer", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_not_found", "error_detail": err}

    # Apply the change.
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "write", [order_id], args=[{"partner_id": new_partner_id}],
        )
    except Exception as e:
        err = f"Error actualizando partner_id de sale.order {order_id}: {e}"
        _log_call("change_quotation_customer", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "write_failed", "error_detail": err}

    new_partner = partner_rows[0]
    result = {
        "success": True,
        "order_id": order_id,
        "name": order.get("name") or "",
        "state": state,
        "partner_id": new_partner_id,
        "partner_name": new_partner.get("name") or "",
        "partner_vat": new_partner.get("vat") or "",
    }
    _log_call("change_quotation_customer", tenant_id, log_args,
              {"order_id": order_id, "new_partner_id": new_partner_id}, None,
              int((time.time() - started) * 1000))
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

        lines_detail = []
        for ln in raw_lines:
            total = ln.get("price_total", 0) or 0
            lines_detail.append({
                "product": ln["product_id"][1] if isinstance(ln.get("product_id"), list) else "",
                "quantity": ln.get("product_uom_qty", 1),
                "total": total,
                "total_display": format_price_display(total),
            })
    except Exception as e:
        err = f"Error leyendo cotizacion: {e}"
        _log_call("get_active_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "read_failed", "error_detail": err}

    total_amt = order["amount_total"]
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "total": total_amt,
        "total_display": format_price_display(total_amt),
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
        _log_call("list_quotations", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
        return result

    orders_summary = []
    for r in rows:
        line_ids = r.get("order_line", []) or []
        total_amt = r["amount_total"]
        subtotal_amt = r["amount_untaxed"]
        orders_summary.append({
            "order_id": r["id"],
            "name": r["name"],
            "state": r["state"],
            "state_label": _STATE_LABEL.get(r["state"], r["state"]),
            "total": total_amt,
            "total_display": format_price_display(total_amt),
            "subtotal": subtotal_amt,
            "subtotal_display": format_price_display(subtotal_amt),
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
                price_unit = ln.get("price_unit", 0) or 0
                subtotal = ln.get("price_subtotal", 0) or 0
                tax = ln.get("price_tax", 0) or 0
                total = ln.get("price_total", 0) or 0
                lines_detail.append({
                    "product": pname,
                    "quantity": ln.get("product_uom_qty", 1),
                    "price_unit": price_unit,
                    "price_unit_display": format_price_display(price_unit),
                    "discount": ln.get("discount", 0),
                    "subtotal": subtotal,
                    "subtotal_display": format_price_display(subtotal),
                    "tax": tax,
                    "tax_display": format_price_display(tax),
                    "total": total,
                    "total_display": format_price_display(total),
                })
        except Exception as e:
            err = f"Order {order['name']} read OK but lines failed: {e}"
            _log_call("get_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
            return {"success": False, "error_code": "lines_read_failed", "error_detail": err}

    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else ""

    subtotal_amt = order["amount_untaxed"]
    tax_amt = order["amount_tax"]
    total_amt = order["amount_total"]
    result = {
        "success": True,
        "order_id": order["id"],
        "name": order["name"],
        "state": order["state"],
        "state_label": _STATE_LABEL.get(order["state"], order["state"]),
        "partner": partner_name,
        "subtotal": subtotal_amt,
        "subtotal_display": format_price_display(subtotal_amt),
        "tax": tax_amt,
        "tax_display": format_price_display(tax_amt),
        "total": total_amt,
        "total_display": format_price_display(total_amt),
        "date_order": order.get("date_order") or order.get("create_date"),
        "lines_count": len(lines_detail),
        "lines": lines_detail,
    }
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
) -> dict:
    """Send quotation by email to the customer (action_quotation_send).

    This triggers Odoo's built-in email template for quotations.
    The order state changes from 'draft' to 'sent'.
    """
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

    orders = odoo_read(
        tenant_id, url, db, user, password,
        "sale.order", [order_id],
        ["name", "state", "partner_id"],
    )
    order = orders[0] if orders else {}
    partner_name = order["partner_id"][1] if isinstance(order.get("partner_id"), list) else ""
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
) -> dict:
    """Confirm a draft sale order (quotation → sale order)."""
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "action_confirm", [order_id],
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

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


# ─────────────────────────────────────────────────────────────────────
# Sprint 2 — B2B Sales Assistant
# ─────────────────────────────────────────────────────────────────────


def odoo_lookup_user_by_email(
    tenant_id: str, url: str, db: str, user: str, password: str,
    email: str,
) -> dict:
    """Locate a res.users record by email (login OR partner.email).

    Used by seller_otp.py to validate that a Telegram user requesting
    /login corresponds to an actual Odoo salesperson before sending an
    OTP.

    Returns
    -------
    {success: True, user: {user_id, name, login, email, partner_id, partner_name}}
    {success: False, error_code, error_detail}
    """
    started = time.time()
    log_args = {"email": email}

    if not email or not isinstance(email, str) or not email.strip():
        err = "email requerido y debe ser un texto no vacio"
        _log_call("lookup_user_by_email", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_email",
            "error_detail": err,
        }

    needle = email.strip()
    fields = ["id", "name", "login", "partner_id", "active"]

    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "res.users",
            [["login", "=ilike", needle]],
            fields,
            limit=10,
        )
    except Exception as e:
        err = f"Error consultando res.users por login={needle!r}: {e}"
        _log_call("lookup_user_by_email", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "lookup_failed",
            "error_detail": err,
        }

    if not rows:
        # Fallback: search by partner.email (the user's partner record).
        try:
            rows = odoo_search(
                tenant_id, url, db, user, password,
                "res.users",
                [["partner_id.email", "=ilike", needle]],
                fields,
                limit=10,
            )
        except Exception as e:
            err = f"Error consultando res.users por partner.email={needle!r}: {e}"
            _log_call("lookup_user_by_email", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {
                "success": False,
                "error_code": "lookup_failed",
                "error_detail": err,
            }

    # Filter inactive accounts — they cannot log in.
    active_rows = [r for r in (rows or []) if r.get("active")]
    if not active_rows:
        err = "No hay un vendedor con ese email en Odoo"
        _log_call("lookup_user_by_email", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "user_not_found",
            "error_detail": err,
        }

    if len(active_rows) > 1:
        logger.warning(
            "ODOO_CALL lookup_user_by_email tenant=%s email=%r returned %d active users; "
            "picking lowest id",
            tenant_id, needle, len(active_rows),
        )

    chosen = min(active_rows, key=lambda r: r["id"])
    partner_field = chosen.get("partner_id")
    partner_id: int | None = None
    partner_name: str | None = None
    if isinstance(partner_field, list) and len(partner_field) >= 2:
        partner_id = partner_field[0]
        partner_name = partner_field[1]
    elif isinstance(partner_field, int):
        partner_id = partner_field

    # Pull partner.email + vat (the seller's own RUC/cedula). The vat
    # is needed by niko's B2B gates so the seller cannot accidentally
    # invoke ``identify_customer`` with their own RUC and end up
    # cotizando a sí mismo (observed in smoke test).
    partner_email: str | None = None
    partner_vat: str | None = None
    if partner_id:
        try:
            partner_rows = odoo_read(
                tenant_id, url, db, user, password,
                "res.partner", [partner_id], ["email", "name", "vat"],
            )
            if partner_rows:
                partner_email = partner_rows[0].get("email") or None
                partner_vat = partner_rows[0].get("vat") or None
                # Refresh partner_name from canonical record (more reliable
                # than the Many2one display string).
                partner_name = partner_rows[0].get("name") or partner_name
        except Exception as e:
            logger.warning(
                "ODOO_CALL lookup_user_by_email tenant=%s could not read partner_id=%s: %s",
                tenant_id, partner_id, e,
            )

    final_email = partner_email or chosen.get("login") or None

    result = {
        "success": True,
        "user": {
            "user_id": chosen["id"],
            "name": chosen.get("name") or "",
            "login": chosen.get("login") or "",
            "email": final_email,
            "partner_id": partner_id,
            "partner_name": partner_name,
            "partner_vat": partner_vat,
        },
    }
    _log_call("lookup_user_by_email", tenant_id, log_args,
              {"user_id": chosen["id"]}, None,
              int((time.time() - started) * 1000))
    return result


# ─────────────────────────────────────────────────────────────────────
# Sprint 2F — Generic ERP-agnostic policy & authorization
# ─────────────────────────────────────────────────────────────────────


def _resolve_group_id_by_xmlid(
    tenant_id: str, url: str, db: str, user: str, password: str,
    module: str, name: str,
) -> int | None:
    """Resolve a group's database id from its XML id (module + name).

    Returns the res.groups id, or None if the XML id is not present (e.g.
    the underlying Odoo module is not installed in this tenant).
    """
    rows = odoo_search(
        tenant_id, url, db, user, password,
        "ir.model.data",
        [["module", "=", module], ["name", "=", name]],
        ["res_id"],
        limit=1,
    )
    if not rows:
        return None
    res_id = rows[0].get("res_id")
    if isinstance(res_id, int) and res_id > 0:
        return res_id
    return None


def odoo_get_discount_policy(
    tenant_id: str, url: str, db: str, user: str, password: str,
) -> dict:
    """Generic 'get discount policy' contract for the ERP plugin.

    Reads the ERP-specific knobs (in Odoo: an ``ir.config_parameter`` and
    the ``account.group_account_manager`` security group) and returns a
    plugin-agnostic shape that ``niko/`` can rely on. The Niko core never
    has to know the Odoo internals — those leak only through the
    ``source`` metadata which the dashboard renders as a read-only badge
    ("this came from Odoo X parameter").

    Returns
    -------
    {
        "success": True,
        "policy": {
            "max_pct": float,         # 0.0 means 'no control configured'
            "supervisors": [
                {"user_id": int, "name": str, "email": str|None, "login": str}
            ],
            "source": {
                "max_pct_key": "ir.config_parameter:sale.partner_max_sale_discount",
                "supervisors_group_xmlid": "account.group_account_manager"
            }
        }
    }
    or {"success": False, "error_code": "...", "error_detail": "..."}
    """
    started = time.time()
    log_args: dict = {}

    # 1) Read max_pct from ir.config_parameter.
    # In Odoo 13, ``get_param`` is a class-level method that does not
    # accept an ``ids`` arg, so XML-RPC ``execute_kw('ir.config_parameter',
    # 'get_param', [[], 'key', '0'])`` fails with "takes from 2 to 3
    # positional arguments but 4 were given". Read the row directly.
    max_pct: float = 0.0
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "ir.config_parameter",
            [["key", "=", "sale.partner_max_sale_discount"]],
            ["value"],
            limit=1,
        )
        if rows:
            try:
                max_pct = float(rows[0].get("value") or 0.0)
            except (TypeError, ValueError):
                # Non-numeric stored value (e.g. an admin typo) → treat as
                # "no control configured" rather than blocking the caller.
                max_pct = 0.0
    except Exception as e:
        err = f"Error leyendo ir.config_parameter sale.partner_max_sale_discount: {e}"
        _log_call("get_discount_policy", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "config_parameter_read_failed",
            "error_detail": err,
        }

    # 2) Resolve the supervisor group via its XML id.
    try:
        group_id = _resolve_group_id_by_xmlid(
            tenant_id, url, db, user, password,
            module="account", name="group_account_manager",
        )
    except Exception as e:
        err = f"Error resolviendo grupo account.group_account_manager: {e}"
        _log_call("get_discount_policy", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "supervisor_group_lookup_failed",
            "error_detail": err,
        }

    if group_id is None:
        err = "Modulo account no instalado o group_account_manager no encontrado"
        _log_call("get_discount_policy", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "supervisor_group_missing",
            "error_detail": err,
        }

    # 3) List active supervisors in that group.
    try:
        sup_rows = odoo_search(
            tenant_id, url, db, user, password,
            "res.users",
            [["groups_id", "in", [group_id]], ["active", "=", True]],
            ["id", "name", "login", "partner_id"],
            limit=200,
        )
    except Exception as e:
        err = f"Error listando supervisores en grupo {group_id}: {e}"
        _log_call("get_discount_policy", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "supervisors_lookup_failed",
            "error_detail": err,
        }

    # 4) Read partner emails in batch for the canonical email per supervisor.
    partner_ids: list[int] = []
    user_to_partner: dict[int, int | None] = {}
    for row in sup_rows or []:
        pf = row.get("partner_id")
        pid: int | None = None
        if isinstance(pf, list) and len(pf) >= 2:
            pid = pf[0]
        elif isinstance(pf, int):
            pid = pf
        user_to_partner[row["id"]] = pid
        if pid:
            partner_ids.append(pid)

    partner_email_map: dict[int, str | None] = {}
    if partner_ids:
        try:
            partners = odoo_read(
                tenant_id, url, db, user, password,
                "res.partner", partner_ids, ["email"],
            )
            for p in partners or []:
                partner_email_map[p["id"]] = p.get("email") or None
        except Exception as e:
            # Non-fatal — fall back to login as the email surrogate below.
            logger.warning(
                "ODOO_CALL get_discount_policy tenant=%s could not read partner emails: %s",
                tenant_id, e,
            )

    supervisors: list[dict] = []
    for row in sup_rows or []:
        pid = user_to_partner.get(row["id"])
        partner_email = partner_email_map.get(pid) if pid else None
        login = row.get("login") or ""
        # Login is often the email already in modern Odoo installs; fall
        # back to it when partner.email is empty.
        email = partner_email or (login if "@" in login else None)
        supervisors.append({
            "user_id": row["id"],
            "name": row.get("name") or "",
            "email": email,
            "login": login,
        })

    result = {
        "success": True,
        "policy": {
            "max_pct": max_pct,
            "supervisors": supervisors,
            "source": {
                "max_pct_key": "ir.config_parameter:sale.partner_max_sale_discount",
                "supervisors_group_xmlid": "account.group_account_manager",
            },
        },
    }
    _log_call("get_discount_policy", tenant_id, log_args,
              {"max_pct": max_pct, "supervisor_count": len(supervisors)}, None,
              int((time.time() - started) * 1000))
    return result


def odoo_verify_seller_authorization(
    tenant_id: str, url: str, db: str, user: str, password: str,
    email: str,
) -> dict:
    """Generic 'is this email an authorized seller in the ERP?' contract.

    Deeper than ``odoo_lookup_user_by_email``: not only finds the user,
    but also confirms the user belongs to the ERP's seller security
    group (in Odoo: ``sales_team.group_sale_salesman``). Niko's core
    asks "is this email allowed to sell?" and the plugin owns the answer.

    Returns
    -------
    {
        "success": True,
        "authorized": bool,
        "user": {"user_id", "name", "email", "login", "partner_id"} | None,
        "reason": str | None
    }
    or {"success": False, "error_code": "...", "error_detail": "..."}
    """
    started = time.time()
    log_args = {"email": email}

    # 1) Reuse the canonical lookup-by-email helper. It already filters
    #    inactive users, falls back to partner.email and resolves the
    #    canonical partner email — no need to re-implement here.
    lookup = odoo_lookup_user_by_email(
        tenant_id, url, db, user, password, email,
    )

    if lookup.get("success") is False:
        code = lookup.get("error_code")
        if code == "user_not_found":
            _log_call("verify_seller_authorization", tenant_id, log_args,
                      {"authorized": False, "reason": "user_not_found"}, None,
                      int((time.time() - started) * 1000))
            return {
                "success": True,
                "authorized": False,
                "user": None,
                "reason": "Email no registrado en res.users",
            }
        if code == "invalid_email":
            # Surface the validation error to the caller; the wrapper in
            # niko/ should never have called us with garbage.
            _log_call("verify_seller_authorization", tenant_id, log_args,
                      None, "invalid_email",
                      int((time.time() - started) * 1000))
            return lookup
        # Any other lookup failure is an infrastructure problem.
        _log_call("verify_seller_authorization", tenant_id, log_args,
                  None, code or "lookup_failed",
                  int((time.time() - started) * 1000))
        return lookup

    user_obj = lookup.get("user") or {}

    # 2) Resolve the seller group via its XML id.
    try:
        group_id = _resolve_group_id_by_xmlid(
            tenant_id, url, db, user, password,
            module="sales_team", name="group_sale_salesman",
        )
    except Exception as e:
        err = f"Error resolviendo grupo sales_team.group_sale_salesman: {e}"
        _log_call("verify_seller_authorization", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "seller_group_lookup_failed",
            "error_detail": err,
        }

    if group_id is None:
        err = "Modulo sales_team no instalado o group_sale_salesman no encontrado"
        _log_call("verify_seller_authorization", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "seller_group_missing",
            "error_detail": err,
        }

    # 3) Read the user's groups_id and check membership.
    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "res.users", [user_obj["user_id"]], ["groups_id"],
        )
    except Exception as e:
        err = f"Error leyendo res.users.groups_id para user_id={user_obj.get('user_id')}: {e}"
        _log_call("verify_seller_authorization", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "user_groups_read_failed",
            "error_detail": err,
        }

    user_group_ids = (rows[0].get("groups_id") or []) if rows else []
    authorized = group_id in user_group_ids

    user_payload = {
        "user_id": user_obj.get("user_id"),
        "name": user_obj.get("name") or "",
        "email": user_obj.get("email"),
        "login": user_obj.get("login") or "",
        "partner_id": user_obj.get("partner_id"),
    }

    if authorized:
        _log_call("verify_seller_authorization", tenant_id, log_args,
                  {"authorized": True, "user_id": user_payload["user_id"]}, None,
                  int((time.time() - started) * 1000))
        return {
            "success": True,
            "authorized": True,
            "user": user_payload,
            "reason": None,
        }

    _log_call("verify_seller_authorization", tenant_id, log_args,
              {"authorized": False, "user_id": user_payload["user_id"]}, None,
              int((time.time() - started) * 1000))
    return {
        "success": True,
        "authorized": False,
        "user": user_payload,
        "reason": "Usuario existe pero no pertenece al grupo de Ventas",
    }


def odoo_apply_discount(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    discount_pct: float,
    line_id: int | None = None,
    reason: str | None = None,
) -> dict:
    """Apply a percentage discount to a quotation.

    If `line_id` is given, only that line is updated. Otherwise every line
    on the order is updated. The orchestrator validates the discount
    against approval thresholds — this tool just applies what is asked.

    Returns
    -------
    {success: True, order_id, lines_updated, discount_pct,
     new_amount_total, new_amount_untaxed}
    {success: False, error_code, error_detail, ...}
    """
    started = time.time()
    log_args = {
        "order_id": order_id,
        "discount_pct": discount_pct,
        "line_id": line_id,
        "reason": reason,
    }

    # Validation -----------------------------------------------------------
    if not isinstance(order_id, int) or order_id <= 0:
        err = "order_id requerido y debe ser entero positivo"
        _log_call("apply_discount", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_order_id", "error_detail": err}

    if not isinstance(discount_pct, (int, float)) or isinstance(discount_pct, bool):
        err = "discount_pct debe ser numerico"
        _log_call("apply_discount", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_discount", "error_detail": err}

    if discount_pct < 0 or discount_pct > 100:
        err = f"discount_pct fuera de rango (0-100): {discount_pct}"
        _log_call("apply_discount", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_discount", "error_detail": err}

    if line_id is not None and (not isinstance(line_id, int) or line_id <= 0):
        err = "line_id debe ser entero positivo o None"
        _log_call("apply_discount", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_line_id", "error_detail": err}

    # Read order and check editable state ---------------------------------
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["id", "name", "state", "order_line", "user_id"],
        )
    except Exception as e:
        err = f"Error leyendo sale.order {order_id}: {e}"
        _log_call("apply_discount", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "order_read_failed", "error_detail": err}

    if not orders:
        err = f"sale.order {order_id} no existe"
        _log_call("apply_discount", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "order_not_found", "error_detail": err}

    order = orders[0]
    if order["state"] not in ("draft", "sent"):
        err = (
            f"sale.order {order['name']} esta en estado '{order['state']}', "
            f"no se puede modificar el descuento"
        )
        _log_call("apply_discount", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "order_not_editable",
            "error_detail": err,
            "order_id": order_id,
            "state": order["state"],
        }

    all_line_ids: list[int] = list(order.get("order_line") or [])
    if not all_line_ids:
        err = f"sale.order {order['name']} no tiene lineas"
        _log_call("apply_discount", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "no_lines", "error_detail": err}

    # Resolve target line ids ---------------------------------------------
    if line_id is not None:
        # Confirm the line belongs to this order.
        try:
            line_rows = odoo_read(
                tenant_id, url, db, user, password,
                "sale.order.line", [line_id], ["id", "order_id"],
            )
        except Exception as e:
            err = f"Error leyendo sale.order.line {line_id}: {e}"
            _log_call("apply_discount", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "line_read_failed", "error_detail": err}

        if not line_rows:
            err = f"sale.order.line {line_id} no existe"
            _log_call("apply_discount", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "line_not_found", "error_detail": err}

        line_row = line_rows[0]
        line_order = line_row.get("order_id")
        line_order_id = (
            line_order[0] if isinstance(line_order, list) and line_order
            else line_order
        )
        if line_order_id != order_id:
            err = (
                f"La linea {line_id} pertenece a la orden {line_order_id}, "
                f"no a {order_id}"
            )
            _log_call("apply_discount", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {
                "success": False,
                "error_code": "line_mismatch",
                "error_detail": err,
                "order_id": order_id,
                "line_id": line_id,
            }
        target_ids = [line_id]
    else:
        target_ids = all_line_ids

    # Apply the discount ---------------------------------------------------
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order.line", "write", target_ids,
            args=[{"discount": discount_pct}],
        )
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error(
            "ODOO_CALL apply_discount FAILED tenant=%s order=%s lines=%s err=%s\n%s",
            tenant_id, order_id, target_ids, e, tb,
        )
        _log_call("apply_discount", tenant_id, log_args, None, str(e), elapsed)
        return {
            "success": False,
            "error_code": "discount_write_failed",
            "error_detail": str(e),
            "order_id": order_id,
        }

    # Optionally log a chatter note (best-effort — never block the result).
    if reason:
        try:
            note_body = (
                f"Descuento {discount_pct}% aplicado. Motivo: {reason}"
            )
            odoo_call_method(
                tenant_id, url, db, user, password,
                "sale.order", "message_post", [order_id],
                kwargs={"body": note_body, "subtype_xmlid": "mail.mt_note"},
            )
        except Exception as e:
            logger.warning(
                "ODOO_CALL apply_discount tenant=%s order=%s message_post failed: %s",
                tenant_id, order_id, e,
            )

    # Re-read totals -------------------------------------------------------
    try:
        refreshed = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["amount_total", "amount_untaxed", "amount_tax"],
        )
        head = refreshed[0] if refreshed else {}
        new_total = head.get("amount_total", 0) or 0
        new_untaxed = head.get("amount_untaxed", 0) or 0
    except Exception as e:
        err = f"Descuento aplicado pero no pudimos releer totales: {e}"
        _log_call("apply_discount", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "read_after_write_failed",
            "error_detail": err,
            "order_id": order_id,
        }

    result = {
        "success": True,
        "order_id": order_id,
        "lines_updated": len(target_ids),
        "discount_pct": discount_pct,
        "new_amount_total": new_total,
        "new_amount_total_display": format_price_display(new_total),
        "new_amount_untaxed": new_untaxed,
        "new_amount_untaxed_display": format_price_display(new_untaxed),
    }
    _log_call("apply_discount", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


_ALLOWED_QUOTATION_STATES = {"draft", "sent", "sale", "done", "cancel"}


def odoo_list_my_quotations(
    tenant_id: str, url: str, db: str, user: str, password: str,
    salesperson_user_id: int,
    state: list[str] | None = None,
    limit: int = 20,
) -> dict:
    """List quotations owned by a specific salesperson.

    Default state filter is ['draft', 'sent'] (active quotations the seller
    might still close). Use the `state` arg to widen the filter.
    """
    started = time.time()
    log_args = {
        "salesperson_user_id": salesperson_user_id,
        "state": state,
        "limit": limit,
    }

    if not isinstance(salesperson_user_id, int) or salesperson_user_id <= 0:
        err = "salesperson_user_id requerido y debe ser entero positivo"
        _log_call("list_my_quotations", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_salesperson_user_id",
            "error_detail": err,
        }

    states = state or ["draft", "sent"]
    if not isinstance(states, list) or not states:
        err = "state debe ser una lista no vacia"
        _log_call("list_my_quotations", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_state",
            "error_detail": err,
        }

    bad = [s for s in states if s not in _ALLOWED_QUOTATION_STATES]
    if bad:
        err = (
            f"Estados invalidos: {bad}. "
            f"Usa cualquiera de {sorted(_ALLOWED_QUOTATION_STATES)}"
        )
        _log_call("list_my_quotations", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_state",
            "error_detail": err,
        }

    if not isinstance(limit, int) or limit <= 0:
        limit = 20
    limit = min(limit, 200)

    domain = [
        ["user_id", "=", salesperson_user_id],
        ["state", "in", states],
    ]

    try:
        orders = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order",
            domain,
            ["id", "name", "partner_id", "amount_total", "state",
             "date_order", "order_line"],
            limit=limit,
            order="date_order DESC",
        )
    except Exception as e:
        err = f"Error consultando cotizaciones del vendedor {salesperson_user_id}: {e}"
        _log_call("list_my_quotations", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "search_failed",
            "error_detail": err,
        }

    # Batch-load the partners (best effort; missing data simply omitted).
    partner_ids: list[int] = []
    for o in orders or []:
        pf = o.get("partner_id")
        if isinstance(pf, list) and pf:
            partner_ids.append(pf[0])
        elif isinstance(pf, int):
            partner_ids.append(pf)
    partner_ids = sorted(set(partner_ids))

    partner_index: dict[int, dict] = {}
    if partner_ids:
        try:
            partner_rows = odoo_read(
                tenant_id, url, db, user, password,
                "res.partner", partner_ids, ["id", "name", "vat"],
            )
            for pr in partner_rows or []:
                partner_index[pr["id"]] = pr
        except Exception as e:
            logger.warning(
                "ODOO_CALL list_my_quotations tenant=%s could not read partners %s: %s",
                tenant_id, partner_ids, e,
            )

    quotations = []
    for o in orders or []:
        pf = o.get("partner_id")
        if isinstance(pf, list) and pf:
            partner_id_val = pf[0]
            partner_display = pf[1] if len(pf) > 1 else ""
        elif isinstance(pf, int):
            partner_id_val = pf
            partner_display = ""
        else:
            partner_id_val = None
            partner_display = ""

        partner_record = partner_index.get(partner_id_val) if partner_id_val else None
        partner_name = (partner_record or {}).get("name") or partner_display or ""
        partner_vat = (partner_record or {}).get("vat") or None

        amount_total = o.get("amount_total", 0) or 0
        line_ids = o.get("order_line") or []

        quotations.append({
            "order_id": o["id"],
            "name": o.get("name") or "",
            "partner_id": partner_id_val,
            "partner_name": partner_name,
            "partner_vat": partner_vat,
            "amount_total": amount_total,
            "amount_total_display": format_price_display(amount_total),
            "state": o.get("state") or "",
            "state_label": _STATE_LABEL.get(o.get("state") or "", o.get("state") or ""),
            "date_order": str(o.get("date_order") or ""),
            "line_count": len(line_ids),
        })

    result = {
        "success": True,
        "count": len(quotations),
        "quotations": quotations,
    }
    _log_call("list_my_quotations", tenant_id, log_args,
              {"count": len(quotations)}, None,
              int((time.time() - started) * 1000))
    return result


def odoo_schedule_visit(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
    summary: str,
    date_deadline: str,
    salesperson_user_id: int,
    note: str | None = None,
) -> dict:
    """Create a mail.activity (Meeting type) on the partner.

    The activity shows up in the salesperson's calendar/CRM as a pending
    visit. `date_deadline` must be a YYYY-MM-DD string.
    """
    started = time.time()
    log_args = {
        "partner_id": partner_id,
        "summary": summary,
        "date_deadline": date_deadline,
        "salesperson_user_id": salesperson_user_id,
        "has_note": bool(note),
    }

    # Argument validation -------------------------------------------------
    if not isinstance(partner_id, int) or partner_id <= 0:
        err = "partner_id requerido y debe ser entero positivo"
        _log_call("schedule_visit", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_partner_id", "error_detail": err}

    if not isinstance(salesperson_user_id, int) or salesperson_user_id <= 0:
        err = "salesperson_user_id requerido y debe ser entero positivo"
        _log_call("schedule_visit", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_salesperson_user_id",
            "error_detail": err,
        }

    if not summary or not isinstance(summary, str) or not summary.strip():
        err = "summary requerido y debe ser texto no vacio"
        _log_call("schedule_visit", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_summary", "error_detail": err}

    if not date_deadline or not isinstance(date_deadline, str):
        err = "date_deadline requerido en formato YYYY-MM-DD"
        _log_call("schedule_visit", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_date", "error_detail": err}

    try:
        datetime.strptime(date_deadline, "%Y-%m-%d")
    except ValueError:
        err = f"date_deadline debe estar en formato YYYY-MM-DD, recibi {date_deadline!r}"
        _log_call("schedule_visit", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "invalid_date", "error_detail": err}

    # Resolve the Meeting activity type id --------------------------------
    try:
        meeting_rows = odoo_search(
            tenant_id, url, db, user, password,
            "mail.activity.type",
            [["name", "=", "Meeting"]],
            ["id", "name"],
            limit=1,
        )
    except Exception as e:
        err = f"Error consultando mail.activity.type: {e}"
        _log_call("schedule_visit", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "activity_type_lookup_failed",
            "error_detail": err,
        }

    if not meeting_rows:
        # Fallback for non-English Odoo installs.
        try:
            meeting_rows = odoo_search(
                tenant_id, url, db, user, password,
                "mail.activity.type",
                [["name", "ilike", "meeting"]],
                ["id", "name"],
                limit=1,
            )
        except Exception as e:
            err = f"Error consultando mail.activity.type (fallback): {e}"
            _log_call("schedule_visit", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {
                "success": False,
                "error_code": "activity_type_lookup_failed",
                "error_detail": err,
            }

    if not meeting_rows:
        err = (
            "No encontre el tipo de actividad 'Meeting' en Odoo. "
            "Pide al admin que active mail.mail_activity_data_meeting."
        )
        _log_call("schedule_visit", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "meeting_activity_type_missing",
            "error_detail": err,
        }
    meeting_type_id = meeting_rows[0]["id"]

    # Resolve ir.model id for res.partner ---------------------------------
    try:
        model_rows = odoo_search(
            tenant_id, url, db, user, password,
            "ir.model",
            [["model", "=", "res.partner"]],
            ["id", "model"],
            limit=1,
        )
    except Exception as e:
        err = f"Error consultando ir.model res.partner: {e}"
        _log_call("schedule_visit", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "model_lookup_failed",
            "error_detail": err,
        }

    if not model_rows:
        err = "No encontre ir.model para res.partner; no puedo crear la actividad"
        _log_call("schedule_visit", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "partner_model_missing",
            "error_detail": err,
        }
    partner_model_id = model_rows[0]["id"]

    # Validate partner exists ---------------------------------------------
    try:
        partner_rows = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [partner_id], ["id", "name"],
        )
    except Exception as e:
        err = f"Error leyendo partner {partner_id}: {e}"
        _log_call("schedule_visit", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "partner_read_failed",
            "error_detail": err,
        }

    if not partner_rows:
        err = f"Partner id={partner_id} no existe en Odoo"
        _log_call("schedule_visit", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "partner_not_found",
            "error_detail": err,
            "partner_id": partner_id,
        }

    partner_name = partner_rows[0].get("name") or ""

    # Build vals + create -------------------------------------------------
    vals: dict = {
        "activity_type_id": meeting_type_id,
        "res_model_id": partner_model_id,
        "res_model": "res.partner",
        "res_id": partner_id,
        "user_id": salesperson_user_id,
        "summary": summary.strip(),
        "date_deadline": date_deadline,
    }
    if note:
        if note.lstrip().startswith("<"):
            vals["note"] = note
        else:
            vals["note"] = f"<p>{note}</p>"

    try:
        activity_id = odoo_create(
            tenant_id, url, db, user, password,
            "mail.activity", vals,
        )
    except Exception as e:
        elapsed = int((time.time() - started) * 1000)
        tb = traceback.format_exc()
        logger.error(
            "ODOO_CALL schedule_visit FAILED tenant=%s partner=%s err=%s\n%s",
            tenant_id, partner_id, e, tb,
        )
        _log_call("schedule_visit", tenant_id, log_args, None, str(e), elapsed)
        return {
            "success": False,
            "error_code": "activity_create_failed",
            "error_detail": str(e),
            "partner_id": partner_id,
        }

    result = {
        "success": True,
        "activity_id": activity_id,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "summary": summary.strip(),
        "date_deadline": date_deadline,
        "salesperson_user_id": salesperson_user_id,
    }
    _log_call("schedule_visit", tenant_id, log_args,
              {"activity_id": activity_id}, None,
              int((time.time() - started) * 1000))
    return result
