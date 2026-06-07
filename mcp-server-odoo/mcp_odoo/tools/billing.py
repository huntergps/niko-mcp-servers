"""Billing tools for salon tenants (Odoo 19 ``account.move`` + l10n_ec_edi).

Single write helper that lets a chat agent draft a customer invoice for a
beauty salon running Ecuadorian electronic invoicing (``l10n_ec`` +
``l10n_ec_edi``):

  * ``create_invoice`` → create an ``account.move`` (move_type
                          'out_invoice') in **DRAFT** state for an
                          already-identified customer.

Design notes
------------
* Pure function: ``create_invoice`` receives the per-request Odoo
  connection tuple ``(tenant_id, url, db, user, password)`` plus typed
  args, exactly like ``mcp_odoo.tools.appointments``. The dispatch layer
  in ``mcp_transport.py`` resolves that tuple from the request headers
  (multi-tenant) and passes it down — we never open a connection here.
* All Odoo I/O goes through the shared ``odoo_search`` / ``odoo_read`` /
  ``odoo_create`` / ``odoo_call_method`` from ``mcp_odoo.tools.generic``.
* The invoice is left in **DRAFT** on purpose. We do NOT call
  ``action_post`` and we do NOT set the l10n_ec posting fields
  (``l10n_latam_document_type_id`` / ``l10n_ec_sri_payment_id``) — those
  are only required at posting time and are filled by the salon staff
  when they review and authorize the document at the SRI.
* Each invoice line resolves a service/product by name fuzzily
  (case-insensitive ilike, exact-match preferred) the same way
  ``appointments._resolve_appointment_type`` does. We never invent a
  product: if a line item is not found we abort with a clear error
  listing the offending item.
* ``price_unit`` / ``tax_ids`` are NOT forced unless the caller passes a
  ``price_unit`` explicitly — Odoo derives the list price and the
  product's ``taxes_id`` (IVA) on its own, which keeps the EC tax
  computation correct.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from mcp_odoo.tools.generic import (
    odoo_call_method,
    odoo_create,
    odoo_read,
    odoo_search,
)

logger = logging.getLogger("mcp_odoo.billing")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Message posted on the freshly-created draft so the salon staff know a chat
# assistant drafted it and it still needs human review + SRI authorization.
_STAFF_NOTICE_BODY = (
    "Factura creada por Liz (asistente de chat). Pendiente de revisión y "
    "autorización al SRI por el staff."
)

# Fields read back from the created account.move for the success envelope.
_MOVE_FIELDS = [
    "id",
    "name",
    "state",
    "partner_id",
    "amount_total",
    "amount_untaxed",
    "amount_tax",
    "currency_id",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_call(
    tool: str, tenant_id: str, args: dict, result_summary: Any,
    error: str | None, duration_ms: int,
) -> None:
    """Lightweight structured log (mirrors appointments._log_call style)."""
    if error:
        logger.warning(
            "billing_tool=%s tenant=%s args=%s error=%s ms=%d",
            tool, tenant_id, args, error, duration_ms,
        )
    else:
        logger.info(
            "billing_tool=%s tenant=%s args=%s result=%s ms=%d",
            tool, tenant_id, args, result_summary, duration_ms,
        )


def _flatten_m2o(value: Any) -> dict | None:
    """``[id, name]`` -> ``{"id": id, "name": name}``."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {"id": value[0], "name": value[1]}
    return None


def _resolve_product(creds: tuple, item: str) -> dict | None:
    """Resolve a service/product name to its product.product record.

    Fuzzy ilike on ``name``: tries an exact (case-insensitive) match first;
    otherwise prefers the shortest name (most specific hit) to avoid
    grabbing a longer unrelated product that merely contains the substring.
    Mirrors ``appointments._resolve_appointment_type``. Returns the read
    dict (``id, name, list_price``) or ``None`` when nothing matches.
    """
    name = (item or "").strip()
    if not name:
        return None
    tenant_id, url, db, user, password = creds
    rows = odoo_search(
        tenant_id, url, db, user, password,
        "product.product", [["name", "ilike", name]],
        fields=["id", "name", "list_price"], limit=10, order="name",
    )
    if not rows:
        return None
    lowered = name.lower()
    exact = [r for r in rows if (r.get("name") or "").strip().lower() == lowered]
    if exact:
        return exact[0]
    rows.sort(key=lambda r: len(r.get("name") or ""))
    return rows[0]


