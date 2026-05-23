"""WhatsApp/Telegram formatters for invoice / payment / statement tools.

ZETA iter 80. Same contract as ``mcp_odoo.formatters.whatsapp``:
  * never emit pipes ``|``, triple dashes ``---`` or tabs
  * ``*bold*`` / ``_italic_`` only
  * one blank line between items
  * cap lists at 10 items, append "y N más" hint
  * dates as ``DD-mmm[-YYYY] HH:MM`` (reuse ``_fmt_date_compact``)
  * prices via ``format_price_display`` (BPE-safe thousands separator)

These formatters never raise — a render error must not break a successful
tool call. Caller (``_attach_display_text``) swallows exceptions.
"""
from __future__ import annotations

from typing import Any

import re as _re

from mcp_odoo.formatters.whatsapp import (
    _MORE_HINT,
    _fmt_date_compact,
    _fmt_total,
    _truncate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATE_FILTER_LABEL_ES = {
    "all": "todas",
    "paid": "pagadas",
    "not_paid": "pendientes",
    "overdue": "vencidas",
}

# Iter 81b — WhatsApp/Telegram detectan ``\\d+-\\d+-\\d+`` como teléfonos
# y los renderizan con dial. Las facturas migradas Tecnosmart tienen
# ``name='002-002-000014038'`` (formato SRI: establecimiento-punto-
# secuencial). Reemplazamos guiones por puntos para romper el patrón
# sin perder legibilidad humana. Owner-evidencia (screenshot WhatsApp
# 2026-05-23): "002-002-000014038" se subrayaba en verde como tap-to-call.
_SRI_DOC_PATTERN = _re.compile(r"^\d{2,4}-\d{2,4}-\d{6,12}$")


def _safe_doc_name(name: Any) -> str:
    """Disable phone-number auto-link for SRI invoice numbers."""
    if not name:
        return ""
    s = str(name).strip()
    if _SRI_DOC_PATTERN.fullmatch(s):
        return s.replace("-", ".")
    return s


def _fmt_overdue_tag(days_overdue: Any) -> str:
    """Render a ``_(vencida hace N días)_`` / ``_(vence en N días)_`` tag.

    Returns empty string when ``days_overdue`` is None or 0.
    """
    try:
        d = int(days_overdue) if days_overdue is not None else None
    except (TypeError, ValueError):
        return ""
    if d is None or d == 0:
        return ""
    if d > 0:
        return f"_(vencida hace {d} día{'s' if d != 1 else ''})_"
    n = abs(d)
    return f"_(vence en {n} día{'s' if n != 1 else ''})_"


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

def format_invoices_list(
    invoices: list[dict] | None,
    total_amount: float | None = None,
    total_residual: float | None = None,
    state_filter: str | None = None,
) -> str:
    """Render ``odoo_get_customer_invoices`` for chat channels.

    ``invoices`` is the ``invoices`` array of the envelope. Each item
    carries ``name, invoice_date, invoice_date_due, days_overdue,
    amount_total, amount_residual, payment_state_label, ref, type_label,
    portal_url``.

    ``state_filter`` is the raw filter value ("all" / "paid" / ...). We
    translate it to ES for the header.
    """
    invoices = invoices or []
    filter_label = _STATE_FILTER_LABEL_ES.get(
        (state_filter or "all").lower(), state_filter or "",
    )

    if not invoices:
        if filter_label and filter_label != "todas":
            return f"🧾 No tienes facturas {filter_label} registradas."
        return "🧾 No tienes facturas registradas."

    visible, hidden = _truncate(invoices)
    n_shown = len(visible)
    if filter_label and filter_label != "todas":
        head = (
            f"🧾 *Tu {n_shown} factura {filter_label}*"
            if n_shown == 1
            else f"🧾 *Tus {n_shown} facturas {filter_label}*"
        )
    else:
        head = (
            f"🧾 *Tu {n_shown} factura* (más reciente primero)"
            if n_shown == 1
            else f"🧾 *Tus {n_shown} facturas* (más reciente primero)"
        )

    lines: list[str] = [head]

    # Summary line — total + residual when meaningful.
    bits: list[str] = []
    if total_amount is not None:
        bits.append(f"total {_fmt_total(total_amount)}")
    if total_residual is not None and float(total_residual or 0) > 0:
        bits.append(f"pendiente {_fmt_total(total_residual)}")
    if bits:
        lines.append("_" + " · ".join(bits) + "_")

    for inv in visible:
        name = _safe_doc_name(inv.get("name")) or "(sin número)"
        amount_total = inv.get("amount_total")
        amount_residual = inv.get("amount_residual")
        state_lbl = (inv.get("payment_state_label") or "").strip()
        type_lbl = (inv.get("type_label") or "").strip()
        date_str = _fmt_date_compact(inv.get("invoice_date"), with_time=False)
        due_str = _fmt_date_compact(inv.get("invoice_date_due"), with_time=False)
        days_over = inv.get("days_overdue")
        ref = (inv.get("ref") or "").strip()

        # Header row.
        head_bits = [f"*{name}* — {_fmt_total(amount_total)}"]
        if state_lbl:
            head_bits.append(f"_{state_lbl}_")
        lines.append(" · ".join(head_bits))

        # Subline 1: dates + days overdue tag.
        sub1: list[str] = []
        if date_str:
            sub1.append(f"emitida {date_str}")
        if due_str:
            sub1.append(f"vence {due_str}")
        # Only surface the overdue tag when the invoice is actually unpaid.
        try:
            residual_f = float(amount_residual) if amount_residual is not None else 0.0
        except (TypeError, ValueError):
            residual_f = 0.0
        if residual_f > 0:
            tag = _fmt_overdue_tag(days_over)
            if tag:
                sub1.append(tag)
        if sub1:
            lines.append("   " + " · ".join(sub1))

        # Subline 2: residual + ref + type (when non-default).
        sub2: list[str] = []
        if residual_f > 0:
            sub2.append(f"pendiente {_fmt_total(residual_f)}")
        if ref:
            sub2.append(f"ref {ref}")
        if type_lbl and type_lbl not in ("factura", ""):
            sub2.append(type_lbl)
        if sub2:
            lines.append("   " + " · ".join(sub2))

    if hidden:
        lines.append(f"_y {hidden} más._ " + _MORE_HINT)

    lines.append("Dime el número (ej.: _" +
                 (_safe_doc_name(visible[0].get("name")) or "FACV/...") +
                 "_) para ver el detalle o el PDF.")
    return "\n\n".join(lines)


def format_invoice_detail(
    invoice: dict | None,
    lines: list[dict] | None = None,
    taxes: list[dict] | None = None,
) -> str:
    """Render a single invoice with optional lines + tax breakdown."""
    if not invoice or not isinstance(invoice, dict):
        return "🧾 No pude leer esa factura."

    name = _safe_doc_name(invoice.get("name")) or "(sin número)"
    amount_total = invoice.get("amount_total")
    amount_residual = invoice.get("amount_residual")
    amount_untaxed = invoice.get("amount_untaxed")
    amount_tax = invoice.get("amount_tax")
    payment_lbl = (invoice.get("payment_state_label") or "").strip()
    type_lbl = (invoice.get("type_label") or "").strip()
    date_str = _fmt_date_compact(invoice.get("invoice_date"), with_time=False)
    due_str = _fmt_date_compact(invoice.get("invoice_date_due"), with_time=False)
    days_over = invoice.get("days_overdue")
    ref = (invoice.get("ref") or "").strip()
    portal_url = (invoice.get("portal_url") or "").strip()

    out: list[str] = []
    head_bits = [f"🧾 *{name}* — {_fmt_total(amount_total)}"]
    if payment_lbl:
        head_bits.append(f"_{payment_lbl}_")
    out.append(" · ".join(head_bits))

    meta_bits: list[str] = []
    if type_lbl and type_lbl != "factura":
        meta_bits.append(type_lbl)
    if date_str:
        meta_bits.append(f"emitida {date_str}")
    if due_str:
        meta_bits.append(f"vence {due_str}")
    try:
        residual_f = float(amount_residual) if amount_residual is not None else 0.0
    except (TypeError, ValueError):
        residual_f = 0.0
    if residual_f > 0:
        tag = _fmt_overdue_tag(days_over)
        if tag:
            meta_bits.append(tag)
    if ref:
        meta_bits.append(f"ref {ref}")
    if meta_bits:
        out.append("   " + " · ".join(meta_bits))

    if lines:
        out.append("*Productos:*")
        visible, hidden = _truncate(lines)
        for ln in visible:
            qty = ln.get("quantity") or 0
            try:
                qty_f = float(qty)
                qty_str = (
                    str(int(qty_f)) if qty_f == int(qty_f) else f"{qty_f:.2f}"
                )
            except (TypeError, ValueError):
                qty_str = "1"
            prod = ln.get("product")
            prod_name = ""
            if isinstance(prod, dict):
                prod_name = (prod.get("name") or "").strip()
            line_name = prod_name or (ln.get("name") or "").strip() or "(sin nombre)"
            price = ln.get("price_unit")
            sub = ln.get("price_subtotal")
            discount = ln.get("discount")

            out.append(f"• {qty_str} × *{line_name}*")
            mini: list[str] = []
            if price not in (None, ""):
                mini.append(f"c/u {_fmt_total(price)}")
            if sub not in (None, ""):
                mini.append(f"subtotal {_fmt_total(sub)}")
            try:
                if discount and float(discount) > 0:
                    mini.append(f"desc {float(discount):.1f}%")
            except (TypeError, ValueError):
                pass
            if mini:
                out.append("   " + " · ".join(mini))
        if hidden:
            out.append(f"_y {hidden} línea(s) más._")

    if taxes:
        tax_bits: list[str] = []
        for t in taxes:
            tname = (t.get("name") or "").strip() or "IVA"
            tamt = t.get("tax_amount") or 0
            tax_bits.append(f"{tname}: {_fmt_total(tamt)}")
        if tax_bits:
            out.append("*Impuestos:* " + " · ".join(tax_bits))

    totals_bits: list[str] = []
    if amount_untaxed not in (None, ""):
        totals_bits.append(f"subtotal {_fmt_total(amount_untaxed)}")
    if amount_tax not in (None, "") and amount_tax:
        totals_bits.append(f"impuesto {_fmt_total(amount_tax)}")
    if amount_total not in (None, ""):
        totals_bits.append(f"*total {_fmt_total(amount_total)}*")
    if residual_f > 0:
        totals_bits.append(f"_pendiente {_fmt_total(residual_f)}_")
    if totals_bits:
        out.append(" · ".join(totals_bits))

    if portal_url:
        out.append(f"📎 PDF: {portal_url}")

    return "\n\n".join(out)


def format_payments_list(
    payments: list[dict] | dict | None,
) -> str:
    """Render ``odoo_get_customer_payments`` for chat channels.

    Accepts the full envelope (``count, total_amount, payments``) or a
    plain list of payment dicts.
    """
    if payments is None:
        return "💳 No tienes pagos registrados."

    if isinstance(payments, dict):
        items = payments.get("payments") or []
        total = payments.get("total_amount")
    else:
        items = list(payments)
        total = None

    if not items:
        return "💳 No tienes pagos registrados."

    visible, hidden = _truncate(items)
    n_shown = len(visible)
    head = (
        f"💳 *{n_shown} pago registrado*"
        if n_shown == 1
        else f"💳 *{n_shown} pagos recientes*"
    )

    lines: list[str] = [head]
    if total is not None and float(total or 0) > 0:
        lines.append(f"_total {_fmt_total(total)}_")

    for p in visible:
        name = _safe_doc_name((p.get("name") or "").strip()) or "(sin número)"
        amount = p.get("amount")
        journal = (p.get("journal") or "").strip()
        date_str = _fmt_date_compact(p.get("payment_date"), with_time=False)
        applied = p.get("applied_to") or []
        applied_names = [
            _safe_doc_name(
                (a.get("name") if isinstance(a, dict) else str(a)).strip()
            )
            for a in applied if a
        ]
        applied_names = [n for n in applied_names if n]

        head_bits = [f"*{name}* — {_fmt_total(amount)}"]
        if journal:
            head_bits.append(f"_{journal}_")
        lines.append(" · ".join(head_bits))

        sub_bits: list[str] = []
        if date_str:
            sub_bits.append(date_str)
        if applied_names:
            shown = ", ".join(applied_names[:3])
            extra = len(applied_names) - 3
            if extra > 0:
                shown += f" (+{extra})"
            sub_bits.append(f"aplicado a {shown}")
        if sub_bits:
            lines.append("   " + " · ".join(sub_bits))

    if hidden:
        lines.append(f"_y {hidden} más._ " + _MORE_HINT)

    return "\n\n".join(lines)


def format_statement_summary(
    summary: dict | None,
    recent_movements: list[dict] | None = None,
    period: dict | None = None,
) -> str:
    """Render ``odoo_get_customer_statement`` for chat channels."""
    if not summary or not isinstance(summary, dict):
        return "💰 No pude generar tu estado de cuenta."

    period = period or {}
    p_from = (period.get("from") or "").strip()
    p_to = (period.get("to") or "").strip()
    p_from_str = _fmt_date_compact(p_from, with_time=False) if p_from else ""
    p_to_str = _fmt_date_compact(p_to, with_time=False) if p_to else ""

    lines: list[str] = ["💰 *Estado de cuenta*"]
    if p_from_str and p_to_str:
        lines.append(f"_periodo {p_from_str} — {p_to_str}_")

    period_bits: list[str] = []
    total_billed = summary.get("total_billed")
    total_paid = summary.get("total_paid")
    invoices_in_period = summary.get("invoices_in_period")
    if total_billed not in (None, "") and float(total_billed or 0):
        period_bits.append(f"facturado {_fmt_total(total_billed)}")
    if total_paid not in (None, "") and float(total_paid or 0):
        period_bits.append(f"pagado {_fmt_total(total_paid)}")
    if invoices_in_period:
        period_bits.append(
            f"{invoices_in_period} factura"
            + ("s" if int(invoices_in_period) != 1 else "")
            + " en el periodo"
        )
    if period_bits:
        lines.append("*En el periodo:* " + " · ".join(period_bits))

    open_bits: list[str] = []
    total_due_now = summary.get("total_due_now")
    total_overdue_now = summary.get("total_overdue_now")
    invoices_overdue = summary.get("invoices_overdue")
    if total_due_now not in (None, "") and float(total_due_now or 0):
        open_bits.append(f"saldo {_fmt_total(total_due_now)}")
    if total_overdue_now not in (None, "") and float(total_overdue_now or 0):
        open_bits.append(f"vencido {_fmt_total(total_overdue_now)}")
    if invoices_overdue:
        open_bits.append(
            f"{invoices_overdue} factura"
            + ("s" if int(invoices_overdue) != 1 else "")
            + " vencida"
            + ("s" if int(invoices_overdue) != 1 else "")
        )
    if open_bits:
        lines.append("*Pendientes hoy:* " + " · ".join(open_bits))

    avg_days = summary.get("avg_payment_days")
    if avg_days is not None:
        try:
            avg_f = float(avg_days)
            lines.append(
                f"_pagas en promedio en {avg_f:.1f} días_"
            )
        except (TypeError, ValueError):
            pass

    movements = recent_movements or []
    if movements:
        lines.append("*Últimos movimientos:*")
        visible, hidden = _truncate(movements)
        for m in visible:
            mtype = (m.get("type") or "").strip()
            name = _safe_doc_name(m.get("name")) or "(sin número)"
            amount = m.get("amount") or 0
            date_str = _fmt_date_compact(m.get("date"), with_time=False)
            arrow = "▪"
            if mtype == "invoice":
                arrow = "🧾"
            elif mtype == "payment":
                arrow = "💳"
            try:
                amt_f = float(amount)
            except (TypeError, ValueError):
                amt_f = 0.0
            sign = "-" if amt_f < 0 else ""
            amt_disp = _fmt_total(abs(amt_f))
            row = f"{arrow} *{name}* · {sign}{amt_disp}"
            if date_str:
                row += f" · {date_str}"
            lines.append(row)
        if hidden:
            lines.append(f"_y {hidden} movimiento(s) más._")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# ETA iter 81 — PDF download formatters
# ---------------------------------------------------------------------------
# Each formatter takes the tool result envelope and returns a short
# message the LLM can copy verbatim to the customer. The PDF URL goes on
# its own line so WhatsApp/Telegram render it as a clickable preview.


def _fmt_kb(size_bytes: Any) -> str:
    """Render byte count as 'NN KB' (rounded). Returns '' on bad input."""
    try:
        b = int(size_bytes)
    except (TypeError, ValueError):
        return ""
    if b <= 0:
        return ""
    if b < 1024:
        return f"{b} B"
    kb = b / 1024.0
    if kb < 1024:
        return f"{kb:.0f} KB"
    return f"{kb / 1024.0:.1f} MB"


def format_statement_pdf(result: dict | None) -> str:
    """Render ``get_customer_statement_pdf`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        return "📄 No pude generar el PDF de tu estado de cuenta."
    pdf_url = (result.get("pdf_url") or "").strip()
    if not pdf_url:
        return "📄 El estado de cuenta se generó pero no tengo URL para enviártelo."

    size_str = _fmt_kb(result.get("pdf_size_bytes"))
    generated_at = (result.get("generated_at_local") or "").strip()
    expires_at = (result.get("expires_at") or "").strip()[:10]

    lines: list[str] = ["📄 *Tu estado de cuenta oficial*"]
    meta_bits: list[str] = []
    if generated_at:
        meta_bits.append(f"Generado: {generated_at}")
    fmt_bits = ["PDF"]
    if size_str:
        fmt_bits.append(f"({size_str})")
    meta_bits.append(
        " ".join(fmt_bits) +
        " — incluye facturas pendientes, cuentas bancarias y todo lo que "
        "necesitas para pagar."
    )
    if meta_bits:
        lines.append("\n".join(meta_bits))

    lines.append(f"*Descargar:* {pdf_url}")

    footer_bits: list[str] = [
        "Mismo documento que recibes por correo cada 15 días."
    ]
    if expires_at:
        footer_bits.append(f"_Enlace activo hasta {expires_at}._")
    lines.append("_" + " ".join(footer_bits).strip("_ ") + "_")

    return "\n\n".join(lines)


def format_invoice_pdf(result: dict | None) -> str:
    """Render ``get_invoice_pdf`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        return "🧾 No pude generar el PDF de la factura."
    pdf_url = (result.get("pdf_url") or "").strip()
    if not pdf_url:
        return "🧾 Generé el PDF pero no tengo URL para enviártelo."
    name = (result.get("invoice_name") or "").strip()
    size_str = _fmt_kb(result.get("pdf_size_bytes"))

    title = f"🧾 *RIDE oficial — {name}*" if name else "🧾 *RIDE oficial*"
    lines: list[str] = [title]
    if size_str:
        lines.append(f"_Formato PDF ({size_str})._")
    lines.append(f"*Descargar:* {pdf_url}")
    lines.append("_Versión SRI Ecuador con clave de acceso y firma electrónica._")
    return "\n\n".join(lines)


def format_credit_note_pdf(result: dict | None) -> str:
    """Render ``get_credit_note_pdf`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        return "📋 No pude generar el PDF de la nota de crédito."
    pdf_url = (result.get("pdf_url") or "").strip()
    if not pdf_url:
        return "📋 Generé el PDF pero no tengo URL para enviártelo."
    name = (result.get("invoice_name") or "").strip()
    size_str = _fmt_kb(result.get("pdf_size_bytes"))

    title = f"📋 *Nota de crédito — {name}*" if name else "📋 *Nota de crédito*"
    lines: list[str] = [title]
    if size_str:
        lines.append(f"_Formato PDF ({size_str})._")
    lines.append(f"*Descargar:* {pdf_url}")
    lines.append("_Versión SRI Ecuador con clave de acceso y firma electrónica._")
    return "\n\n".join(lines)


def format_retention_pdf(result: dict | None) -> str:
    """Render ``get_retention_pdf`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        return "🧾 No pude generar el PDF de la retención."
    pdf_url = (result.get("pdf_url") or "").strip()
    if not pdf_url:
        return "🧾 Generé el PDF pero no tengo URL para enviártelo."
    name = (result.get("retention_name") or "").strip()
    size_str = _fmt_kb(result.get("pdf_size_bytes"))

    title = f"🧾 *Comprobante de retención — {name}*" if name else "🧾 *Comprobante de retención*"
    lines: list[str] = [title]
    if size_str:
        lines.append(f"_Formato PDF ({size_str})._")
    lines.append(f"*Descargar:* {pdf_url}")
    lines.append("_Versión SRI Ecuador con clave de acceso y firma electrónica._")
    return "\n\n".join(lines)


__all__ = [
    "format_invoices_list",
    "format_invoice_detail",
    "format_payments_list",
    "format_statement_summary",
    # ETA iter 81 — PDF download formatters
    "format_statement_pdf",
    "format_invoice_pdf",
    "format_credit_note_pdf",
    "format_retention_pdf",
]
