"""Invoice / payment / statement tools — ZETA iter 80.

These tools cover the gap surfaced by the WhatsApp incident 2026-05-23
(trace: cliente OTP-verificado pidio "dame mis ultimas facturas vencidas",
el bot devolvio 5 cotizaciones porque la unica tool con datos financieros
detallados era ``odoo_check_balance`` y solo retorna el AGREGADO).

Four read-only helpers against Odoo 13 / l10n_ec:

  * ``odoo_get_customer_invoices``   → list ``account.move`` headers
  * ``odoo_get_invoice_detail``      → single invoice with lines + taxes
  * ``odoo_get_customer_payments``   → ``account.payment`` inbound
  * ``odoo_get_customer_statement``  → aggregated period summary

All four are wired behind the OTP gate at the handler layer (see
``check_balance``). The helpers here do NOT touch Supabase — they just
talk to Odoo via the existing ``odoo_search`` / ``odoo_read`` plumbing.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from mcp_odoo.tools.generic import odoo_read, odoo_search
from mcp_odoo.tools.sales import _absolutize_share_link, _log_call

logger = logging.getLogger("mcp_odoo.invoices")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INVOICE_PAYMENT_STATE_LABEL_ES = {
    "paid": "pagada",
    "in_payment": "en pago",
    "not_paid": "pendiente",
    "partial": "parcial",
    "reversed": "anulada",
    "invoicing_legacy": "previa",
}

_INVOICE_TYPE_LABEL_ES = {
    "out_invoice": "factura",
    "out_refund": "nota de credito",
    "in_invoice": "factura proveedor",
    "in_refund": "nota credito proveedor",
}

# Default fields read from ``account.move``. The list is small on purpose:
# we surface only the columns the chat formatter / LLM actually needs.
_INVOICE_HEADER_FIELDS = [
    "id",
    "name",
    "number",
    "type",
    "state",
    "partner_id",
    "invoice_date",
    "invoice_date_due",
    "amount_total",
    "amount_residual",
    "amount_untaxed",
    "amount_tax",
    "invoice_payment_state",
    "ref",
    "access_token",
    "access_url",
    "currency_id",
]

# Optional l10n_ec extras — declared separately because they only exist in
# Tecnosmart-style installs and we must tolerate their absence.
_INVOICE_L10N_EC_FIELDS = [
    "total_sri",
    "total_descuento_xml",
]

_PAYMENT_FIELDS = [
    "id",
    "name",
    "payment_date",
    "amount",
    "journal_id",
    "partner_id",
    "payment_type",
    "state",
    "communication",
    "reconciled_invoice_ids",
]

_MOVE_LINE_FIELDS = [
    "id",
    "product_id",
    "name",
    "quantity",
    "price_unit",
    "discount",
    "price_subtotal",
    "price_total",
    "tax_ids",
    "account_id",
]

_TAX_FIELDS = ["id", "name", "amount", "amount_type", "type_tax_use"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: Any) -> date | None:
    """Coerce Odoo date strings into ``datetime.date``. Returns None on miss.

    Accepts ``"YYYY-MM-DD"`` and ``"YYYY-MM-DD HH:MM:SS"``. The first form
    is the common case (Odoo ``date`` columns). The second covers the
    ``datetime`` columns we occasionally read (``write_date`` etc.).
    """
    if raw in (None, "", False):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # Try strict date first (most common), then full datetime.
    for fmt, length in (("%Y-%m-%d", 10), ("%Y-%m-%d %H:%M:%S", 19)):
        if len(s) < length:
            continue
        try:
            return datetime.strptime(s[:length], fmt).date()
        except ValueError:
            continue
    return None


def _build_portal_url(
    odoo_url: str,
    invoice_id: int,
    access_url: Any,
    access_token: Any,
) -> str | None:
    """Build an absolute portal URL for the invoice's RIDE PDF.

    Priority:
      1. If ``access_url`` is already absolute, append the token query.
      2. Otherwise compose ``{odoo_url}/my/invoices/{id}?access_token=...``.

    Returns ``None`` when both base URL and token are missing — in that
    case the LLM cannot link the RIDE and must say so honestly. We
    intentionally NEVER strip the ``access_token`` (memory: that token IS
    the portal mechanism, stripping it makes the LLM fabricate invalid
    URLs).
    """
    token = (access_token or "").strip() if isinstance(access_token, str) else ""
    raw_url = (access_url or "").strip() if isinstance(access_url, str) else ""

    if raw_url:
        absolute = _absolutize_share_link(raw_url, odoo_url)
        if absolute:
            if token and "access_token=" not in absolute:
                sep = "&" if "?" in absolute else "?"
                absolute = f"{absolute}{sep}access_token={token}"
            return absolute

    # Fallback to the canonical /my/invoices/<id> route.
    if not odoo_url:
        return None
    base = odoo_url.rstrip("/")
    suffix = f"/my/invoices/{int(invoice_id)}"
    if token:
        suffix += f"?access_token={token}"
    return base + suffix


def _flatten_m2o(value: Any) -> dict | None:
    """``[id, name]`` -> ``{"id": id, "name": name}``."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {"id": value[0], "name": value[1]}
    return None


