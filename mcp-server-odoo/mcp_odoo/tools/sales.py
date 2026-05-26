"""Sales tools — quotations, sale orders, customer queries."""

import logging
import time
import traceback
from typing import Any

from mcp_odoo.tools.generic import odoo_search, odoo_read, odoo_create, odoo_write, odoo_call_method

logger = logging.getLogger("mcp_odoo.sales")


# ---------------------------------------------------------------------------
# Anti-confusion guard: detect when an LLM passes the numeric suffix of a
# sale.order ``name`` (ej. 'VENTA122173' → 122173) as if it were the
# ``sale.order.id`` (which would be a different integer, ej. 113604).
#
# Production incident (2026-05-12): the LLM ran
#   get_quotation(order_id=113604)        → ✅ returned VENTA122173
#   find_quotation_by_name('VENTA122173') → redundant
#   get_quotation(order_id=122173)        → ❌ order does not exist
# and after the error, drifted off the original request.
#
# The heuristic below catches this: when the order_id falls in the range
# of the in-use ``name`` suffixes AND a record ``name='VENTA{order_id}'``
# exists, we reject the call with a precise error pointing at the real id.
# ---------------------------------------------------------------------------

# In-use VENTA suffix range as of 2026-05 (Tecnosmart).
_NAME_SUFFIX_RANGE = (110_000, 130_000)


def _absolutize_share_link(share_link: str | None, base_url: str | None) -> str:
    """Return an absolute URL for an Odoo ``share_link_so`` value.

    Bug M9 (May 2026): the customer sometimes received a path-only
    ``/files/quotations/VENTA122584.pdf`` from Odoo when ``web.base.url``
    was unset. We normalise here so the LLM always surfaces a clickable
    https://... link.

    Rules:
      * Empty / None input → return "".
      * Already absolute (starts with ``http://`` or ``https://``) →
        returned unchanged.
      * ``//host/path`` (protocol-relative) → prefix with ``https:``.
      * Relative path (``/x/y`` or ``x/y``) → prefix with ``base_url``
        when present; otherwise return as-is (best effort).
    """
    if not share_link:
        return ""
    link = str(share_link).strip()
    if not link:
        return ""
    low = link.lower()
    if low.startswith(("http://", "https://")):
        return link
    if link.startswith("//"):
        return "https:" + link
    if not base_url:
        # No Odoo URL hint — surface the raw link rather than risk
        # corrupting it.
        return link
    base = str(base_url).rstrip("/")
    if not link.startswith("/"):
        link = "/" + link
    return base + link


def _looks_like_name_suffix(order_id_int: int) -> bool:
    """Return True if ``order_id_int`` falls in the in-use ``name`` suffix range.

    Tecnosmart sale.order.name follows the pattern ``VENTAxxxxxx`` with a
    monotonically increasing 6-digit suffix in the [110000, 129999]
    range as of 2026-05. Real ``sale.order.id`` values also live around
    this range, so we CANNOT distinguish a real id from a name suffix by
    value alone — we need a confirmation lookup downstream.

    This function is the cheap pre-filter that skips the lookup for
    values that obviously aren't suffix candidates (e.g. 42).
    """
    lo, hi = _NAME_SUFFIX_RANGE
    return lo <= order_id_int <= hi


def _guard_order_id_vs_name_suffix(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
) -> dict | None:
    """If ``order_id`` looks like the suffix of an existing ``name``, reject.

    Returns a structured error dict when confusion is detected, or
    ``None`` when the value passes the guard.

    Logic (collision-safe, 2026-05-12 fix):

    1. Cheap pre-filter — if ``order_id`` is outside the in-use
       suffix range it cannot possibly be confusion → pass.
    2. **Authoritative check** — look up ``order_id`` as a real
       ``sale.order.id``. If a record exists with that id, the value
       IS a valid order_id and we MUST let it through, even if a
       different ``sale.order`` happens to have ``name='VENTA{order_id}'``
       (collision between an id of order A and a name suffix of order B).
    3. Only when the id does NOT exist do we check for a name match
       and surface the ``suggested_order_id`` error.

    Production incident (2026-05-12, before fix step 2 was added): the
    LLM called ``send_quotation(order_id=113998)`` where 113998 is the
    REAL id of ``VENTA122567`` AND coincidentally another quotation
    named ``VENTA113998`` exists with id 105429. The original guard
    rejected the call with a false positive. Step 2 prevents this by
    verifying the id first.
    """
    if not _looks_like_name_suffix(order_id):
        return None

    # Step 2: authoritative existence check. If order_id is a real
    # sale.order.id in this tenant, NEVER reject — collisions with a
    # different record's name suffix are not confusion.
    try:
        id_check = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order",
            [["id", "=", order_id]],
            ["id"],
            limit=1,
        )
    except Exception:
        # Don't block legitimate calls when the verification lookup
        # itself errors out — fall through to the suffix check.
        id_check = []
    if id_check:
        # The id is real → the value IS a valid order_id, regardless
        # of whether some unrelated record also has name=VENTA{order_id}.
        return None

    # Step 3: id does not exist. Now check if it matches an existing
    # name suffix and, if so, point at the real id.
    try:
        candidate_name = f"VENTA{order_id}"
        check = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order",
            [["name", "=", candidate_name]],
            ["id"],
            limit=1,
        )
    except Exception:
        # If the verification lookup fails, fall through to the normal
        # flow — we never block legitimate calls because the guard
        # itself errored out.
        return None
    if not check:
        return None
    real_id = check[0]["id"] if isinstance(check[0], dict) else check[0]
    if real_id == order_id:
        # Defensive: should not happen (id_check above would have hit)
        # but keep the original self-reference guard.
        return None
    err = (
        f"order_id={order_id} parece ser el sufijo del name "
        f"'{candidate_name}', NO el order_id real. El order_id de "
        f"'{candidate_name}' es {real_id}. Reintenta con "
        f"order_id={real_id}, O si no estás seguro llama "
        f"find_quotation_by_name(name='{candidate_name}')."
    )
    return {
        "success": False,
        "error_code": "order_id_looks_like_name_suffix",
        "error_detail": err,
        "hint": (
            "Pasa el order_id real (devuelto por list_quotations o "
            "find_quotation_by_name), NO el sufijo numérico del name."
        ),
        "suggested_order_id": real_id,
        "found_name": candidate_name,
    }


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


def _resolve_product_code_to_template_id(
    tenant_id: str, url: str, db: str, user: str, password: str,
    code: str,
) -> dict:
    """Resolve a product visible code (default_code) to a template_id.

    Returns a dict that the caller can branch on:
      - {"ok": True, "template_id": int, "code": "<normalized>"}      → unique match
      - {"ok": False, "error_code": "product_code_not_found", ...}    → 0 matches
      - {"ok": False, "error_code": "ambiguous_product_code", ...}    → >1 matches

    The orchestrator/LLM passes the user-visible code (e.g. "MON0026")
    instead of an integer template_id. This avoids the LLM hallucinating
    sequential template_ids (the root cause of the 14791/14792/... bug
    where it fabricates IDs from past search_products calls).

    Match rule: case-insensitive exact match on ``product.template.default_code``
    among active templates. We DO NOT use the BGE-M3 RAG here — that's
    a separate concern (`search_products` for natural-language lookup);
    once the LLM has the canonical SKU, this resolution must be exact.
    """
    if not code or not isinstance(code, str):
        return {
            "ok": False,
            "error_code": "product_code_not_found",
            "error_detail": "code vacío o no es string",
        }

    normalized = code.strip().upper()
    if not normalized:
        return {
            "ok": False,
            "error_code": "product_code_not_found",
            "error_detail": "code vacío después de normalizar",
        }

    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "product.template",
            [["default_code", "=", normalized], ["active", "=", True]],
            ["id", "default_code", "name"],
            limit=10,
        )
    except Exception as e:
        return {
            "ok": False,
            "error_code": "product_code_lookup_failed",
            "error_detail": f"Error consultando product.template por default_code={normalized!r}: {e}",
            "code": normalized,
        }

    if not rows:
        return {
            "ok": False,
            "error_code": "product_code_not_found",
            "error_detail": f"No existe ningún producto con código '{normalized}'.",
            "code": normalized,
        }

    if len(rows) > 1:
        candidates = [
            {
                "template_id": r["id"],
                "code": r.get("default_code") or "",
                "name": r.get("name") or "",
            }
            for r in rows
        ]
        return {
            "ok": False,
            "error_code": "ambiguous_product_code",
            "error_detail": (
                f"El código '{normalized}' coincide con {len(rows)} productos activos. "
                "Pasa product_id (template_id) en lugar de code para desambiguar."
            ),
            "code": normalized,
            "candidates": candidates,
        }

    template_id = int(rows[0]["id"])
    logger.debug(
        "_resolve_product_code_to_template_id: code=%s -> template_id=%s",
        normalized, template_id,
    )
    return {"ok": True, "template_id": template_id, "code": normalized}