def _resolve_sale_journal_id(creds: tuple) -> int | None:
    """Resolve the default sales journal (account.journal type='sale').

    Returns the first sales journal id (ordered by sequence/id) or ``None``
    when the tenant has no sales journal configured.
    """
    tenant_id, url, db, user, password = creds
    rows = odoo_search(
        tenant_id, url, db, user, password,
        "account.journal", [["type", "=", "sale"]],
        fields=["id", "name", "code"], limit=1, order="sequence, id",
    )
    if not rows:
        return None
    return int(rows[0]["id"])


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def create_invoice(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    partner_id: int,
    lines: list[dict],
    note: str | None = None,
) -> dict:
    """Create a DRAFT customer invoice (``account.move`` out_invoice).

    The invoice is created in ``draft`` state — it is NOT posted and NOT
    authorized at the SRI. A note is posted on the document so the salon
    staff know to review and emit it.

    Args
    ----
    partner_id : int
        ``res.partner.id`` of the already-identified customer.
    lines : list[dict]
        One dict per invoice line. Each item:
          * ``item``       : str  — service/product name (resolved fuzzily)
          * ``quantity``   : float — defaults to 1
          * ``price_unit`` : float | None — only forced when provided;
                              otherwise Odoo uses the product list price.
    note : str, optional
        Free-text note appended to the staff message on the draft.

    Returns
    -------
    dict
        Success envelope with ``invoice_id, name, partner_id,
        partner_name, state='draft', currency, amount_untaxed,
        amount_total, lines[], staff_notice=True``. On failure an envelope
        ``{success: False, error_code, error_detail}`` in Spanish.
    """
    started = time.time()
    creds = (tenant_id, url, db, user, password)
    log_args = {"partner_id": partner_id, "n_lines": len(lines or [])}

    # ----- 1. Validate partner_id ------------------------------------------
    if not isinstance(partner_id, int) or partner_id <= 0:
        return {
            "success": False,
            "error_code": "invalid_partner_id",
            "error_detail": "El partner_id del cliente no es válido.",
        }

    if not lines or not isinstance(lines, list):
        return {
            "success": False,
            "error_code": "no_lines",
            "error_detail": (
                "No me diste ningún servicio o producto para facturar."
            ),
        }

    partner_name = ""
    try:
        prows = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [partner_id], ["name"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo el cliente: {exc}"
        _log_call("create_invoice", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_read_failed",
                "error_detail": msg}
    if not prows:
        return {
            "success": False,
            "error_code": "partner_not_found",
            "error_detail": f"No existe un cliente con id={partner_id}.",
        }
    partner_name = prows[0].get("name") or ""

    # ----- 2. Resolve the sales journal ------------------------------------
    try:
        journal_id = _resolve_sale_journal_id(creds)
    except Exception as exc:  # noqa: BLE001
        msg = f"Error buscando el diario de ventas: {exc}"
        _log_call("create_invoice", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "journal_read_failed",
                "error_detail": msg}
    if not journal_id:
        return {
            "success": False,
            "error_code": "no_sale_journal",
            "error_detail": (
                "No encontré un diario de ventas configurado en Odoo."
            ),
        }

    # ----- 3. Resolve every line by product name ---------------------------
    invoice_line_cmds: list[tuple] = []
    line_summaries: list[dict] = []
    not_found: list[str] = []
    for raw in lines:
        if not isinstance(raw, dict):
            not_found.append(str(raw))
            continue
        item = str(raw.get("item") or "").strip()
        if not item:
            not_found.append("(ítem vacío)")
            continue
        try:
            qty = float(raw.get("quantity") if raw.get("quantity") is not None else 1)
        except (TypeError, ValueError):
            qty = 1.0
        if qty <= 0:
            qty = 1.0

        product = _resolve_product(creds, item)
        if not product:
            not_found.append(item)
            continue

        line_vals: dict[str, Any] = {
            "product_id": int(product["id"]),
            "quantity": qty,
        }
        # Only force price_unit when the caller passed one explicitly;
        # otherwise let Odoo take the product list price + its taxes_id.
        price_unit = raw.get("price_unit")
        if price_unit is not None:
            try:
                line_vals["price_unit"] = float(price_unit)
            except (TypeError, ValueError):
                pass

        invoice_line_cmds.append((0, 0, line_vals))
        line_summaries.append({
            "item": product.get("name") or item,
            "product_id": int(product["id"]),
            "quantity": qty,
            "price_unit": line_vals.get("price_unit"),
        })

    if not_found:
        listed = ", ".join(f"'{n}'" for n in not_found)
        return {
            "success": False,
            "error_code": "item_not_found",
            "error_detail": (
                f"No encontré en el catálogo: {listed}. "
                "Revisa el nombre exacto del servicio o producto; no puedo "
                "facturar algo que no existe en el sistema."
            ),
        }

    if not invoice_line_cmds:
        return {
            "success": False,
            "error_code": "no_lines",
            "error_detail": (
                "No me diste ningún servicio o producto válido para facturar."
            ),
        }

    # ----- 4. Create the account.move in DRAFT -----------------------------
    move_values: dict[str, Any] = {
        "move_type": "out_invoice",
        "partner_id": partner_id,
        "journal_id": journal_id,
        "invoice_date": date.today().isoformat(),
        "invoice_line_ids": invoice_line_cmds,
    }
    try:
        invoice_id = odoo_create(
            tenant_id, url, db, user, password,
            "account.move", move_values,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error creando la factura en borrador: {exc}"
        _log_call("create_invoice", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "create_failed",
                "error_detail": msg}

    invoice_id = int(invoice_id)

    # ----- 5. Staff notice (best-effort, never aborts) ---------------------
    notice_body = _STAFF_NOTICE_BODY
    if note and str(note).strip():
        notice_body = f"{notice_body}\n\nNota: {str(note).strip()}"
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "account.move", "message_post", [invoice_id],
            kwargs={"body": notice_body},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "create_invoice: message_post failed for move %s: %s",
            invoice_id, exc,
        )

    # ----- 6. Read the created invoice back for the envelope ---------------
    name = ""
    state = "draft"
    amount_total: float | None = None
    amount_untaxed: float | None = None
    try:
        mrows = odoo_read(
            tenant_id, url, db, user, password,
            "account.move", [invoice_id], _MOVE_FIELDS,
        )
        if mrows:
            mv = mrows[0]
            name = mv.get("name") or ""
            state = mv.get("state") or "draft"
            amount_total = float(mv.get("amount_total") or 0)
            amount_untaxed = float(mv.get("amount_untaxed") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "create_invoice: read-back failed for move %s: %s",
            invoice_id, exc,
        )

    result = {
        "success": True,
        "invoice_id": invoice_id,
        "name": name,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "state": state,
        "currency": "USD",
        "amount_untaxed": (
            round(amount_untaxed, 2) if amount_untaxed is not None else None
        ),
        "amount_total": (
            round(amount_total, 2) if amount_total is not None else None
        ),
        "lines": line_summaries,
        "staff_notice": True,
        "display_type": "list_data",
    }
    _log_call("create_invoice", tenant_id, log_args,
              {"invoice_id": invoice_id, "state": state}, None,
              int((time.time() - started) * 1000))
    return result


__all__ = ["create_invoice"]