def _state_label(payment_state: str | None, state: str | None) -> str:
    """Best-effort Spanish label for the row.

    ``invoice_payment_state`` covers posted invoices. When it is empty
    (draft, cancel) we fall back to ``state``.
    """
    if payment_state:
        return _INVOICE_PAYMENT_STATE_LABEL_ES.get(payment_state, payment_state)
    if state == "draft":
        return "borrador"
    if state == "cancel":
        return "cancelada"
    return state or ""


def _days_overdue(due: date | None, *, today: date | None = None) -> int | None:
    """Positive = days late, negative = days until due, None when undue date."""
    if due is None:
        return None
    ref = today or date.today()
    return (ref - due).days


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def odoo_get_customer_invoices(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    partner_id: int,
    state: str = "all",
    limit: int = 10,
    year: int | None = None,
) -> dict:
    """List ``account.move`` headers for a partner.

    Args
    ----
    partner_id : int
        ``res.partner.id``.
    state : str, default ``"all"``
        One of ``"all" | "paid" | "not_paid" | "overdue"``. ``"overdue"``
        filters server-side by ``invoice_payment_state in (not_paid,
        partial)`` AND ``invoice_date_due < today``.
    limit : int, default ``10`` (max ``50``)
        Cap on rows returned.
    year : int, optional
        Restrict to invoices whose ``invoice_date`` year matches.

    Returns
    -------
    dict
        Envelope with ``success, partner_id, state_filter, count,
        total_amount, total_residual, invoices[]``. Each invoice carries
        ``invoice_id, name, invoice_date, invoice_date_due, days_overdue,
        amount_total, amount_residual, payment_state, payment_state_label,
        ref, type, type_label, portal_url, currency``.
    """
    started = time.time()
    log_args = {
        "partner_id": partner_id,
        "state": state,
        "limit": limit,
        "year": year,
    }

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id"}

    try:
        limit_int = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        limit_int = 10

    state = (state or "all").strip().lower()
    if state not in {"all", "paid", "not_paid", "overdue"}:
        return {
            "success": False,
            "error_code": "invalid_state",
            "error_detail": (
                f"state debe ser uno de: all, paid, not_paid, overdue "
                f"(recibido: {state!r})"
            ),
        }

    # ----- Domain ---------------------------------------------------------
    domain: list = [
        ["partner_id", "=", partner_id],
        ["type", "in", ["out_invoice", "out_refund"]],
        ["state", "=", "posted"],
    ]

    today_iso = date.today().isoformat()
    if state == "paid":
        domain.append(["invoice_payment_state", "=", "paid"])
    elif state == "not_paid":
        domain.append(["invoice_payment_state", "in", ["not_paid", "partial"]])
    elif state == "overdue":
        domain.append(["invoice_payment_state", "in", ["not_paid", "partial"]])
        domain.append(["invoice_date_due", "<", today_iso])

    if year is not None:
        try:
            y = int(year)
            domain.append(["invoice_date", ">=", f"{y}-01-01"])
            domain.append(["invoice_date", "<=", f"{y}-12-31"])
        except (TypeError, ValueError):
            return {"success": False, "error_code": "invalid_year"}

    # ----- Read fields (with l10n_ec fallback) ---------------------------
    fields = list(_INVOICE_HEADER_FIELDS)
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "account.move", domain,
            fields=fields, limit=limit_int,
            order="invoice_date desc, id desc",
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error listando facturas: {exc}"
        _log_call("get_customer_invoices", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "search_failed",
            "error_detail": msg,
        }

    today_d = date.today()
    invoices: list[dict] = []
    total_amount = 0.0
    total_residual = 0.0
    for r in rows:
        inv_id = int(r["id"])
        due = _parse_date(r.get("invoice_date_due"))
        d_over = _days_overdue(due, today=today_d)
        amount_total = float(r.get("amount_total") or 0)
        amount_residual = float(r.get("amount_residual") or 0)
        total_amount += amount_total
        total_residual += amount_residual

        payment_state = r.get("invoice_payment_state") or ""
        type_v = r.get("type") or ""
        currency = _flatten_m2o(r.get("currency_id"))

        invoices.append({
            "invoice_id": inv_id,
            "name": r.get("name") or "",
            "number": r.get("number") or None,
            "invoice_date": str(r.get("invoice_date") or "") or None,
            "invoice_date_due": str(r.get("invoice_date_due") or "") or None,
            "days_overdue": d_over,
            "amount_total": round(amount_total, 2),
            "amount_residual": round(amount_residual, 2),
            "amount_untaxed": round(float(r.get("amount_untaxed") or 0), 2),
            "amount_tax": round(float(r.get("amount_tax") or 0), 2),
            "payment_state": payment_state or None,
            "payment_state_label": _state_label(payment_state, r.get("state")),
            "ref": r.get("ref") or None,
            "type": type_v,
            "type_label": _INVOICE_TYPE_LABEL_ES.get(type_v, type_v),
            "currency": currency.get("name") if currency else None,
            "portal_url": _build_portal_url(
                url, inv_id,
                r.get("access_url"), r.get("access_token"),
            ),
        })

    result = {
        "success": True,
        "partner_id": partner_id,
        "state_filter": state,
        "count": len(invoices),
        "total_amount": round(total_amount, 2),
        "total_residual": round(total_residual, 2),
        "invoices": invoices,
        "display_type": "list_data",
    }
    _log_call("get_customer_invoices", tenant_id, log_args,
              {"count": len(invoices)}, None,
              int((time.time() - started) * 1000))
    return result