def _normalize_quotation_lines(
    tenant_id: str, url: str, db: str, user: str, password: str,
    lines: list[dict],
) -> tuple[list[dict] | None, dict | None]:
    """Normalize a ``lines`` payload for create/add_to_quotation.

    Accepts each line with EITHER ``product_id`` (int, template_id) OR
    ``code`` (string, default_code). When ``code`` is provided without
    ``product_id``, this helper resolves the template_id via Odoo.

    Returns ``(normalized_lines, None)`` on success, or
    ``(None, error_dict)`` if any line fails to resolve. The error dict
    has the same shape the tool returns to the orchestrator (so callers
    can just propagate it).

    Behaviour matrix per line:
      - product_id only:       passthrough (legacy).
      - template_id only:      aliased to product_id (legacy).
      - code only:             resolve → fill product_id.
      - product_id + code:     keep product_id, leave code in dict so the
                               existing consistency guard later in
                               create_quotation can validate them.
      - none:                  error (no_valid_product_ids), same as before.
    """
    normalized: list[dict] = []
    for line in lines:
        pid = line.get("product_id") or line.get("template_id")
        code = line.get("code")

        if not pid and code:
            res = _resolve_product_code_to_template_id(
                tenant_id, url, db, user, password, str(code),
            )
            if not res.get("ok"):
                err: dict[str, Any] = {
                    "success": False,
                    "error_code": res["error_code"],
                    "error_detail": res.get("error_detail", ""),
                    "code": res.get("code", code),
                }
                if "candidates" in res:
                    err["candidates"] = res["candidates"]
                return None, err
            new_line = {**line, "product_id": res["template_id"]}
            # Preserve the resolved code so the downstream mismatch
            # validator stays a no-op (declared == real after resolution).
            new_line["code"] = res["code"]
            normalized.append(new_line)
            continue

        if pid:
            normalized.append({**line, "product_id": pid})
            continue

        # No pid and no code → caller will raise no_valid_product_ids
        # (we preserve the original line so the error count is accurate).
        normalized.append(line)

    return normalized, None


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
    salesperson_user_id: int | None = None,
) -> dict:
    """Create a sale order (quotation/proforma) in draft state.

    Args:
        partner_id: res.partner ID
        lines: List of dicts with {product_id, quantity, price_unit (optional)}
        notes: Optional notes for the order
        end_customer_name: Name of the end customer (consumidor final)
        end_customer_phone: Phone of the end customer
        end_customer_email: Email of the end customer
        salesperson_user_id: Optional res.users ID. When provided, it is
            written to ``sale.order.user_id`` so Odoo attributes commissions
            to that vendor. Used by the B2B flow where the agent passes the
            authenticated seller's ``odoo_user_id``. If omitted, Odoo
            defaults to the connection user.

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

    # ── Tenant-owner partner guard (PII leak prevention) ────────────────
    # Bug observed prod 2026-05-15: LLM passed partner_id=1 (the
    # tenant owner's res.partner) when it could not identify the lead
    # Nataly Torres. Odoo accepted it because the partner exists, and
    # the quote was created under "Aldas Romero Erik Andres" (the
    # vendor) leaking the owner's PII back to the customer.
    #
    # Reject any partner_id that belongs to a res.company on this Odoo
    # instance. The legitimate path for unidentified leads is
    # ``create_partner`` first, then ``create_quotation`` with the new
    # partner_id.
    try:
        companies = odoo_search(
            tenant_id, url, db, user, password,
            "res.company", [], fields=["partner_id"], limit=100,
        )
        company_partner_ids = {
            c["partner_id"][0]
            for c in (companies or [])
            if isinstance(c.get("partner_id"), list) and c["partner_id"]
        }
        if partner_id in company_partner_ids:
            err = (
                f"partner_id={partner_id} pertenece a una res.company "
                "(dueño del tenant). No puede ser destinatario de "
                "una cotización. Identifica al cliente real o crea un "
                "partner nuevo con create_partner."
            )
            _log_call("create_quotation", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {
                "success": False,
                "error_code": "partner_is_tenant_company",
                "error_detail": err,
                "partner_id": partner_id,
                "hint": (
                    "Antes de cotizar para un cliente nuevo (sin RUC), "
                    "llama create_partner con nombre/email/teléfono. "
                    "El partner_id retornado SI puede usarse en "
                    "create_quotation."
                ),
            }
    except Exception as _comp_exc:
        # Non-fatal: si la verificación de company falla seguimos
        # adelante. El guard es defense-in-depth, no el único filtro.
        log.debug("company partner guard skipped: %s", _comp_exc)

    # The entire RAG/search/details pipeline canonicalizes on `product.template`
    # IDs (that's what _fetch_products_live, get_product_details and the
    # embedding store all use, matching what the Odoo UI shows). But
    # sale.order.line needs the `product.product` (variant) ID — Odoo enforces
    # this at the ORM level. So at THIS boundary (and only here) we resolve
    # template_id → first active variant. We also capture uom_id because
    # TecnoSmart's flex_erp override KeyError's on missing 'product_uom'.
    # Normalize: accept "product_id", "template_id", or "code" (default_code)
    # interchangeably. Passing `code` is preferred — it removes the entire
    # class of "LLM hallucinated a template_id" bugs.
    normalized_lines, resolve_error = _normalize_quotation_lines(
        tenant_id, url, db, user, password, lines,
    )
    if resolve_error is not None:
        _log_call("create_quotation", tenant_id, log_args, None,
                  resolve_error.get("error_code", "code_resolution_failed"),
                  int((time.time() - started) * 1000))
        return resolve_error
    # Drop lines that ended up with no product_id (legacy behaviour).
    normalized_lines = [ln for ln in (normalized_lines or []) if ln.get("product_id")]
    if not normalized_lines:
        err = "Ninguna línea tiene product_id ni code válido. Pasa product_id (template_id) o code (default_code) por línea."
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

    # SECURITY: validate declared code matches Odoo default_code to catch
    # LLM hallucinations where it uses a wrong template_id (e.g. webcam
    # instead of laptop). Fetch actual default_codes from Odoo and compare
    # against the 'code' field optionally declared per line.
    lines_with_code = [l for l in lines if l.get("code")]
    if lines_with_code:
        try:
            real_templates = odoo_read(
                tenant_id, url, db, user, password,
                "product.template", template_ids, ["id", "default_code"],
            )
            real_code_by_id = {
                t["id"]: (t.get("default_code") or "").upper()
                for t in (real_templates or [])
            }
            for ln in lines_with_code:
                declared = (ln.get("code") or "").upper()
                real = real_code_by_id.get(ln["product_id"], "")
                if declared and real and declared != real:
                    err = (
                        f"Inconsistencia: el LLM declaro code={declared!r} para "
                        f"template_id={ln['product_id']}, pero en Odoo ese template "
                        f"es {real!r}. Vuelve a buscar con search_products y usa "
                        "EXACTAMENTE el template_id que devuelve el JSON."
                    )
                    _log_call("create_quotation", tenant_id, log_args, None, err,
                              int((time.time() - started) * 1000))
                    return {
                        "success": False,
                        "error_code": "product_code_mismatch",
                        "error_detail": err,
                        "declared_code": declared,
                        "actual_code": real,
                        "template_id": ln["product_id"],
                    }
        except Exception as _val_exc:
            logger.warning("create_quotation: code validation skipped: %s", _val_exc)

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
        # Iter 75: ver comment en add_to_quotation — price_unit del bot
        # IGNORADO. Odoo aplica pricelist + product.list_price.
        if "price_unit" in line:
            logger.warning(
                "create_quotation: IGNORING bot-supplied price_unit=%s "
                "for template=%s — using Odoo pricelist instead",
                line.get("price_unit"), tmpl_id,
            )
        if "discount" in line:
            # tecno_discount_sale valida vs sale.partner_max_sale_discount
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

    # Vendedor asignado (B2B commission attribution). Odoo calcula comisiones
    # según sale.order.user_id; cuando el agente B2B autenticado pasa su
    # odoo_user_id, lo escribimos aquí para que la cotización quede
    # correctamente atribuida. Si viene None, Odoo usa el connection user
    # por default.
    if salesperson_user_id is not None:
        try:
            values["user_id"] = int(salesperson_user_id)
        except (TypeError, ValueError):
            err = (
                f"salesperson_user_id debe ser entero, recibido "
                f"{salesperson_user_id!r}"
            )
            _log_call("create_quotation", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {
                "success": False,
                "error_code": "invalid_salesperson_user_id",
                "error_detail": err,
            }

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

    # Iter88 2026-05-25: amount_total ya incluye IVA (pricelist
    # tecno_l10n_ec_sri price_include=True). El campo "tax" se mantiene
    # por compat pero ``note`` lo aclara al LLM para evitar el bug
    # observado en megachat T21 (bot recitó "subtotal 152.95 + IVA
    # 22.94 = 175.89" inventando matemáticas).
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "partner": partner_name,
        "partner_id": partner_id,  # int — used by orchestrator security validator
        "lines": order_lines_detail,
        "total": order["amount_total"],
        "subtotal_excl_iva": order["amount_untaxed"],
        "iva_15_amount": order["amount_tax"],
        "iva_already_included_in_total": True,
        "note": (
            "El campo 'total' YA incluye IVA 15%. NO sumes "
            "'iva_15_amount' al 'total' — es desglose informativo, "
            "no un cargo adicional. Al cliente muéstrale SOLO el "
            "'total' con la frase 'incluye IVA 15%'."
        ),
        "share_link": _absolutize_share_link(order.get("share_link_so"), url),
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
    salesperson_user_id: int | None = None,
) -> dict:
    """Append product lines to an existing sale.order in draft state.

    Use this when the customer is already chatting about an existing
    quotation and wants to add another product to it. The order must be
    in 'draft' or 'sent' state — confirmed orders are immutable.

    USAGE — confirmed flag (CRITICAL, read carefully):

    - **Intent IMPLÍCITO del usuario** (verbos imperativos como "agrega",
      "añade", "ponme", "mete", "a la actual", "agrégalo a mi proforma",
      "súbelo", "incluye"): the customer has ALREADY confirmed by stating
      intent. Call DIRECTLY with confirmed=True. Do NOT make an extra
      preview round-trip — they already gave the go-ahead.

    - **Intent EXPLORATORIO del usuario** ("muéstrame qué quedaría con X",
      "y si agrego Y", "haz un dry-run", "ver cómo se vería"): call with
      confirmed=False, show the preview, then wait for an additional
      confirmation ('sí', 'confirmo', 'dale', 'procede') before calling
      again with confirmed=True.

    The preview return (when confirmed=False) is sanitized human-readable
    summary: it contains product codes and names only — never internal
    product_id integers. You can pass the ``preview`` field verbatim to
    the user.

    Args:
        order_id: existing sale.order ID
        lines: [{product_id (template_id), quantity}, ...]
        confirmed: False = dry-run preview only; True = execute the write
        salesperson_user_id: Optional res.users ID. We NEVER overwrite an
            already-assigned vendedor — that would steal commissions from
            whoever owns the order. Only when the order has no user_id
            (e.g. created by an automation without a vendor) AND a value is
            provided here, we set it. This is the safest behaviour for
            add-to operations.

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

    # Anti-confusion guard: reject when order_id looks like the numeric
    # suffix of a sale.order name (e.g. 'VENTA122173' → 122173).
    guard = _guard_order_id_vs_name_suffix(
        tenant_id, url, db, user, password, order_id,
    )
    if guard is not None:
        _log_call("add_to_quotation", tenant_id, log_args, guard, None,
                  int((time.time() - started) * 1000))
        return guard

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
            "sale.order", [order_id],
            ["id", "state", "partner_id", "name", "amount_total", "user_id"],
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

    # Normalize lines BEFORE the preview so dry-runs reflect the resolved
    # product_id (the LLM may have passed `code` only). This also lets us
    # short-circuit invalid codes early, before the user sees a misleading
    # confirmation prompt.
    # Accepts "product_id", "template_id", or "code" (default_code).
    add_normalized, resolve_error = _normalize_quotation_lines(
        tenant_id, url, db, user, password, lines,
    )
    if resolve_error is not None:
        _log_call("add_to_quotation", tenant_id, log_args, None,
                  resolve_error.get("error_code", "code_resolution_failed"),
                  int((time.time() - started) * 1000))
        return resolve_error
    add_normalized = [ln for ln in (add_normalized or []) if ln.get("product_id")]
    if not add_normalized:
        err = "Ninguna línea tiene product_id ni code válido. Pasa product_id (template_id) o code (default_code) por línea."
        _log_call("add_to_quotation", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "no_valid_product_ids", "error_detail": err}
    lines = add_normalized

    # Dry-run / preview — return what would be done without writing to Odoo.
    #
    # IMPORTANT (fixed 2026-05-17): the preview MUST NOT contain raw
    # ``product_id=N`` strings. The response_guard at the orchestrator
    # layer flags any integer-id leak as ``internal_id_leak`` and forces
    # a retry, which leads to garbled customer-facing messages
    # ("Para recomendarte componentes que faltan necesito ver el catálogo
    # en vivo …"). Instead we resolve template_id → (default_code, name,
    # list_price) so the preview reads like a real invoice line.
    if not confirmed:
        template_ids_for_preview = list({ln["product_id"] for ln in lines if ln.get("product_id")})
        tmpl_map: dict[int, dict] = {}
        if template_ids_for_preview:
            try:
                tmpls = odoo_read(
                    tenant_id, url, db, user, password,
                    "product.template", template_ids_for_preview,
                    ["id", "default_code", "name", "list_price"],
                )
                tmpl_map = {t["id"]: t for t in (tmpls or [])}
            except Exception as _read_exc:
                logger.warning(
                    "add_to_quotation preview: could not resolve product names: %s",
                    _read_exc,
                )

        line_descriptions: list[str] = []
        for ln in lines:
            pid = ln.get("product_id")
            qty = ln.get("quantity", 1)
            tmpl = tmpl_map.get(pid) if pid is not None else None
            if tmpl:
                code = tmpl.get("default_code") or ""
                name = (tmpl.get("name") or "producto").strip()
                # Trim names so the preview stays scannable.
                if len(name) > 70:
                    name = name[:67].rstrip() + "…"
                price = tmpl.get("list_price") or 0
                prefix = f"{code} · " if code else ""
                line_descriptions.append(
                    f"{prefix}{name} x{qty} (USD {price:.2f})"
                )
            else:
                # Fallback when the read failed; still avoid leaking the
                # raw template_id integer to the LLM.
                line_descriptions.append(f"producto (código no resuelto) x{qty}")

        order_name = order.get("name", f"orden {order_id}")
        current_total = order.get("amount_total", 0)
        preview_msg = (
            f"Voy a agregar a {order_name} (total actual USD {current_total:.2f}):\n  - "
            + "\n  - ".join(line_descriptions)
            + "\nSi el usuario ya dio intent claro ('agrega', 'añade', 'a la actual'), "
            "llama de nuevo con confirmed=true SIN preguntar otra vez. "
            "Si fue exploratorio, muestra esta lista y espera confirmación."
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
        # Iter 75: price_unit del bot SE IGNORA. Odoo aplica
        # product.pricelist.get_product_price(template, qty, partner)
        # automáticamente cuando creamos la línea con product_id sin
        # price_unit override. Carlos-LLM v7 confirmó bot inflando
        # PSU007 $15.30→$19.99 (+30%) y RAM0043 $98.42→$135.56 (+37%).
        # tecno_discount_sale module valida discount contra
        # sale.partner_max_sale_discount config → si exceed → UserError.
        if "price_unit" in line:
            logger.warning(
                "add_to_quotation: IGNORING bot-supplied price_unit=%s "
                "for template=%s — using Odoo pricelist instead",
                line.get("price_unit"), tmpl_id,
            )
        has_overrides = "discount" in line
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
        # NO price_unit override. Odoo auto-computes from pricelist.
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

        # B2B commission attribution: when a salesperson_user_id is provided
        # AND the order has NO user assigned (rare; Odoo defaults to the
        # connection user at create-time, but legacy data or imports can
        # leave it empty), set it. We NEVER overwrite an existing user_id —
        # that would steal commissions from the original vendedor.
        if salesperson_user_id is not None:
            try:
                sp_id = int(salesperson_user_id)
            except (TypeError, ValueError):
                sp_id = None
            existing_user = order.get("user_id")
            # Odoo returns Many2one as either False, [id, name], or omitted.
            has_user = bool(existing_user) and not (
                isinstance(existing_user, list) and not existing_user
            )
            if sp_id is not None and not has_user:
                odoo_write(
                    tenant_id, url, db, user, password,
                    "sale.order", [order_id], {"user_id": sp_id},
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
    # Iter88: ver odoo_create_quotation — mismo fix de IVA-already-included.
    result = {
        "success": True,
        "order_id": order_id,
        "name": order["name"],
        "state": order["state"],
        "partner": partner_name,
        "lines": order_lines_detail,
        "lines_added": len(new_line_cmds),
        "total": order["amount_total"],
        "subtotal_excl_iva": order["amount_untaxed"],
        "iva_15_amount": order["amount_tax"],
        "iva_already_included_in_total": True,
        "note": (
            "El 'total' YA incluye IVA 15%. NO sumes 'iva_15_amount' al "
            "'total'. Al cliente muestra solo el 'total' con 'incluye IVA 15%'."
        ),
        "share_link": _absolutize_share_link(order.get("share_link_so"), url),
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
        "iva_already_included_in_total": True,
        "note": (
            "El 'total' YA incluye IVA 15%. No agregues impuesto adicional."
        ),
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
            "subtotal_excl_iva": r["amount_untaxed"],
            "iva_already_included_in_total": True,
            "date_order": r.get("date_order") or r.get("create_date"),
            "lines_count": len(line_ids),
            "share_link": _absolutize_share_link(r.get("share_link_so"), url),
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

    # Anti-confusion guard: reject when order_id looks like the numeric
    # suffix of a sale.order name (e.g. 'VENTA122173' → 122173).
    guard = _guard_order_id_vs_name_suffix(
        tenant_id, url, db, user, password, order_id,
    )
    if guard is not None:
        _log_call("get_quotation", tenant_id, log_args, guard, None,
                  int((time.time() - started) * 1000))
        return guard

    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["id", "name", "state", "partner_id", "amount_total",
             "amount_untaxed", "amount_tax", "date_order", "create_date",
             "order_line", "share_link_so"],
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
        # Public Odoo portal URL with access_token. Customers can use
        # this to view/sign/pay via the standard Odoo /my/orders/<id>
        # template. Distinct from Niko's signature mini-app (which is
        # served at /sign/ on niko.galapagos.tech). The LLM should
        # share THIS link when the user asks for "el link de la venta".
        "share_link": _absolutize_share_link(order.get("share_link_so"), url),
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

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    guard = _guard_order_id_vs_name_suffix(
        tenant_id, url, db, user, password, order_id,
    )
    if guard is not None:
        _log_call("render_quotation_pdf", tenant_id, log_args, guard, None,
                  int((time.time() - started) * 1000))
        return guard

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
    # Anti-confusion guard: reject when order_id looks like the numeric
    # suffix of a sale.order name (e.g. 'VENTA122173' → 122173).
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            return guard

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
    # Anti-confusion guard: reject when order_id looks like the numeric
    # suffix of a sale.order name (e.g. 'VENTA122173' → 122173).
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            return guard

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


def odoo_lookup_user_by_email(
    tenant_id: str, url: str, db: str, user: str, password: str,
    email: str,
) -> dict:
    """Locate a res.users record by login username OR partner email.

    Used by seller_otp.py to validate /login requests before sending OTP.

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
        return {"success": False, "error_code": "invalid_email", "error_detail": err}

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
        return {"success": False, "error_code": "lookup_failed", "error_detail": err}

    if not rows:
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
            return {"success": False, "error_code": "lookup_failed", "error_detail": err}

    active_rows = [r for r in (rows or []) if r.get("active")]
    if not active_rows:
        err = "No hay un vendedor con ese usuario/email en Odoo"
        _log_call("lookup_user_by_email", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "user_not_found", "error_detail": err}

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

    partner_email: str | None = None
    if partner_id:
        try:
            partner_rows = odoo_read(
                tenant_id, url, db, user, password,
                "res.partner", [partner_id], ["email", "name"],
            )
            if partner_rows:
                partner_email = partner_rows[0].get("email") or None
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
        },
    }
    _log_call("lookup_user_by_email", tenant_id, log_args,
              {"user_id": chosen["id"]}, None,
              int((time.time() - started) * 1000))
    return result


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
    """Fetch the SINGLE most recent quotation for a partner (limit=1, newest first).

    Returns full order detail (same format as get_quotation) plus _card metadata.
    Use when the customer asks for ONE quotation in SINGULAR: 'mi última
    proforma', 'la más reciente', 'la última cotización'.

    DO NOT use when the customer asks for MULTIPLE in PLURAL: 'mis últimas N
    cotizaciones', 'los últimos productos que cotizé/proformé', 'mis
    cotizaciones recientes' — those need ``odoo_list_quotations(limit=N)`` and
    optionally ``odoo_get_quotation`` per order to enumerate lines.
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
    code: str | None = None,
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

    if quantity is None and price_unit is None and discount is None and name is None and product_id is None and code is None:
        err = "Debes especificar al menos un campo a actualizar"
        _log_call("update_quotation_line", tenant_id, log_args, None, err, 0)
        return {"success": False, "error_code": "no_changes", "error_detail": err}

    # Resolve code → product_id when the caller passed `code` only.
    if product_id is None and code:
        res = _resolve_product_code_to_template_id(
            tenant_id, url, db, user, password, str(code),
        )
        if not res.get("ok"):
            err_out: dict[str, Any] = {
                "success": False,
                "error_code": res["error_code"],
                "error_detail": res.get("error_detail", ""),
                "code": res.get("code", code),
            }
            if "candidates" in res:
                err_out["candidates"] = res["candidates"]
            _log_call("update_quotation_line", tenant_id, log_args, None,
                      res["error_code"], 0)
            return err_out
        product_id = res["template_id"]

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
    # Iter 75b: IGNORAR price_unit del bot también en update_quotation_line.
    # Carlos-LLM v9 vio VENTA123543 con PSU007=$19.99 (catálogo $15.30),
    # RAM0043=$135.56 (catálogo $98.42). No había log "IGNORING" porque
    # iter75 inicial sólo cubría create_quotation y add_to_quotation.
    # Este endpoint singular era la puerta de atrás.
    if price_unit is not None:
        logger.warning(
            "update_quotation_line: IGNORING bot-supplied price_unit=%s "
            "for line_id=%s — Odoo pricelist remains authoritative",
            price_unit, line_id,
        )
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

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("change_quotation_customer", tenant_id, log_args,
                      guard, None, int((time.time() - started) * 1000))
            return guard

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

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("apply_global_discount", tenant_id, log_args,
                      guard, None, int((time.time() - started) * 1000))
            return guard

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

    # Tecnosmart gotcha (Odoo 13 + l10n_ec_sri):
    # ``sale.order.calculate_discount(order_id)`` returns ``None``
    # server-side; Odoo's XMLRPC layer then raises "cannot marshal None
    # unless allow_none is enabled" even though the discount DID get
    # written and propagated to lines. Same fix-shape used by
    # ``odoo_sign_quotation`` (L5092) and ``action_confirm`` fallback
    # (L5137): catch the marshal-None error and treat as success.
    try:
        odoo_write(tenant_id, url, db, user, password,
                   "sale.order", [order_id],
                   {"discount_type": discount_type, "discount_rate": float(discount_rate)})
    except Exception as e:
        err_msg = str(e)
        if "cannot marshal None" in err_msg or "allow_none" in err_msg:
            logger.info(
                "apply_global_discount: write returned marshal-None on order=%s — treating as success",
                order_id,
            )
        else:
            elapsed = int((time.time() - started) * 1000)
            tb = traceback.format_exc()
            logger.error("apply_global_discount write failed order=%s err=%s\n%s", order_id, e, tb)
            _log_call("apply_global_discount", tenant_id, log_args, None, str(e), elapsed)
            return {"success": False, "error_code": "discount_failed", "error_detail": err_msg}

    try:
        odoo_call_method(tenant_id, url, db, user, password,
                         "sale.order", "calculate_discount", [order_id])
    except Exception as e:
        err_msg = str(e)
        if "cannot marshal None" in err_msg or "allow_none" in err_msg:
            logger.info(
                "apply_global_discount: calculate_discount returned marshal-None on order=%s — treating as success",
                order_id,
            )
        else:
            elapsed = int((time.time() - started) * 1000)
            tb = traceback.format_exc()
            logger.error("apply_global_discount calculate_discount failed order=%s err=%s\n%s", order_id, e, tb)
            _log_call("apply_global_discount", tenant_id, log_args, None, str(e), elapsed)
            return {"success": False, "error_code": "discount_failed", "error_detail": err_msg}

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

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("set_quotation_header", tenant_id, log_args, guard,
                      None, int((time.time() - started) * 1000))
            return guard

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
    product_id: int | None = None,
    quantity: float = 1.0,
    *,
    code: str | None = None,
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

    Acepta ``product_id`` (template_id) o ``code`` (default_code). Debe
    pasarse al menos uno; si pasas ambos, ``product_id`` gana (legacy).
    """
    started = time.time()
    log_args = {"order_id": order_id, "product_id": product_id,
                "code": code, "quantity": quantity, "confirmed": confirmed}

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("add_quotation_line", tenant_id, log_args, guard,
                      None, int((time.time() - started) * 1000))
            return guard

    # Resolve code → product_id (template_id) when caller passed code only.
    if product_id is None and code:
        res = _resolve_product_code_to_template_id(
            tenant_id, url, db, user, password, str(code),
        )
        if not res.get("ok"):
            err_out: dict[str, Any] = {
                "success": False,
                "error_code": res["error_code"],
                "error_detail": res.get("error_detail", ""),
                "code": res.get("code", code),
            }
            if "candidates" in res:
                err_out["candidates"] = res["candidates"]
            _log_call("add_quotation_line", tenant_id, log_args, None,
                      res["error_code"], 0)
            return err_out
        product_id = res["template_id"]

    if product_id is None:
        err = "Debes pasar product_id (template_id) o code (default_code)."
        _log_call("add_quotation_line", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "missing_product_identifier",
            "error_detail": err,
        }

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
    # Iter 75b: IGNORAR price_unit en add_quotation_line singular (puerta
    # de atrás detectada por Carlos-LLM v9 — VENTA123543 con precios fab).
    if price_unit is not None:
        logger.warning(
            "add_quotation_line: IGNORING bot-supplied price_unit=%s "
            "for product_id=%s — Odoo pricelist remains authoritative",
            price_unit, product_id,
        )
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

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            return guard

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

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            return guard

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

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("transition_quotation", tenant_id, log_args, guard,
                      None, int((time.time() - started) * 1000))
            return guard

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


# ---------------------------------------------------------------------------
# Tool: get_customer_credit_status
# ---------------------------------------------------------------------------

def odoo_get_customer_credit_status(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
) -> dict:
    """Consultar el estado financiero de un cliente — saldo pendiente, facturas
    vencidas y credito disponible.

    Usa account.move (Odoo 13) con type=out_invoice para facturas de cliente.
    Calcula:
      - credit_used: suma de amount_residual de facturas no pagadas / parcialmente pagadas
      - overdue_amount: idem pero solo para facturas con fecha vencida < hoy
      - credit_limit: campo credit_limit de res.partner (si el modulo de credito esta activo)

    Args:
        partner_id: ID del cliente en res.partner (Odoo)

    Returns {success, partner_id, partner_name, credit_used, overdue_amount,
    invoices_pending, invoices_overdue, invoices: [{name, due_date,
    amount_total, amount_residual, payment_state, overdue}]}
    """
    from datetime import date as _date

    started = time.time()
    log_args = {"partner_id": partner_id}

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id",
                "error_detail": "partner_id debe ser entero positivo"}

    # 1. Leer datos del partner (nombre + campos custom Tecnosmart l10n_ec_sri).
    # Iter 77: agregar campos del módulo l10n_ec_sri (Tecnosmart):
    # - blocking_stage = "Crédito Otorgado" (campo principal, NO credit_limit
    #   que es Odoo standard. MEPRIGA: $25,000.00)
    # - temp_credit_amount = "Crédito Temporal"
    # - temp_credit_validity_date = fecha de expiración del crédito temporal
    # - total_credit_availible_sales = computed (disponible con ventas)
    # - terminos_pagos_ids = M2M con account.payment.term ("Terms autorizados")
    # - no_considerar_deudas_vencidas = "Vender con deudas vencidas"
    # - partner_sale_discount = descuento per-partner
    try:
        partners = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [partner_id],
            ["id", "name", "credit", "credit_limit",
             "blocking_stage", "temp_credit_amount", "temp_credit_validity_date",
             "total_credit_availible", "total_credit_availible_sales",
             "terminos_pagos_ids", "no_considerar_deudas_vencidas",
             "partner_sale_discount", "acepta_cheques"],
        )
    except Exception as e:
        # Fallback to Odoo standard fields only (en caso que custom fields
        # no existan en este tenant)
        try:
            partners = odoo_read(
                tenant_id, url, db, user, password,
                "res.partner", [partner_id],
                ["id", "name", "credit", "credit_limit"],
            )
        except Exception as e2:
            err = f"Error leyendo partner {partner_id}: {e2}"
            _log_call("get_customer_credit_status", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "partner_read_failed", "error_detail": err}

    if not partners:
        err = f"Partner id={partner_id} no encontrado"
        _log_call("get_customer_credit_status", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_not_found", "error_detail": err,
                "partner_id": partner_id}

    partner = partners[0]
    partner_name = partner.get("name", "")
    # credit_limit standard Odoo
    credit_limit_raw = partner.get("credit_limit")
    credit_limit = float(credit_limit_raw) if credit_limit_raw else None

    # blocking_stage (Crédito Otorgado real de Tecnosmart, l10n_ec_sri)
    blocking_stage_raw = partner.get("blocking_stage")
    credito_otorgado = float(blocking_stage_raw) if blocking_stage_raw else 0.0

    # Crédito temporal + expiración
    temp_credit_amount = float(partner.get("temp_credit_amount") or 0)
    temp_credit_validity = partner.get("temp_credit_validity_date") or None

    # Crédito disponible computed por Odoo (descuenta deudas vencidas y ventas)
    credito_disponible = partner.get("total_credit_availible")
    credito_disponible_sales = partner.get("total_credit_availible_sales")

    # Términos de pago autorizados (M2M devuelve list de IDs)
    terminos_ids = partner.get("terminos_pagos_ids") or []
    terminos_autorizados: list[dict] = []
    if terminos_ids:
        try:
            term_rows = odoo_read(
                tenant_id, url, db, user, password,
                "account.payment.term", terminos_ids,
                ["id", "name"],
            )
            terminos_autorizados = [
                {"id": t["id"], "name": t.get("name", "")}
                for t in (term_rows or [])
            ]
        except Exception as e:
            logger.warning("get_customer_credit_status: term names read failed: %s", e)

    vender_con_deudas_vencidas = bool(partner.get("no_considerar_deudas_vencidas"))
    descuento_partner = float(partner.get("partner_sale_discount") or 0)
    acepta_cheques = bool(partner.get("acepta_cheques"))

    # 2. Buscar facturas pendientes (account.move, Odoo 13)
    # Iter 77b: Odoo 13 estándar usa `invoice_payment_state`, no
    # `payment_state` (ese campo solo aparece en Odoo 14+). Fallback
    # silencioso al campo correcto sin romper la tool.
    try:
        invoices = odoo_search(
            tenant_id, url, db, user, password,
            "account.move",
            [
                ["partner_id", "child_of", partner_id],
                ["type", "=", "out_invoice"],
                ["state", "=", "posted"],
                ["invoice_payment_state", "in", ["not_paid", "in_payment"]],
            ],
            ["name", "invoice_date_due", "amount_total", "amount_residual", "invoice_payment_state"],
            limit=100,
            order="invoice_date_due asc",
        )
    except Exception as e:
        err = f"Error buscando facturas pendientes: {e}"
        _log_call("get_customer_credit_status", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "invoices_search_failed", "error_detail": err,
                "partner_id": partner_id}

    today_str = _date.today().isoformat()
    invoices_list = []
    credit_used = 0.0
    overdue_amount = 0.0
    invoices_overdue = 0

    for inv in (invoices or []):
        due_date = inv.get("invoice_date_due") or ""
        residual = float(inv.get("amount_residual", 0))
        is_overdue = bool(due_date and due_date < today_str)
        credit_used += residual
        if is_overdue:
            overdue_amount += residual
            invoices_overdue += 1
        invoices_list.append({
            "name": inv.get("name", ""),
            "due_date": due_date,
            "amount_total": float(inv.get("amount_total", 0)),
            "amount_residual": residual,
            "payment_state": inv.get("invoice_payment_state", ""),
            "overdue": is_overdue,
        })

    result: dict = {
        "success": True,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "credit_used": round(credit_used, 2),
        "overdue_amount": round(overdue_amount, 2),
        "invoices_pending": len(invoices_list),
        "invoices_overdue": invoices_overdue,
        "invoices": invoices_list,
    }
    if credit_limit is not None:
        result["credit_limit"] = credit_limit
        result["credit_available"] = round(max(credit_limit - credit_used, 0), 2)

    # Iter 77: agregar campos custom Tecnosmart (l10n_ec_sri)
    # Estos son los que el agente realmente debe consultar para clientes
    # con crédito pre-aprobado (e.g. MEPRIGA: $25,000 Crédito Otorgado,
    # terms autorizados: Pago inmediato + 30 días + 1 día + 21 días).
    if credito_otorgado > 0:
        result["credito_otorgado"] = round(credito_otorgado, 2)
    if temp_credit_amount > 0:
        result["credito_temporal"] = round(temp_credit_amount, 2)
        result["credito_temporal_expiracion"] = str(temp_credit_validity) if temp_credit_validity else None
    if credito_disponible is not None:
        try:
            result["credito_disponible"] = round(float(credito_disponible), 2)
        except (TypeError, ValueError):
            pass
    if credito_disponible_sales is not None:
        try:
            result["credito_disponible_con_ventas"] = round(float(credito_disponible_sales), 2)
        except (TypeError, ValueError):
            pass
    if terminos_autorizados:
        result["terminos_pago_autorizados"] = terminos_autorizados
    if vender_con_deudas_vencidas:
        result["vender_con_deudas_vencidas"] = True
    if descuento_partner > 0:
        result["descuento_partner_pct"] = round(descuento_partner, 2)
    if acepta_cheques:
        result["acepta_cheques"] = True

    _log_call("get_customer_credit_status", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_order_delivery_status
# ---------------------------------------------------------------------------

def odoo_get_order_delivery_status(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int | None = None,
    order_name: str | None = None,
) -> dict:
    """Consultar el estado de entrega de un pedido confirmado (sale.order).

    Devuelve que se ha enviado, que falta por enviar, y el estado de cada
    picking (transferencia de almacen) vinculado al pedido.

    Acepta order_id (entero) O order_name (ej: 'VENTA122196'). Si se pasan
    ambos, order_id tiene precedencia.

    Args:
        order_id: ID numerico del sale.order
        order_name: Nombre del pedido (ej: 'VENTA122196')

    Returns {success, order_id, order_name, state, delivery_status,
    invoice_status, amount_total, partner, deliveries: [{name, state,
    state_label, scheduled_date, date_done, picking_type_code, done}]}
    """
    started = time.time()
    log_args = {"order_id": order_id, "order_name": order_name}

    _PICKING_STATE_LABEL = {
        "draft": "borrador",
        "waiting": "esperando",
        "confirmed": "confirmado",
        "assigned": "listo para enviar",
        "done": "enviado",
        "cancel": "cancelado",
    }

    # Resolver order_id desde order_name si hace falta
    if order_id is None and order_name:
        try:
            rows = odoo_search(
                tenant_id, url, db, user, password,
                "sale.order",
                [["name", "=ilike", order_name.strip()]],
                ["id", "name"],
                limit=1,
            )
        except Exception as e:
            err = f"Error buscando pedido '{order_name}': {e}"
            _log_call("get_order_delivery_status", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "search_failed", "error_detail": err}

        if not rows:
            err = f"Pedido '{order_name}' no encontrado"
            _log_call("get_order_delivery_status", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "order_not_found",
                    "error_detail": err, "order_name": order_name}
        order_id = rows[0]["id"] if isinstance(rows[0], dict) else rows[0]

    if not isinstance(order_id, int) or order_id <= 0:
        return {"success": False, "error_code": "invalid_order_id",
                "error_detail": "Se requiere order_id (entero) o order_name valido"}

    # Anti-confusion guard: if order_id was passed directly (not
    # resolved from order_name), reject when it looks like a name suffix.
    if order_name is None:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("get_order_delivery_status", tenant_id, log_args,
                      guard, None, int((time.time() - started) * 1000))
            return guard

    # Leer cabecera del pedido
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            # delivery_status no existe en Odoo 13 core; el módulo custom
            # l10n_ec_sale_delivery agrega state_delivery — leer ambos y
            # usar el que esté disponible.
            ["name", "state", "state_delivery", "invoice_status",
             "picking_ids", "amount_total", "partner_id"],
        )
    except Exception as e:
        err = f"Error leyendo pedido {order_id}: {e}"
        _log_call("get_order_delivery_status", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "order_read_failed", "error_detail": err}

    if not orders:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"Pedido id={order_id} no encontrado", "order_id": order_id}

    order = orders[0]
    picking_ids = order.get("picking_ids") or []
    partner_name = (
        order["partner_id"][1]
        if isinstance(order.get("partner_id"), list)
        else str(order.get("partner_id", ""))
    )

    # Leer pickings vinculados
    deliveries = []
    if picking_ids:
        try:
            pickings = odoo_read(
                tenant_id, url, db, user, password,
                "stock.picking", picking_ids,
                ["name", "state", "scheduled_date", "date_done", "picking_type_code"],
            )
            for p in (pickings or []):
                state = p.get("state", "")
                deliveries.append({
                    "name": p.get("name", ""),
                    "state": state,
                    "state_label": _PICKING_STATE_LABEL.get(state, state),
                    "scheduled_date": p.get("scheduled_date") or "",
                    "date_done": p.get("date_done") or "",
                    "picking_type_code": p.get("picking_type_code") or "",
                    "done": state == "done",
                })
        except Exception as e:
            err = f"Error leyendo pickings {picking_ids}: {e}"
            _log_call("get_order_delivery_status", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "pickings_read_failed", "error_detail": err,
                    "order_id": order_id}

    result = {
        "success": True,
        "order_id": order_id,
        "order_name": order.get("name", ""),
        "state": order.get("state", ""),
        "state_label": _STATE_LABEL.get(order.get("state", ""), order.get("state", "")),
        "delivery_status": order.get("state_delivery") or order.get("delivery_status") or "",
        "invoice_status": order.get("invoice_status") or "",
        "amount_total": float(order.get("amount_total", 0)),
        "partner": partner_name,
        "deliveries_count": len(deliveries),
        "deliveries": deliveries,
    }
    _log_call("get_order_delivery_status", tenant_id, log_args,
              {"order_name": result["order_name"], "deliveries": len(deliveries)},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_my_sales_summary
# ---------------------------------------------------------------------------

def odoo_get_my_sales_summary(
    tenant_id: str, url: str, db: str, user: str, password: str,
    period: str = "month",
) -> dict:
    """Resumen de ventas del vendedor logueado para el periodo indicado.

    Consulta las sale.order confirmadas/hechas del usuario autenticado
    en las credenciales (user/password). Periodos soportados:
      - 'month'  (default): desde el primer dia del mes en curso
      - 'week':  desde el lunes de la semana en curso
      - 'today': solo el dia de hoy

    Usa read_group para los totales agregados y odoo_search para el
    listado de ordenes (max 50, mas recientes primero).

    Args:
        period: 'month' | 'week' | 'today' (default: 'month')

    Returns {success, period, from_date, total_amount, orders_count,
    unique_customers, orders: [{name, date, partner, amount}]}
    """
    from datetime import date as _date, timedelta as _td

    started = time.time()
    log_args = {"period": period}

    # Calcular fecha de inicio del periodo
    today = _date.today()
    if period == "today":
        from_date = today.isoformat()
    elif period == "week":
        # Retroceder al lunes de la semana actual
        from_date = (today - _td(days=today.weekday())).isoformat()
    else:
        # month (default)
        period = "month"
        from_date = today.replace(day=1).isoformat()

    # Autenticar para obtener uid del vendedor logueado
    try:
        import xmlrpc.client as _xrc
        common = _xrc.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/common")
        uid = common.authenticate(db, user, password, {})
        if not uid:
            err = "Autenticacion fallida: no se pudo obtener uid del vendedor"
            _log_call("get_my_sales_summary", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "auth_failed", "error_detail": err}
    except Exception as e:
        err = f"Error autenticando para obtener uid: {e}"
        _log_call("get_my_sales_summary", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "auth_error", "error_detail": err}

    base_domain = [
        ["user_id", "=", uid],
        ["state", "in", ["sale", "done"]],
        ["date_order", ">=", from_date],
    ]

    # Totales via read_group
    total_amount = 0.0
    orders_count = 0
    unique_customers = 0
    try:
        rg_result = odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "read_group",
            [],  # ids ignorado en read_group — se pasa via args
            args=[base_domain, ["amount_total:sum", "partner_id:count_distinct"], []],
        )
        if rg_result and isinstance(rg_result, list) and rg_result[0]:
            g = rg_result[0]
            total_amount = float(g.get("amount_total", 0) or 0)
            orders_count = int(g.get("sale_order_count", g.get("__count", 0)) or 0)
            unique_customers = int(g.get("partner_id_count_distinct",
                                         g.get("partner_id", 0)) or 0)
    except Exception as e:
        # read_group puede fallar en algunas versiones de Odoo — continuar con ordenes
        logger.warning("get_my_sales_summary read_group failed, falling back: %s", e)

    # Listado de ordenes
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order",
            base_domain,
            ["name", "date_order", "partner_id", "amount_total"],
            limit=50,
            order="date_order desc",
        )
    except Exception as e:
        err = f"Error listando ordenes del vendedor: {e}"
        _log_call("get_my_sales_summary", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "orders_search_failed", "error_detail": err}

    orders_list = []
    fallback_total = 0.0
    partners_seen: set = set()
    for r in (rows or []):
        partner_name = (
            r["partner_id"][1]
            if isinstance(r.get("partner_id"), list)
            else str(r.get("partner_id", ""))
        )
        partner_id_val = (
            r["partner_id"][0]
            if isinstance(r.get("partner_id"), list)
            else r.get("partner_id")
        )
        amount = float(r.get("amount_total", 0))
        fallback_total += amount
        if partner_id_val:
            partners_seen.add(partner_id_val)
        orders_list.append({
            "name": r.get("name", ""),
            "date": (r.get("date_order") or "")[:10],  # solo YYYY-MM-DD
            "partner": partner_name,
            "amount": amount,
        })

    # Si read_group no entrego totales, calcular desde las ordenes
    if orders_count == 0 and orders_list:
        orders_count = len(orders_list)
        total_amount = round(fallback_total, 2)
        unique_customers = len(partners_seen)

    result = {
        "success": True,
        "period": period,
        "from_date": from_date,
        "total_amount": round(total_amount, 2),
        "orders_count": orders_count,
        "unique_customers": unique_customers,
        "orders": orders_list,
    }
    _log_call("get_my_sales_summary", tenant_id, log_args,
              {"period": period, "orders_count": orders_count, "total": total_amount},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_stock_by_warehouse
# ---------------------------------------------------------------------------

def odoo_get_stock_by_warehouse(
    tenant_id: str, url: str, db: str, user: str, password: str,
    template_id: int | None = None,
    product_code: str | None = None,
) -> dict:
    """Stock disponible de un producto agrupado por bodega + entradas esperadas.

    Devuelve cuanto stock libre/reservado hay en cada bodega interna y las
    entradas esperadas (purchase.order.line con qty_received < product_qty).

    Args:
        template_id: ID de product.template (preferido).
        product_code: default_code para resolver template_id (alternativa).

    Returns {success, template_id, product_code, product_name, total_available,
             total_reserved, total_free, by_warehouse:[...], incoming_expected:[...]}
    """
    started = time.time()
    log_args = {"template_id": template_id, "product_code": product_code}

    # 1. Resolver template_id si solo se paso product_code
    if not template_id and not product_code:
        err = "Debes pasar template_id o product_code"
        _log_call("get_stock_by_warehouse", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "missing_args", "error_detail": err}

    product_name = ""
    resolved_code = product_code or ""
    if not template_id:
        try:
            tmpls = odoo_search(
                tenant_id, url, db, user, password,
                "product.template",
                [["default_code", "=ilike", product_code.strip()]],
                ["id", "name", "default_code"],
                limit=1,
            )
            if not tmpls:
                tmpls = odoo_search(
                    tenant_id, url, db, user, password,
                    "product.template",
                    [["default_code", "ilike", product_code.strip()]],
                    ["id", "name", "default_code"],
                    limit=1,
                )
        except Exception as e:
            err = f"Error buscando product.template por code={product_code!r}: {e}"
            _log_call("get_stock_by_warehouse", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "template_search_failed",
                    "error_detail": err}
        if not tmpls:
            err = f"No existe producto con default_code={product_code!r}"
            _log_call("get_stock_by_warehouse", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "product_not_found",
                    "error_detail": err, "product_code": product_code}
        template_id = tmpls[0]["id"]
        product_name = tmpls[0].get("name", "")
        resolved_code = tmpls[0].get("default_code") or product_code or ""
    else:
        # Cargar nombre + code para devolverlo en respuesta
        try:
            tmpls = odoo_read(
                tenant_id, url, db, user, password,
                "product.template", [template_id],
                ["name", "default_code"],
            )
            if tmpls:
                product_name = tmpls[0].get("name", "")
                resolved_code = tmpls[0].get("default_code") or ""
        except Exception:
            pass

    # 2. Stock por ubicacion interna (stock.quant)
    try:
        quants = odoo_search(
            tenant_id, url, db, user, password,
            "stock.quant",
            [["product_id.product_tmpl_id", "=", template_id],
             ["location_id.usage", "=", "internal"]],
            ["product_id", "location_id", "quantity", "reserved_quantity"],
            limit=200,
        )
    except Exception as e:
        err = f"Error leyendo stock.quant template={template_id}: {e}"
        _log_call("get_stock_by_warehouse", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "quants_search_failed",
                "error_detail": err, "template_id": template_id}

    # 3. Bodegas para mapear location_id → warehouse name
    try:
        warehouses = odoo_search(
            tenant_id, url, db, user, password,
            "stock.warehouse", [],
            ["name", "lot_stock_id", "code", "view_location_id"],
            limit=50,
        )
    except Exception as e:
        err = f"Error leyendo stock.warehouse: {e}"
        _log_call("get_stock_by_warehouse", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "warehouse_search_failed",
                "error_detail": err}

    # Mapa lot_stock_id → warehouse name
    location_to_wh: dict[int, str] = {}
    for w in warehouses or []:
        ls = w.get("lot_stock_id")
        if isinstance(ls, list) and ls:
            location_to_wh[ls[0]] = w.get("name", "")

    # Para ubicaciones internas que no son el lot_stock_id directo, leer la
    # ubicacion para mapear via location.warehouse_id si existe.
    location_ids = list({
        q["location_id"][0]
        for q in (quants or [])
        if isinstance(q.get("location_id"), list)
    })
    location_to_wh_full: dict[int, str] = dict(location_to_wh)
    if location_ids:
        try:
            locations = odoo_read(
                tenant_id, url, db, user, password,
                "stock.location", location_ids,
                ["name", "warehouse_id", "complete_name"],
            )
            for loc in locations or []:
                lid = loc.get("id")
                if lid in location_to_wh_full:
                    continue
                wh = loc.get("warehouse_id")
                if isinstance(wh, list) and wh:
                    location_to_wh_full[lid] = wh[1]
                else:
                    cname = loc.get("complete_name") or loc.get("name") or ""
                    location_to_wh_full[lid] = cname.split("/")[0] if cname else "Sin bodega"
        except Exception as e:
            logger.warning("get_stock_by_warehouse: error leyendo stock.location: %s", e)

    # 4. Agregar por bodega
    by_warehouse_map: dict[str, dict[str, float]] = {}
    total_available = 0.0
    total_reserved = 0.0
    for q in quants or []:
        loc_field = q.get("location_id")
        loc_id = loc_field[0] if isinstance(loc_field, list) else None
        wh_name = location_to_wh_full.get(loc_id) or (
            loc_field[1] if isinstance(loc_field, list) else "Sin bodega"
        )
        qty = float(q.get("quantity", 0) or 0)
        reserved = float(q.get("reserved_quantity", 0) or 0)
        total_available += qty
        total_reserved += reserved
        bucket = by_warehouse_map.setdefault(
            wh_name, {"available": 0.0, "reserved": 0.0}
        )
        bucket["available"] += qty
        bucket["reserved"] += reserved

    by_warehouse = []
    for wh_name, vals in sorted(by_warehouse_map.items(), key=lambda x: -x[1]["available"]):
        avail = round(vals["available"], 2)
        res = round(vals["reserved"], 2)
        by_warehouse.append({
            "warehouse": wh_name,
            "available": avail,
            "reserved": res,
            "free": round(avail - res, 2),
        })

    # 5. Entradas esperadas (purchase.order.line)
    incoming = []
    try:
        po_lines = odoo_search(
            tenant_id, url, db, user, password,
            "purchase.order.line",
            [["product_id.product_tmpl_id", "=", template_id],
             ["order_id.state", "in", ["purchase", "done"]],
             ["qty_received", "<", "product_qty"]],
            ["product_id", "product_qty", "qty_received", "date_planned",
             "order_id", "price_unit"],
            limit=10,
            order="date_planned asc",
        )
        for ln in po_lines or []:
            order_field = ln.get("order_id")
            po_name = (
                order_field[1] if isinstance(order_field, list) else str(order_field or "")
            )
            qty_ord = float(ln.get("product_qty", 0) or 0)
            qty_rec = float(ln.get("qty_received", 0) or 0)
            incoming.append({
                "po_name": po_name,
                "qty_ordered": qty_ord,
                "qty_received": qty_rec,
                "qty_pending": round(qty_ord - qty_rec, 2),
                "expected_date": (ln.get("date_planned") or "")[:10],
                "unit_cost": float(ln.get("price_unit", 0) or 0),
            })
    except Exception as e:
        # No bloquea — si falla la consulta de PO, devolvemos stock sin entradas
        logger.warning("get_stock_by_warehouse: purchase.order.line failed: %s", e)

    total_available = round(total_available, 2)
    total_reserved = round(total_reserved, 2)
    result = {
        "success": True,
        "template_id": template_id,
        "product_code": resolved_code,
        "product_name": product_name,
        "total_available": total_available,
        "total_reserved": total_reserved,
        "total_free": round(total_available - total_reserved, 2),
        "by_warehouse": by_warehouse,
        "incoming_expected": incoming,
    }
    _log_call("get_stock_by_warehouse", tenant_id, log_args,
              {"template_id": template_id, "warehouses": len(by_warehouse),
               "incoming": len(incoming)},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_pending_quotations
# ---------------------------------------------------------------------------

def odoo_get_pending_quotations(
    tenant_id: str, url: str, db: str, user: str, password: str,
    days_old: int = 7,
    include_expired: bool = True,
) -> dict:
    """Cotizaciones enviadas (state=sent) sin respuesta del vendedor logueado.

    Util para seguimiento: lista cotizaciones donde el vendedor ya envio la
    proforma al cliente pero el cliente no la ha confirmado, con categorizacion
    por estado de validez (expirada / por_vencer / vigente).

    Args:
        days_old: filtrar cotizaciones enviadas hace mas de N dias (default 7).
        include_expired: incluir cotizaciones cuya validity_date ya paso.

    Returns {success, total, expired, expiring_soon, active, quotations:[...]}
    """
    from datetime import date as _date, timedelta as _td

    started = time.time()
    log_args = {"days_old": days_old, "include_expired": include_expired}

    today = _date.today()
    cutoff = (today - _td(days=int(days_old or 0))).isoformat()
    soon_threshold = (today + _td(days=3)).isoformat()
    today_iso = today.isoformat()

    # Autenticar para obtener uid del vendedor logueado
    try:
        import xmlrpc.client as _xrc
        common = _xrc.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/common")
        uid = common.authenticate(db, user, password, {})
        if not uid:
            err = "Autenticacion fallida: no se pudo obtener uid del vendedor"
            _log_call("get_pending_quotations", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "auth_failed", "error_detail": err}
    except Exception as e:
        err = f"Error autenticando para obtener uid: {e}"
        _log_call("get_pending_quotations", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "auth_error", "error_detail": err}

    domain = [
        ["user_id", "=", uid],
        ["state", "=", "sent"],
        ["date_order", "<=", cutoff + " 23:59:59"],
    ]

    try:
        orders = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order", domain,
            ["name", "date_order", "validity_date", "amount_total",
             "partner_id", "state"],
            limit=100,
            order="date_order asc",
        )
    except Exception as e:
        err = f"Error listando cotizaciones pendientes: {e}"
        _log_call("get_pending_quotations", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "search_failed", "error_detail": err}

    quotations = []
    expired_count = 0
    expiring_soon_count = 0
    active_count = 0
    for o in orders or []:
        vdate = (o.get("validity_date") or "")[:10]
        if vdate and vdate < today_iso:
            category = "expirada"
            expired_count += 1
            if not include_expired:
                continue
        elif vdate and vdate < soon_threshold:
            category = "por_vencer"
            expiring_soon_count += 1
        else:
            category = "vigente"
            active_count += 1

        date_sent = (o.get("date_order") or "")[:10]
        try:
            d_sent = _date.fromisoformat(date_sent) if date_sent else today
            days_pending = (today - d_sent).days
        except Exception:
            days_pending = 0

        partner = o.get("partner_id")
        partner_name = (
            partner[1] if isinstance(partner, list) else str(partner or "")
        )
        quotations.append({
            "order_id": o.get("id"),
            "name": o.get("name", ""),
            "date_sent": date_sent,
            "validity_date": vdate,
            "days_pending": days_pending,
            "amount_total": float(o.get("amount_total", 0) or 0),
            "partner": partner_name,
            "status": category,
        })

    result = {
        "success": True,
        "total": len(quotations),
        "expired": expired_count,
        "expiring_soon": expiring_soon_count,
        "active": active_count,
        "quotations": quotations,
    }
    _log_call("get_pending_quotations", tenant_id, log_args,
              {"total": len(quotations), "expired": expired_count},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: duplicate_quotation
# ---------------------------------------------------------------------------

def odoo_duplicate_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int | None = None,
    order_name: str | None = None,
) -> dict:
    """Duplicar una cotizacion existente como nuevo borrador.

    Util cuando el cliente quiere repetir un pedido similar — clona la cabecera
    y todas las lineas como sale.order en estado draft. La nueva cotizacion
    puede modificarse antes de enviarse.

    Args:
        order_id: ID de la cotizacion a duplicar (preferido).
        order_name: name humano (ej. 'VENTA122196'); se resuelve a order_id.

    Returns {success, source_order_id, source_order_name, new_order_id,
             new_order_name, partner, amount_total, state, message}
    """
    started = time.time()
    log_args = {"order_id": order_id, "order_name": order_name}

    if not order_id and not order_name:
        err = "Debes pasar order_id o order_name"
        _log_call("duplicate_quotation", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "missing_args", "error_detail": err}

    # Resolver order_name → order_id si hace falta
    name_resolved = False
    if not order_id:
        resolved = odoo_find_quotation_by_name(
            tenant_id, url, db, user, password, order_name,
        )
        if not resolved.get("success"):
            _log_call("duplicate_quotation", tenant_id, log_args, None,
                      resolved.get("error_detail", "name resolution failed"),
                      int((time.time() - started) * 1000))
            return resolved
        order_id = resolved["order_id"]
        name_resolved = True

    # Anti-confusion guard: when order_id was passed directly (not
    # resolved from order_name above), reject if it looks like a name
    # suffix.
    if not name_resolved and isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("duplicate_quotation", tenant_id, log_args, guard,
                      None, int((time.time() - started) * 1000))
            return guard

    # Verificar que la cotizacion existe
    try:
        source = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            ["name", "state", "partner_id", "amount_total", "order_line"],
        )
    except Exception as e:
        err = f"Error leyendo sale.order {order_id}: {e}"
        _log_call("duplicate_quotation", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "source_read_failed",
                "error_detail": err, "order_id": order_id}

    if not source:
        err = f"No existe sale.order id={order_id}"
        _log_call("duplicate_quotation", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "order_not_found",
                "error_detail": err, "order_id": order_id}

    src = source[0]
    source_name = src.get("name", "")

    # Duplicar via copy()
    try:
        new_id = odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "copy",
            [order_id], args=[{}],
        )
    except Exception as e:
        err = f"Error duplicando sale.order {order_id}: {e}"
        _log_call("duplicate_quotation", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "copy_failed",
                "error_detail": err, "order_id": order_id}

    # Algunas versiones devuelven [new_id], otras int directo
    if isinstance(new_id, list):
        new_id = new_id[0] if new_id else None
    if not new_id:
        err = f"copy() no devolvio un ID valido (returned={new_id!r})"
        _log_call("duplicate_quotation", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "copy_no_id",
                "error_detail": err, "order_id": order_id}

    # Leer la nueva cotizacion
    try:
        new_orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [new_id],
            ["name", "state", "partner_id", "amount_total"],
        )
    except Exception as e:
        err = f"Cotizacion duplicada (id={new_id}) pero no pudo releerse: {e}"
        _log_call("duplicate_quotation", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "new_read_failed",
                "error_detail": err, "new_order_id": new_id}

    if not new_orders:
        err = f"Cotizacion duplicada (id={new_id}) pero no pudo releerse"
        _log_call("duplicate_quotation", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "new_not_found",
                "error_detail": err, "new_order_id": new_id}

    new = new_orders[0]
    partner = new.get("partner_id")
    partner_name = (
        partner[1] if isinstance(partner, list) else str(partner or "")
    )
    result = {
        "success": True,
        "source_order_id": order_id,
        "source_order_name": source_name,
        "new_order_id": new_id,
        "new_order_name": new.get("name", ""),
        "partner": partner_name,
        "amount_total": float(new.get("amount_total", 0) or 0),
        "state": new.get("state", ""),
        "message": (
            "Cotizacion duplicada exitosamente. Puedes modificarla antes "
            "de enviarla."
        ),
    }
    _log_call("duplicate_quotation", tenant_id, log_args,
              {"source": source_name, "new_id": new_id,
               "new_name": new.get("name")},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_my_crm_opportunities
# ---------------------------------------------------------------------------

def odoo_get_my_crm_opportunities(
    tenant_id: str, url: str, db: str, user: str, password: str,
    stage: str | None = None,
    limit: int = 10,
) -> dict:
    """Oportunidades CRM activas del vendedor logueado — pipeline + seguimiento.

    Lista crm.lead con type='opportunity' del usuario autenticado, ordenadas
    por fecha limite ascendente. Calcula revenue total (planned) y revenue
    ponderado por probabilidad (forecast).

    Args:
        stage: filtrar por nombre de etapa (opcional, ilike).
        limit: maximo de oportunidades a retornar (default 10).

    Returns {success, total_opportunities, total_planned_revenue,
             weighted_revenue, opportunities:[...]}
    """
    started = time.time()
    log_args = {"stage": stage, "limit": limit}

    # Autenticar para obtener uid
    try:
        import xmlrpc.client as _xrc
        common = _xrc.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/common")
        uid = common.authenticate(db, user, password, {})
        if not uid:
            err = "Autenticacion fallida: no se pudo obtener uid del vendedor"
            _log_call("get_my_crm_opportunities", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "auth_failed", "error_detail": err}
    except Exception as e:
        err = f"Error autenticando para obtener uid: {e}"
        _log_call("get_my_crm_opportunities", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "auth_error", "error_detail": err}

    domain = [
        ["user_id", "=", uid],
        ["active", "=", True],
        ["type", "=", "opportunity"],
    ]
    if stage:
        domain.append(["stage_id.name", "ilike", stage.strip()])

    try:
        opportunities = odoo_search(
            tenant_id, url, db, user, password,
            "crm.lead", domain,
            ["name", "partner_id", "stage_id", "probability",
             "planned_revenue", "expected_revenue", "date_deadline",
             "date_open", "priority"],
            limit=int(limit or 10),
            order="date_deadline asc",
        )
    except Exception as e:
        # Si crm no esta instalado o el modelo no existe, devolver error claro
        err = f"Error listando crm.lead: {e}"
        _log_call("get_my_crm_opportunities", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "crm_search_failed",
                "error_detail": err}

    opps_list = []
    total_planned = 0.0
    weighted = 0.0
    for o in opportunities or []:
        # planned_revenue: en Odoo 13 puede llamarse 'planned_revenue' o
        # 'expected_revenue'; preferir el primero que tenga valor.
        revenue = o.get("planned_revenue")
        if revenue in (None, False, 0):
            revenue = o.get("expected_revenue", 0)
        revenue = float(revenue or 0)
        prob = float(o.get("probability", 0) or 0)
        total_planned += revenue
        weighted += revenue * prob / 100.0

        partner = o.get("partner_id")
        partner_name = (
            partner[1] if isinstance(partner, list)
            else (str(partner) if partner else "")
        )
        stage_field = o.get("stage_id")
        stage_name = (
            stage_field[1] if isinstance(stage_field, list)
            else (str(stage_field) if stage_field else "")
        )
        opps_list.append({
            "lead_id": o.get("id"),
            "name": o.get("name", ""),
            "partner": partner_name,
            "stage": stage_name,
            "probability": prob,
            "planned_revenue": round(revenue, 2),
            "deadline": (o.get("date_deadline") or "")[:10],
            "date_open": (o.get("date_open") or "")[:10],
            "priority": str(o.get("priority") or ""),
        })

    result = {
        "success": True,
        "total_opportunities": len(opps_list),
        "total_planned_revenue": round(total_planned, 2),
        "weighted_revenue": round(weighted, 2),
        "opportunities": opps_list,
    }
    _log_call("get_my_crm_opportunities", tenant_id, log_args,
              {"total": len(opps_list), "planned": total_planned},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_pricelist_price
# ---------------------------------------------------------------------------

def odoo_get_pricelist_price(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
    template_id: int,
    quantity: float = 1,
) -> dict:
    """Obtener el precio efectivo de venta de un producto para un cliente
    especifico segun su lista de precios configurada en Odoo.

    Lee `property_product_pricelist` del partner (Many2one a product.pricelist)
    y llama a `pricelist.get_product_price(product, qty, partner)` para
    obtener el precio resuelto. Si el partner no tiene pricelist configurada,
    devuelve el `list_price` del template.

    Args:
        partner_id: ID del cliente (res.partner)
        template_id: ID del producto (product.template) — el que devuelve search_products
        quantity: Cantidad a cotizar (default 1)

    Returns {success, partner_id, partner_name, template_id, product_code,
    product_name, quantity, list_price, pricelist_price, pricelist_name,
    discount_applied}.
    """
    started = time.time()
    log_args = {
        "partner_id": partner_id,
        "template_id": template_id,
        "quantity": quantity,
    }

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id",
                "error_detail": "partner_id debe ser entero positivo"}
    if not isinstance(template_id, int) or template_id <= 0:
        return {"success": False, "error_code": "invalid_template_id",
                "error_detail": "template_id debe ser entero positivo"}
    try:
        qty = float(quantity) if quantity else 1.0
    except (TypeError, ValueError):
        qty = 1.0
    if qty <= 0:
        qty = 1.0

    # 1. Leer partner (nombre + pricelist)
    try:
        partners = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [partner_id],
            ["id", "name", "property_product_pricelist"],
        )
    except Exception as e:
        err = f"Error leyendo partner {partner_id}: {e}"
        _log_call("get_pricelist_price", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_read_failed",
                "error_detail": err}

    if not partners:
        err = f"Partner id={partner_id} no encontrado"
        _log_call("get_pricelist_price", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_not_found",
                "error_detail": err, "partner_id": partner_id}

    partner = partners[0]
    partner_name = partner.get("name", "")
    pricelist_raw = partner.get("property_product_pricelist")
    if isinstance(pricelist_raw, list) and pricelist_raw:
        pricelist_id = pricelist_raw[0]
        pricelist_name = pricelist_raw[1] if len(pricelist_raw) > 1 else ""
    else:
        pricelist_id = pricelist_raw if isinstance(pricelist_raw, int) else None
        pricelist_name = ""

    # 2. Leer template (precio base + nombre + codigo)
    try:
        templates = odoo_read(
            tenant_id, url, db, user, password,
            "product.template", [template_id],
            ["id", "name", "default_code", "list_price"],
        )
    except Exception as e:
        err = f"Error leyendo template {template_id}: {e}"
        _log_call("get_pricelist_price", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "template_read_failed",
                "error_detail": err}

    if not templates:
        err = f"Producto template id={template_id} no encontrado"
        _log_call("get_pricelist_price", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "template_not_found",
                "error_detail": err, "template_id": template_id}

    tpl = templates[0]
    list_price = float(tpl.get("list_price", 0) or 0)
    product_code = tpl.get("default_code") or ""
    product_name = tpl.get("name", "")

    # 3. Calcular precio efectivo via product.product.read (variant)
    # con context.
    # Iter 76b fix A: validado server-side 2026-05-22:
    #   product.product.read(['price'], context={pricelist, partner}) →
    #     PSU007 (variant=3163) → $19.99 ✅ (cascade pricelist aplicado)
    #     RAM0043 (variant=20481) → $135.558 ✅
    # vs product.template.read da SOLO list_price (no cascade).
    pricelist_price = list_price
    if pricelist_id:
        try:
            # Resolve template_id → variant_id (default variant)
            variants = odoo_search(
                tenant_id, url, db, user, password,
                "product.product",
                [["product_tmpl_id", "=", template_id], ["active", "=", True]],
                ["id"], 1,
            )
            if variants:
                variant_id = variants[0]["id"]
                ctx = {"pricelist": pricelist_id, "partner": partner_id, "quantity": qty}
                rows = odoo_call_method(
                    tenant_id, url, db, user, password,
                    "product.product", "read",
                    [variant_id],
                    [["id", "lst_price", "price"]],
                    {"context": ctx},
                )
                if rows and isinstance(rows, list) and rows[0].get("price") is not None:
                    pricelist_price = float(rows[0]["price"])
        except Exception as e:
            logger.warning("get_pricelist_price: product.product.read with context "
                           "failed pricelist=%s template=%s err=%s",
                           pricelist_id, template_id, e)

    discount_applied = 0.0
    if list_price > 0:
        discount_applied = round((1 - (pricelist_price / list_price)) * 100, 2)

    result = {
        "success": True,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "template_id": template_id,
        "product_code": product_code,
        "product_name": product_name,
        "quantity": qty,
        "list_price": round(list_price, 2),
        "pricelist_price": round(pricelist_price, 2),
        "pricelist_id": pricelist_id,
        "pricelist_name": pricelist_name,
        "discount_applied": discount_applied,
    }
    _log_call("get_pricelist_price", tenant_id, log_args,
              {"template_id": template_id,
               "pricelist_price": result["pricelist_price"],
               "discount_applied": discount_applied},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_quotation_margin
# ---------------------------------------------------------------------------

def odoo_get_quotation_margin(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int | None = None,
    order_name: str | None = None,
) -> dict:
    """Calcular el margen de ganancia de una cotizacion o pedido.

    Usa los campos del modulo nativo `sale_margin` (Odoo 13):
      - sale.order.line.purchase_price (Float, costo unitario)
      - sale.order.line.margin (Float, computed = price_subtotal - cost*qty)
      - sale.order.margin (Float, computed = sum(line.margin))

    Si `sale_margin` no esta instalado, hace fallback al calculo manual con
    purchase_price (que en Odoo 13 forma parte del modulo, pero por
    defensividad se calcula tambien).

    Acepta order_id (entero) O order_name (ej: 'VENTA122196'). Si se pasan
    ambos, order_id tiene precedencia.

    Args:
        order_id: ID numerico del sale.order
        order_name: Nombre del pedido/cotizacion

    Returns {success, order_id, order_name, partner, amount_total,
    amount_untaxed, total_margin, margin_pct, lines: [{product, qty,
    price_unit, cost_unit, subtotal, margin, margin_pct, discount}]}.
    """
    started = time.time()
    log_args = {"order_id": order_id, "order_name": order_name}

    # Resolver order_id desde order_name si hace falta
    if order_id is None and order_name:
        try:
            rows = odoo_search(
                tenant_id, url, db, user, password,
                "sale.order",
                [["name", "=ilike", order_name.strip()]],
                ["id", "name"],
                limit=1,
            )
        except Exception as e:
            err = f"Error buscando pedido '{order_name}': {e}"
            _log_call("get_quotation_margin", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "search_failed",
                    "error_detail": err}
        if not rows:
            err = f"Pedido '{order_name}' no encontrado"
            _log_call("get_quotation_margin", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "order_not_found",
                    "error_detail": err, "order_name": order_name}
        order_id = rows[0]["id"] if isinstance(rows[0], dict) else rows[0]

    if not isinstance(order_id, int) or order_id <= 0:
        return {"success": False, "error_code": "invalid_order_id",
                "error_detail": "Se requiere order_id (entero) o order_name valido"}

    # Anti-confusion guard: only when caller passed order_id directly.
    if order_name is None:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("get_quotation_margin", tenant_id, log_args, guard,
                      None, int((time.time() - started) * 1000))
            return guard

    # Leer cabecera. Pedimos `margin` directamente; si el modulo sale_margin
    # no esta instalado, Odoo lanza error y caemos al fallback.
    header_fields_full = [
        "name", "state", "partner_id", "amount_total", "amount_untaxed",
        "order_line", "margin",
    ]
    header_fields_min = [
        "name", "state", "partner_id", "amount_total", "amount_untaxed",
        "order_line",
    ]
    has_margin_field = True
    try:
        orders = odoo_read(
            tenant_id, url, db, user, password,
            "sale.order", [order_id], header_fields_full,
        )
    except Exception:
        has_margin_field = False
        try:
            orders = odoo_read(
                tenant_id, url, db, user, password,
                "sale.order", [order_id], header_fields_min,
            )
        except Exception as e:
            err = f"Error leyendo pedido {order_id}: {e}"
            _log_call("get_quotation_margin", tenant_id, log_args, None, err,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "order_read_failed",
                    "error_detail": err}

    if not orders:
        return {"success": False, "error_code": "order_not_found",
                "error_detail": f"Pedido id={order_id} no encontrado",
                "order_id": order_id}

    order = orders[0]
    line_ids = order.get("order_line") or []
    partner_name = (
        order["partner_id"][1]
        if isinstance(order.get("partner_id"), list)
        else str(order.get("partner_id", ""))
    )

    # Leer lineas con purchase_price + margin (con fallback)
    line_fields_full = [
        "product_id", "name", "product_uom_qty", "price_unit",
        "price_subtotal", "purchase_price", "margin", "discount",
    ]
    line_fields_min = [
        "product_id", "name", "product_uom_qty", "price_unit",
        "price_subtotal", "discount",
    ]
    lines: list[dict] = []
    if line_ids:
        try:
            lines = odoo_read(
                tenant_id, url, db, user, password,
                "sale.order.line", line_ids, line_fields_full,
            ) or []
        except Exception:
            # sale_margin no instalado — leer sin purchase_price/margin
            try:
                lines = odoo_read(
                    tenant_id, url, db, user, password,
                    "sale.order.line", line_ids, line_fields_min,
                ) or []
            except Exception as e:
                err = f"Error leyendo lineas {line_ids}: {e}"
                _log_call("get_quotation_margin", tenant_id, log_args, None, err,
                          int((time.time() - started) * 1000))
                return {"success": False, "error_code": "lines_read_failed",
                        "error_detail": err, "order_id": order_id}

    lines_out: list[dict] = []
    total_margin_calc = 0.0
    total_subtotal = 0.0
    for ln in lines:
        product_name = (
            ln["product_id"][1]
            if isinstance(ln.get("product_id"), list)
            else str(ln.get("product_id") or ln.get("name") or "")
        )
        qty = float(ln.get("product_uom_qty", 0) or 0)
        price_unit = float(ln.get("price_unit", 0) or 0)
        subtotal = float(ln.get("price_subtotal", 0) or 0)
        cost_unit = float(ln.get("purchase_price", 0) or 0)
        # Si margin viene del modulo, usarlo; si no, calcular
        line_margin_raw = ln.get("margin")
        if line_margin_raw not in (None, False):
            line_margin = float(line_margin_raw)
        else:
            line_margin = subtotal - (cost_unit * qty)
        line_margin_pct = (
            round((line_margin / subtotal) * 100, 2) if subtotal > 0 else 0.0
        )
        total_margin_calc += line_margin
        total_subtotal += subtotal
        lines_out.append({
            "product": product_name,
            "qty": qty,
            "price_unit": round(price_unit, 2),
            "cost_unit": round(cost_unit, 2),
            "subtotal": round(subtotal, 2),
            "margin": round(line_margin, 2),
            "margin_pct": line_margin_pct,
            "discount": float(ln.get("discount", 0) or 0),
        })

    # Margen total: preferir el campo del header si existe
    header_margin_raw = order.get("margin") if has_margin_field else None
    if header_margin_raw not in (None, False):
        try:
            total_margin = float(header_margin_raw)
        except (TypeError, ValueError):
            total_margin = total_margin_calc
    else:
        total_margin = total_margin_calc

    margin_pct = (
        round((total_margin / total_subtotal) * 100, 2)
        if total_subtotal > 0 else 0.0
    )

    result = {
        "success": True,
        "order_id": order_id,
        "order_name": order.get("name", ""),
        "state": order.get("state", ""),
        "state_label": _STATE_LABEL.get(order.get("state", ""), order.get("state", "")),
        "partner": partner_name,
        "amount_total": float(order.get("amount_total", 0) or 0),
        "amount_untaxed": float(order.get("amount_untaxed", 0) or 0),
        "total_margin": round(total_margin, 2),
        "margin_pct": margin_pct,
        "lines_count": len(lines_out),
        "lines": lines_out,
    }
    _log_call("get_quotation_margin", tenant_id, log_args,
              {"order_name": result["order_name"],
               "total_margin": result["total_margin"],
               "margin_pct": margin_pct},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: get_customer_purchase_history
# ---------------------------------------------------------------------------

def odoo_get_customer_purchase_history(
    tenant_id: str, url: str, db: str, user: str, password: str,
    partner_id: int,
    limit: int = 10,
    year: int | None = None,
) -> dict:
    """Historial de compras de un cliente — ordenes pasadas, productos
    frecuentes y ticket promedio.

    Devuelve las ultimas N ordenes confirmadas/done del cliente (limit) y
    agrega:
      - total comprado en el periodo (year)
      - ticket promedio (total / orders_count)
      - top productos (mas comprados por cantidad acumulada)

    Args:
        partner_id: ID del cliente (res.partner)
        limit: numero de ordenes recientes a devolver (default 10)
        year: anio del periodo a analizar (default: anio actual)

    Returns {success, partner_id, partner_name, period, orders_count,
    total_amount, avg_ticket, top_products: [{code, name, total_qty,
    total_amount}], recent_orders: [{name, date, amount, state}]}.
    """
    from datetime import date as _date

    started = time.time()
    log_args = {"partner_id": partner_id, "limit": limit, "year": year}

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id",
                "error_detail": "partner_id debe ser entero positivo"}

    try:
        limit_int = int(limit) if limit else 10
    except (TypeError, ValueError):
        limit_int = 10
    if limit_int <= 0:
        limit_int = 10
    if limit_int > 100:
        limit_int = 100

    target_year = year or _date.today().year
    try:
        target_year = int(target_year)
    except (TypeError, ValueError):
        target_year = _date.today().year
    period_from = f"{target_year}-01-01"
    period_to = f"{target_year}-12-31 23:59:59"

    # 1. Verificar que el partner existe
    try:
        partners = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [partner_id], ["id", "name"],
        )
    except Exception as e:
        err = f"Error leyendo partner {partner_id}: {e}"
        _log_call("get_customer_purchase_history", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_read_failed",
                "error_detail": err}

    if not partners:
        err = f"Partner id={partner_id} no encontrado"
        _log_call("get_customer_purchase_history", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_not_found",
                "error_detail": err, "partner_id": partner_id}
    partner_name = partners[0].get("name", "")

    # 2. Listado de ordenes confirmadas/done en el periodo
    base_domain = [
        ["partner_id", "child_of", partner_id],
        ["state", "in", ["sale", "done"]],
        ["date_order", ">=", period_from],
        ["date_order", "<=", period_to],
    ]
    try:
        orders = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order",
            base_domain,
            ["id", "name", "date_order", "amount_total", "order_line", "state"],
            limit=200,  # leer hasta 200 para agregar; mostrar solo limit_int
            order="date_order desc",
        )
    except Exception as e:
        err = f"Error buscando ordenes del cliente: {e}"
        _log_call("get_customer_purchase_history", tenant_id, log_args, None, err,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "orders_search_failed",
                "error_detail": err}

    orders = orders or []
    orders_count = len(orders)
    total_amount = 0.0  # total cotizado (sale.order.amount_total)
    line_ids: list[int] = []
    for o in orders:
        total_amount += float(o.get("amount_total", 0) or 0)
        line_ids.extend(o.get("order_line") or [])

    avg_ticket = round(total_amount / orders_count, 2) if orders_count else 0.0
    # Iter89 owner-audit 2026-05-25: agregar también `total_invoiced` =
    # suma real facturada (untaxed_amount_invoiced de todas las líneas).
    # Esto se computa dentro del bloque siguiente cuando leemos las
    # líneas con qty_invoiced; arrancamos en 0 acá.
    total_invoiced = 0.0

    # 3. Top productos — separar PURCHASED (qty_invoiced > 0) vs
    #    QUOTED_ONLY (cotizado pero no facturado).
    #
    # Lógica honesta para el cliente:
    #   - Una sale.order.line con qty_invoiced > 0 representa COMPRA real
    #     (Odoo ya neta refunds/cancelaciones automáticamente).
    #   - Una línea con qty_invoiced < product_uom_qty tiene una parte que
    #     todavía solo está cotizada (no facturada).
    #   - El cliente debe ver claramente: "compraste X" vs "cotizaste Y".
    #
    # Audit 2026-05-25 (owner feedback): el bot decía "Top productos
    # comprados" cuando solo había sale.order confirmadas. Owner aclaró:
    # "que esté en sale.order.line no significa que lo haya comprado;
    # si está en account.move.line confirmada, sí lo ha comprado". Usamos
    # qty_invoiced (computado por Odoo sumando account.move.line linked
    # menos refunds) como fuente de verdad de "compró".
    top_products: list[dict] = []
    top_products_quoted_only: list[dict] = []
    # Tracking de las orders donde aparece cada producto quoted_only —
    # para que el formatter pueda decir "cotización VENTA xxx".
    quoted_only_orders: dict[int, set[str]] = {}
    if line_ids:
        try:
            lines = odoo_read(
                tenant_id, url, db, user, password,
                "sale.order.line", line_ids,
                ["product_id", "product_uom_qty", "qty_invoiced",
                 "price_subtotal", "untaxed_amount_invoiced", "order_id"],
            ) or []
        except Exception as e:
            # No bloquear: si falla, devolvemos sin top_products
            logger.warning("get_customer_purchase_history: lines read failed: %s", e)
            lines = []

        # Map order_id → order_name para tagging de quoted_only
        order_name_by_id: dict[int, str] = {}
        for o in orders:
            try:
                order_name_by_id[int(o.get("id"))] = o.get("name", "") or ""
            except (TypeError, ValueError):
                continue

        agg_purchased: dict[int, dict] = {}
        agg_quoted_only: dict[int, dict] = {}
        for ln in lines:
            pid_raw = ln.get("product_id")
            if isinstance(pid_raw, list) and pid_raw:
                pid = pid_raw[0]
                pname = pid_raw[1] if len(pid_raw) > 1 else ""
            elif isinstance(pid_raw, int):
                pid = pid_raw
                pname = ""
            else:
                continue

            qty_quoted = float(ln.get("product_uom_qty", 0) or 0)
            qty_invoiced = float(ln.get("qty_invoiced", 0) or 0)
            amount_invoiced = float(ln.get("untaxed_amount_invoiced", 0) or 0)
            qty_pending = max(0.0, qty_quoted - qty_invoiced)
            total_invoiced += amount_invoiced

            if qty_invoiced > 0:
                entry = agg_purchased.setdefault(pid, {
                    "product_id": pid, "name": pname,
                    "total_qty": 0.0, "total_amount": 0.0,
                    "total_qty_quoted": 0.0,  # cuánto se cotizó para esa misma compra
                })
                entry["total_qty"] += qty_invoiced
                entry["total_amount"] += amount_invoiced
                entry["total_qty_quoted"] += qty_quoted

            if qty_pending > 0:
                entry_q = agg_quoted_only.setdefault(pid, {
                    "product_id": pid, "name": pname,
                    "total_qty": 0.0, "total_amount": 0.0,
                })
                entry_q["total_qty"] += qty_pending
                # Usar precio_subtotal proporcional cuando parcialmente facturada
                price_subtotal = float(ln.get("price_subtotal", 0) or 0)
                if qty_quoted > 0:
                    entry_q["total_amount"] += price_subtotal * (qty_pending / qty_quoted)
                # Tagging de orden
                oid_raw = ln.get("order_id")
                if isinstance(oid_raw, list) and oid_raw:
                    oname = order_name_by_id.get(oid_raw[0], "")
                elif isinstance(oid_raw, int):
                    oname = order_name_by_id.get(oid_raw, "")
                else:
                    oname = ""
                if oname:
                    quoted_only_orders.setdefault(pid, set()).add(oname)

        # Resolver default_code para TODOS los productos involucrados (top 10 each).
        ranked_purchased = sorted(
            agg_purchased.values(), key=lambda x: x["total_qty"], reverse=True,
        )[:10]
        ranked_quoted_only = sorted(
            agg_quoted_only.values(), key=lambda x: x["total_qty"], reverse=True,
        )[:10]
        all_ranked_pids = list({
            r["product_id"] for r in ranked_purchased + ranked_quoted_only
        })
        code_by_id: dict[int, str] = {}
        name_by_id: dict[int, str] = {}
        if all_ranked_pids:
            try:
                prods = odoo_read(
                    tenant_id, url, db, user, password,
                    "product.product",
                    all_ranked_pids,
                    ["id", "default_code", "name"],
                )
                code_by_id = {
                    p["id"]: (p.get("default_code") or "") for p in (prods or [])
                }
                name_by_id = {
                    p["id"]: p.get("name", "") for p in (prods or [])
                }
            except Exception as e:
                logger.warning("get_customer_purchase_history: product read failed: %s", e)

        for r in ranked_purchased:
            top_products.append({
                "product_id": r["product_id"],
                "code": code_by_id.get(r["product_id"], ""),
                "name": name_by_id.get(r["product_id"], r["name"]),
                "total_qty": round(r["total_qty"], 2),  # cantidad facturada (real)
                "total_amount": round(r["total_amount"], 2),  # monto facturado neto
                "total_qty_quoted": round(r["total_qty_quoted"], 2),  # contexto
            })

        for r in ranked_quoted_only:
            entry = {
                "product_id": r["product_id"],
                "code": code_by_id.get(r["product_id"], ""),
                "name": name_by_id.get(r["product_id"], r["name"]),
                "total_qty": round(r["total_qty"], 2),  # pendiente de facturar
                "total_amount": round(r["total_amount"], 2),
            }
            # Lista de cotizaciones donde aparece (ej. ["VENTA123598", "VENTA123599"])
            orders_for_pid = sorted(quoted_only_orders.get(r["product_id"], []))
            if orders_for_pid:
                entry["quotations"] = orders_for_pid[:5]
            top_products_quoted_only.append(entry)

    # 4. Recent orders (limit_int mas recientes)
    recent_orders = []
    for o in orders[:limit_int]:
        recent_orders.append({
            "name": o.get("name", ""),
            "date": (o.get("date_order") or "")[:10],
            "amount": float(o.get("amount_total", 0) or 0),
            "state": o.get("state", ""),
        })

    # Iter89 owner-audit: response shape ampliado para distinguir
    # 'comprado' (facturado) de 'cotizado pero no facturado'.
    # Backward compat: el viejo `top_products` ahora SOLO contiene
    # productos realmente facturados (no más mentir al cliente).
    result = {
        "success": True,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "period": str(target_year),
        "orders_count": orders_count,
        "total_amount": round(total_amount, 2),      # total cotizado (sale.order.amount_total)
        "total_invoiced": round(total_invoiced, 2),  # total facturado neto (sum untaxed_amount_invoiced)
        "avg_ticket": avg_ticket,
        "top_products": top_products,                # FACTURADOS (qty_invoiced)
        "top_products_quoted_only": top_products_quoted_only,  # NO facturados (qty - qty_invoiced)
        "recent_orders": recent_orders,
        "note": (
            "`top_products` son productos FACTURADOS (cliente sí los compró). "
            "`top_products_quoted_only` son productos en cotizaciones que aún "
            "NO se han facturado — NO afirmes que el cliente los compró."
        ),
    }
    _log_call("get_customer_purchase_history", tenant_id, log_args,
              {"period": str(target_year),
               "orders_count": orders_count,
               "total_amount": result["total_amount"],
               "total_invoiced": result["total_invoiced"],
               "purchased_count": len(top_products),
               "quoted_only_count": len(top_products_quoted_only)},
              None, int((time.time() - started) * 1000))
    return result


# ---------------------------------------------------------------------------
# Tool: sign_quotation — write digital signature to a sale.order
# ---------------------------------------------------------------------------

def odoo_sign_quotation(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
    signature: str,
    signed_by_name: str,
    auto_confirm: bool = True,
) -> dict:
    """Sign a quotation by writing signature/signed_by/signed_on to sale.order.

    Replicates the behavior of /my/orders/<id>/accept (Odoo portal):
    1. Validates state in (draft, sent) and signature is null.
    2. Writes signature (base64 PNG, no data:image prefix), signed_by, signed_on=now().
    3. If auto_confirm=True and the quotation does not require payment,
       calls action_confirm() to move state to 'sale'.

    Args:
        order_id: sale.order ID.
        signature: PNG base64 SIN el prefijo "data:image/png;base64,".
        signed_by_name: Full name of the signer (>= 3 chars).
        auto_confirm: If True, confirm the order after signing.

    Returns:
        On success::

            {
                "success": True,
                "order_id": int,
                "name": str,
                "state": str,
                "signed_by": str,
                "signed_on": str,  # ISO datetime
                "action": "signed" | "signed_and_confirmed",
            }

        On failure::

            {"success": False, "error_code": str, "error_detail": str}
    """
    import base64
    from datetime import datetime, timezone

    started = time.time()
    log_args = {
        "order_id": order_id,
        "signed_by_name": signed_by_name,
        "auto_confirm": auto_confirm,
        "signature_len": len(signature) if isinstance(signature, str) else 0,
    }

    # 1) Validate signed_by_name length.
    if not isinstance(signed_by_name, str) or len(signed_by_name.strip()) < 3:
        result = {
            "success": False,
            "error_code": "invalid_signed_by_name",
            "error_detail": "signed_by_name debe tener al menos 3 caracteres.",
        }
        _log_call("sign_quotation", tenant_id, log_args, None,
                  result["error_detail"], int((time.time() - started) * 1000))
        return result
    signed_by_clean = signed_by_name.strip()

    # 2) Validate signature is non-empty base64 (no data: prefix).
    if not isinstance(signature, str) or not signature.strip():
        result = {
            "success": False,
            "error_code": "invalid_signature",
            "error_detail": "signature vacia. Debe ser PNG base64 sin el prefijo 'data:image/...'.",
        }
        _log_call("sign_quotation", tenant_id, log_args, None,
                  result["error_detail"], int((time.time() - started) * 1000))
        return result
    if signature.startswith("data:"):
        result = {
            "success": False,
            "error_code": "invalid_signature",
            "error_detail": (
                "signature incluye prefijo 'data:image/...'. "
                "Envia solo el base64 puro (lo que va despues de la coma)."
            ),
        }
        _log_call("sign_quotation", tenant_id, log_args, None,
                  result["error_detail"], int((time.time() - started) * 1000))
        return result
    try:
        # validate=True rejects non base64 chars; we only need a sanity check.
        base64.b64decode(signature, validate=True)
    except Exception as e:
        result = {
            "success": False,
            "error_code": "invalid_signature",
            "error_detail": f"signature no es base64 valido: {e}",
        }
        _log_call("sign_quotation", tenant_id, log_args, None,
                  result["error_detail"], int((time.time() - started) * 1000))
        return result

    # Anti-confusion guard: reject when order_id looks like a name suffix.
    if isinstance(order_id, int) and order_id > 0:
        guard = _guard_order_id_vs_name_suffix(
            tenant_id, url, db, user, password, order_id,
        )
        if guard is not None:
            _log_call("sign_quotation", tenant_id, log_args, guard, None,
                      int((time.time() - started) * 1000))
            return guard

    # 3) Read sale.order — verify it exists and is in a signable state.
    order = _read_sale_order(
        tenant_id, url, db, user, password, order_id,
        ["name", "state", "signature", "amount_total", "partner_id",
         "require_signature"],
    )
    if not order:
        result = {
            "success": False,
            "error_code": "order_not_found",
            "error_detail": f"sale.order {order_id} no existe",
        }
        _log_call("sign_quotation", tenant_id, log_args, None,
                  result["error_detail"], int((time.time() - started) * 1000))
        return result

    allowed_states = {"draft", "sent"}
    if order.get("state") not in allowed_states:
        result = {
            "success": False,
            "error_code": "invalid_state",
            "error_detail": (
                f"La cotizacion {order.get('name', '?')} esta en estado "
                f"'{order.get('state', '?')}' y no acepta firma. "
                f"Estados permitidos: {sorted(allowed_states)}."
            ),
            "order_id": order_id,
            "name": order.get("name"),
            "state": order.get("state"),
        }
        _log_call("sign_quotation", tenant_id, log_args, None,
                  result["error_detail"], int((time.time() - started) * 1000))
        return result

    if order.get("signature"):
        result = {
            "success": False,
            "error_code": "already_signed",
            "error_detail": (
                f"La cotizacion {order.get('name', '?')} ya tiene firma. "
                "No se puede sobrescribir."
            ),
            "order_id": order_id,
            "name": order.get("name"),
            "state": order.get("state"),
        }
        _log_call("sign_quotation", tenant_id, log_args, None,
                  result["error_detail"], int((time.time() - started) * 1000))
        return result

    # 4) Build signed_on ISO datetime (UTC, naive — Odoo stores datetimes as
    #    naive UTC strings 'YYYY-MM-DD HH:MM:SS').
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 5) Write signature, signed_by, signed_on.
    #
    # Tecnosmart-specific gotcha: l10n_ec_sri overrides sale.order.write
    # and (incorrectly) returns None instead of True. Odoo's XMLRPC layer
    # then raises "cannot marshal None unless allow_none is enabled" - but
    # the write DID execute server-side. Same fix-shape as the
    # action_aprobar/action_confirm block below: treat marshal-None as
    # success so vanilla Odoo tenants keep working too.
    try:
        odoo_write(
            tenant_id, url, db, user, password,
            "sale.order", [order_id],
            {
                "signature": signature,
                "signed_by": signed_by_clean,
                "signed_on": now_iso,
            },
        )
    except Exception as e:
        err_msg = str(e)
        if "cannot marshal None" in err_msg or "allow_none" in err_msg:
            logger.info(
                "sign_quotation: write returned marshal-None on order=%s "
                "(l10n_ec_sri override returns None) -- treating as success",
                order_id,
            )
        else:
            elapsed = int((time.time() - started) * 1000)
            tb = traceback.format_exc()
            logger.error("sign_quotation failed order=%s err=%s\n%s",
                         order_id, e, tb)
            _log_call("sign_quotation", tenant_id, log_args, None, str(e), elapsed)
            return {
                "success": False,
                "error_code": "write_failed",
                "error_detail": err_msg,
            }

    action_taken = "signed"
    final_state = order.get("state")

    # 6) Replicate the Odoo portal behaviour: when the customer signs,
    # Odoo's portal_quote_accept calls either action_aprobar (Tecnosmart
    # custom from l10n_ec_sri — moves to 'approved') or action_confirm
    # (Odoo upstream — moves to 'sale'). Try aprobar first because in
    # tenants with l10n_ec_sri installed action_confirm raises
    # ("debe aprobar antes"), and fall back to action_confirm so vanilla
    # Odoo tenants keep working.
    if auto_confirm:
        confirm_method_used = None
        confirm_err: str | None = None
        for method in ("action_aprobar", "action_confirm"):
            try:
                odoo_call_method(
                    tenant_id, url, db, user, password,
                    "sale.order", method, [order_id], {},
                )
                confirm_method_used = method
                break
            except Exception as e:
                err_msg = str(e)
                # Odoo 13 action_aprobar/action_confirm return None;
                # XML-RPC raises a marshal error but the action DID
                # execute server-side. Same pattern as L1183 for
                # odoo_confirm_sale_order — treat marshal-None as success.
                if "cannot marshal None" in err_msg or "allow_none" in err_msg:
                    confirm_method_used = method
                    break
                # If the method just doesn't exist on this Odoo install
                # (vanilla without l10n_ec_sri), the call returns a
                # MissingError-like message — try the next one.
                confirm_err = err_msg
                logger.info(
                    "sign_quotation: %s on order=%s failed (%s) — trying next",
                    method, order_id, err_msg[:200],
                )
                continue

        if confirm_method_used:
            action_taken = f"signed_and_{confirm_method_used.replace('action_', '')}"
            after = _read_sale_order(
                tenant_id, url, db, user, password, order_id,
                ["state"],
            ) or {}
            final_state = after.get("state", final_state)
        else:
            # Both methods failed. Signature is already persisted, so
            # surface a partial success.
            elapsed = int((time.time() - started) * 1000)
            logger.error(
                "sign_quotation: both action_aprobar and action_confirm "
                "failed order=%s last_err=%s", order_id, confirm_err,
            )
            _log_call("sign_quotation", tenant_id, log_args, None,
                      f"confirm_failed: {confirm_err}", elapsed)
            return {
                "success": False,
                "error_code": "confirm_failed",
                "error_detail": (
                    "La firma se guardo correctamente pero la confirmacion "
                    f"automatica fallo: {confirm_err}. La cotizacion sigue "
                    "firmable y puede confirmarse manualmente desde Odoo."
                ),
                "order_id": order_id,
                "name": order.get("name"),
                "state": order.get("state"),
                "signed_by": signed_by_clean,
                "signed_on": now_iso,
                "action": "signed",
            }

    result = {
        "success": True,
        "order_id": order_id,
        "name": order.get("name"),
        "state": final_state,
        "signed_by": signed_by_clean,
        "signed_on": now_iso,
        "action": action_taken,
    }
    _log_call("sign_quotation", tenant_id, log_args, result, None,
              int((time.time() - started) * 1000))
    return result