def odoo_get_invoice_detail(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    invoice_id: int,
    include_lines: bool = True,
    include_taxes: bool = True,
) -> dict:
    """Read one invoice with optional lines + tax breakdown.

    The line breakdown reads ``account.move.line`` rows linked via
    ``move_id`` and resolves ``tax_ids`` once at the end with a single
    ``account.tax`` lookup (one call, many ids).
    """
    started = time.time()
    log_args = {
        "invoice_id": invoice_id,
        "include_lines": include_lines,
        "include_taxes": include_taxes,
    }

    try:
        inv_id_int = int(invoice_id)
    except (TypeError, ValueError):
        return {"success": False, "error_code": "invalid_invoice_id"}
    if inv_id_int <= 0:
        return {"success": False, "error_code": "invalid_invoice_id"}

    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "account.move", [inv_id_int],
            _INVOICE_HEADER_FIELDS + ["invoice_line_ids"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo factura {inv_id_int}: {exc}"
        _log_call("get_invoice_detail", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "read_failed",
            "error_detail": msg,
        }
    if not rows:
        _log_call("get_invoice_detail", tenant_id, log_args, None,
                  "not_found", int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "invoice_not_found",
            "error_detail": f"No existe la factura id={inv_id_int}.",
        }

    r = rows[0]
    today_d = date.today()
    due = _parse_date(r.get("invoice_date_due"))
    amount_total = float(r.get("amount_total") or 0)
    amount_residual = float(r.get("amount_residual") or 0)
    payment_state = r.get("invoice_payment_state") or ""
    type_v = r.get("type") or ""
    partner = _flatten_m2o(r.get("partner_id"))
    currency = _flatten_m2o(r.get("currency_id"))

    invoice: dict[str, Any] = {
        "invoice_id": inv_id_int,
        "name": r.get("name") or "",
        "number": r.get("number") or None,
        "type": type_v,
        "type_label": _INVOICE_TYPE_LABEL_ES.get(type_v, type_v),
        "state": r.get("state") or "",
        "invoice_date": str(r.get("invoice_date") or "") or None,
        "invoice_date_due": str(r.get("invoice_date_due") or "") or None,
        "days_overdue": _days_overdue(due, today=today_d),
        "amount_total": round(amount_total, 2),
        "amount_residual": round(amount_residual, 2),
        "amount_untaxed": round(float(r.get("amount_untaxed") or 0), 2),
        "amount_tax": round(float(r.get("amount_tax") or 0), 2),
        "payment_state": payment_state or None,
        "payment_state_label": _state_label(payment_state, r.get("state")),
        "ref": r.get("ref") or None,
        "partner": partner,
        "currency": currency.get("name") if currency else None,
        "portal_url": _build_portal_url(
            url, inv_id_int,
            r.get("access_url"), r.get("access_token"),
        ),
    }

    lines: list[dict] = []
    taxes_breakdown: list[dict] = []

    line_ids = r.get("invoice_line_ids") or []
    if include_lines and line_ids:
        try:
            raw_lines = odoo_read(
                tenant_id, url, db, user, password,
                "account.move.line", list(line_ids), _MOVE_LINE_FIELDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_invoice_detail: read lines failed inv=%s: %s",
                inv_id_int, exc,
            )
            raw_lines = []

        # Filter only product lines (skip tax + payment ledger entries).
        # Heuristic: keep lines with non-empty ``product_id`` OR positive
        # quantity AND a non-zero ``price_subtotal``. account.move.line
        # rows for taxes have product_id=False and quantity=0.
        prod_lines = []
        all_tax_ids: set[int] = set()
        for ln in raw_lines:
            prod = _flatten_m2o(ln.get("product_id"))
            qty = float(ln.get("quantity") or 0)
            sub = float(ln.get("price_subtotal") or 0)
            if not prod and qty == 0 and sub == 0:
                continue
            ln_taxes = ln.get("tax_ids") or []
            for t in ln_taxes:
                if isinstance(t, int):
                    all_tax_ids.add(t)
            prod_lines.append({
                "line_id": ln.get("id"),
                "product": prod,
                "name": ln.get("name") or "",
                "quantity": qty,
                "price_unit": round(float(ln.get("price_unit") or 0), 4),
                "discount": round(float(ln.get("discount") or 0), 2),
                "price_subtotal": round(sub, 2),
                "price_total": round(float(ln.get("price_total") or 0), 2),
                "tax_ids": ln_taxes,
            })

        # Resolve tax names in one shot.
        tax_lookup: dict[int, dict] = {}
        if include_taxes and all_tax_ids:
            try:
                tax_rows = odoo_read(
                    tenant_id, url, db, user, password,
                    "account.tax", sorted(all_tax_ids), _TAX_FIELDS,
                )
                tax_lookup = {int(t["id"]): t for t in tax_rows}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_invoice_detail: read taxes failed inv=%s: %s",
                    inv_id_int, exc,
                )

        for ln in prod_lines:
            ln_taxes_info = []
            for tid in ln.get("tax_ids", []):
                if not isinstance(tid, int):
                    continue
                t = tax_lookup.get(tid)
                if t:
                    ln_taxes_info.append({
                        "id": tid,
                        "name": t.get("name") or "",
                        "amount": float(t.get("amount") or 0),
                    })
            ln["taxes"] = ln_taxes_info
            lines.append(ln)

        if include_taxes and tax_lookup:
            # Aggregate by tax_id across all lines.
            per_tax: dict[int, dict] = {}
            for ln in lines:
                base = ln.get("price_subtotal", 0)
                for t in ln.get("taxes") or []:
                    bucket = per_tax.setdefault(t["id"], {
                        "tax_id": t["id"],
                        "name": t["name"],
                        "amount_pct": t["amount"],
                        "base": 0.0,
                        "tax_amount": 0.0,
                    })
                    bucket["base"] += base
                    bucket["tax_amount"] += base * (t["amount"] / 100.0)
            for tid, b in per_tax.items():
                taxes_breakdown.append({
                    "tax_id": b["tax_id"],
                    "name": b["name"],
                    "amount_pct": round(b["amount_pct"], 2),
                    "base": round(b["base"], 2),
                    "tax_amount": round(b["tax_amount"], 2),
                })

    result = {
        "success": True,
        "invoice": invoice,
        "lines": lines if include_lines else None,
        "taxes": taxes_breakdown if include_taxes else None,
    }
    _log_call("get_invoice_detail", tenant_id, log_args,
              {"lines": len(lines), "taxes": len(taxes_breakdown)}, None,
              int((time.time() - started) * 1000))
    return result


def odoo_get_customer_payments(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    partner_id: int,
    limit: int = 10,
    year: int | None = None,
) -> dict:
    """List inbound ``account.payment`` rows for a partner.

    Only posted, ``payment_type='inbound'`` rows are returned. The
    ``reconciled_invoice_ids`` list is resolved to invoice names in a
    second batch read so the LLM can answer "que factura pagaste el N de
    M".
    """
    started = time.time()
    log_args = {"partner_id": partner_id, "limit": limit, "year": year}

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id"}

    try:
        limit_int = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        limit_int = 10

    domain: list = [
        ["partner_id", "=", partner_id],
        ["payment_type", "=", "inbound"],
        ["state", "=", "posted"],
    ]
    if year is not None:
        try:
            y = int(year)
            domain.append(["payment_date", ">=", f"{y}-01-01"])
            domain.append(["payment_date", "<=", f"{y}-12-31"])
        except (TypeError, ValueError):
            return {"success": False, "error_code": "invalid_year"}

    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "account.payment", domain,
            fields=_PAYMENT_FIELDS, limit=limit_int,
            order="payment_date desc, id desc",
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error listando pagos: {exc}"
        _log_call("get_customer_payments", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "search_failed",
            "error_detail": msg,
        }

    # Batch resolve reconciled invoices (name only).
    all_inv_ids: set[int] = set()
    for r in rows:
        for iid in (r.get("reconciled_invoice_ids") or []):
            if isinstance(iid, int):
                all_inv_ids.add(iid)
    invoice_lookup: dict[int, str] = {}
    if all_inv_ids:
        try:
            inv_rows = odoo_read(
                tenant_id, url, db, user, password,
                "account.move", sorted(all_inv_ids), ["name"],
            )
            invoice_lookup = {int(i["id"]): (i.get("name") or "") for i in inv_rows}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_customer_payments: invoice name lookup failed: %s", exc,
            )

    payments: list[dict] = []
    total_amount = 0.0
    for r in rows:
        amount = float(r.get("amount") or 0)
        total_amount += amount
        journal = _flatten_m2o(r.get("journal_id"))
        applied = [
            {"invoice_id": iid, "name": invoice_lookup.get(iid, "")}
            for iid in (r.get("reconciled_invoice_ids") or [])
            if isinstance(iid, int)
        ]
        payments.append({
            "payment_id": int(r["id"]),
            "name": r.get("name") or "",
            "payment_date": str(r.get("payment_date") or "") or None,
            "amount": round(amount, 2),
            "journal": journal.get("name") if journal else None,
            "communication": r.get("communication") or None,
            "applied_to": applied,
        })

    result = {
        "success": True,
        "partner_id": partner_id,
        "count": len(payments),
        "total_amount": round(total_amount, 2),
        "payments": payments,
        "display_type": "list_data",
    }
    _log_call("get_customer_payments", tenant_id, log_args,
              {"count": len(payments)}, None,
              int((time.time() - started) * 1000))
    return result


def odoo_get_customer_statement(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    partner_id: int,
    days_back: int = 90,
) -> dict:
    """Aggregated account statement for the partner.

    Reads:
      * ``account.move`` (posted out_invoice/out_refund) within the
        period for ``total_billed`` + ``invoices_in_period``.
      * ``account.payment`` (posted inbound) within the period for
        ``total_paid``.
      * ``account.move.line`` receivable unreconciled (current snapshot)
        for ``total_due_now`` + ``total_overdue_now`` +
        ``invoices_overdue``.
      * A merged recent_movements list (last 10 by date) combining
        invoices + payments.

    ``avg_payment_days`` is computed from invoices with
    ``invoice_payment_state='paid'`` in the period: difference between
    ``invoice_date`` and the latest reconciled payment date.
    """
    started = time.time()
    log_args = {"partner_id": partner_id, "days_back": days_back}

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id"}

    try:
        days_back_int = max(1, min(int(days_back or 90), 730))
    except (TypeError, ValueError):
        days_back_int = 90

    today_d = date.today()
    period_from = today_d - timedelta(days=days_back_int)
    period_from_iso = period_from.isoformat()
    today_iso = today_d.isoformat()

    # ----- Invoices in period -----------------------------------------
    inv_domain = [
        ["partner_id", "=", partner_id],
        ["type", "in", ["out_invoice", "out_refund"]],
        ["state", "=", "posted"],
        ["invoice_date", ">=", period_from_iso],
        ["invoice_date", "<=", today_iso],
    ]
    try:
        invoices = odoo_search(
            tenant_id, url, db, user, password,
            "account.move", inv_domain,
            fields=[
                "id", "name", "invoice_date", "invoice_date_due",
                "amount_total", "amount_residual", "invoice_payment_state",
                "type",
            ],
            limit=200, order="invoice_date desc, id desc",
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error consultando facturas: {exc}"
        _log_call("get_customer_statement", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "search_failed",
            "error_detail": msg,
        }

    # ----- Payments in period -----------------------------------------
    pay_domain = [
        ["partner_id", "=", partner_id],
        ["payment_type", "=", "inbound"],
        ["state", "=", "posted"],
        ["payment_date", ">=", period_from_iso],
        ["payment_date", "<=", today_iso],
    ]
    try:
        payments = odoo_search(
            tenant_id, url, db, user, password,
            "account.payment", pay_domain,
            fields=[
                "id", "name", "payment_date", "amount",
                "reconciled_invoice_ids",
            ],
            limit=200, order="payment_date desc, id desc",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_customer_statement: payment search failed: %s", exc,
        )
        payments = []

    # ----- Current open receivables (snapshot, NOT period-bound) -------
    # Reuse the proven logic from odoo_check_balance.
    try:
        open_lines = odoo_search(
            tenant_id, url, db, user, password,
            "account.move.line",
            [
                ["partner_id", "=", partner_id],
                ["account_id.user_type_id.type", "=", "receivable"],
                ["full_reconcile_id", "=", False],
                ["parent_state", "=", "posted"],
            ],
            fields=["move_id", "date_maturity", "amount_residual"],
            limit=500,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_customer_statement: open lines failed: %s", exc,
        )
        open_lines = []

    total_due_now = sum(float(l.get("amount_residual") or 0) for l in open_lines)
    overdue_lines = [
        l for l in open_lines
        if _parse_date(l.get("date_maturity")) and
        _parse_date(l.get("date_maturity")) < today_d
    ]
    total_overdue_now = sum(
        float(l.get("amount_residual") or 0) for l in overdue_lines
    )

    # ----- Aggregates --------------------------------------------------
    total_billed = sum(float(i.get("amount_total") or 0) for i in invoices)
    total_paid = sum(float(p.get("amount") or 0) for p in payments)

    # avg_payment_days: paid invoices in period, average of (paid_date -
    # invoice_date). Approximation: use TODAY for paid invoices whose
    # reconciled payment falls inside our payments[] (we have date there).
    paid_invoices = [
        i for i in invoices
        if i.get("invoice_payment_state") == "paid" and i.get("invoice_date")
    ]
    # Build inv_id -> payment_date map from payments.
    inv_to_paydate: dict[int, date] = {}
    for p in payments:
        pd = _parse_date(p.get("payment_date"))
        if not pd:
            continue
        for iid in (p.get("reconciled_invoice_ids") or []):
            if isinstance(iid, int):
                # Keep the latest payment date if multiple.
                cur = inv_to_paydate.get(iid)
                if cur is None or pd > cur:
                    inv_to_paydate[iid] = pd

    diffs: list[int] = []
    for inv in paid_invoices:
        inv_d = _parse_date(inv.get("invoice_date"))
        pay_d = inv_to_paydate.get(int(inv["id"]))
        if inv_d and pay_d and pay_d >= inv_d:
            diffs.append((pay_d - inv_d).days)
    avg_payment_days = round(sum(diffs) / len(diffs), 1) if diffs else None

    # ----- Recent movements (merge invoices + payments) ---------------
    movements: list[dict] = []
    for inv in invoices:
        movements.append({
            "date": str(inv.get("invoice_date") or ""),
            "type": "invoice",
            "name": inv.get("name") or "",
            "amount": round(float(inv.get("amount_total") or 0), 2),
            "residual": round(float(inv.get("amount_residual") or 0), 2),
            "applied_to": None,
        })
    for pay in payments:
        applied_names = []
        for iid in (pay.get("reconciled_invoice_ids") or []):
            if isinstance(iid, int):
                # We don't have the names cached here; defer resolution.
                applied_names.append(iid)
        movements.append({
            "date": str(pay.get("payment_date") or ""),
            "type": "payment",
            "name": pay.get("name") or "",
            "amount": -round(float(pay.get("amount") or 0), 2),
            "residual": 0.0,
            "applied_to_ids": applied_names,
        })

    # Resolve payment.applied_to_ids → invoice names (single batch).
    all_app_ids: set[int] = set()
    for m in movements:
        for iid in (m.get("applied_to_ids") or []):
            if isinstance(iid, int):
                all_app_ids.add(iid)
    if all_app_ids:
        try:
            inv_rows = odoo_read(
                tenant_id, url, db, user, password,
                "account.move", sorted(all_app_ids), ["name"],
            )
            name_lookup = {int(r["id"]): (r.get("name") or "") for r in inv_rows}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_customer_statement: applied name lookup failed: %s", exc,
            )
            name_lookup = {}
        for m in movements:
            if m["type"] == "payment":
                m["applied_to"] = [
                    name_lookup.get(iid, "")
                    for iid in (m.get("applied_to_ids") or [])
                ]
                m.pop("applied_to_ids", None)
    # Order by date desc and cap to 10.
    movements.sort(key=lambda x: x.get("date") or "", reverse=True)
    recent_movements = movements[:10]

    summary = {
        "total_billed": round(total_billed, 2),
        "total_paid": round(total_paid, 2),
        "total_due_now": round(total_due_now, 2),
        "total_overdue_now": round(total_overdue_now, 2),
        "invoices_in_period": len(invoices),
        "invoices_overdue": len(overdue_lines),
        "avg_payment_days": avg_payment_days,
    }
    result = {
        "success": True,
        "partner_id": partner_id,
        "period": {"from": period_from_iso, "to": today_iso},
        "summary": summary,
        "recent_movements": recent_movements,
    }
    _log_call("get_customer_statement", tenant_id, log_args,
              {"invoices_in_period": len(invoices),
               "payments_in_period": len(payments)}, None,
              int((time.time() - started) * 1000))
    return result


__all__ = [
    "odoo_get_customer_invoices",
    "odoo_get_invoice_detail",
    "odoo_get_customer_payments",
    "odoo_get_customer_statement",
]
