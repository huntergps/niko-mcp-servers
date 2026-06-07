"""WhatsApp/Telegram formatter for billing tools (``create_invoice``).

Same contract as ``mcp_odoo.formatters.whatsapp``:
  * never emit pipes ``|``, triple dashes ``---`` or tabs
  * ``*bold*`` / ``_italic_`` only
  * one blank line between blocks
  * prices via ``_fmt_total`` (BPE-safe thousands separator)

These formatters never raise — a render error must not break a successful
tool call. The caller (``_attach_display_text``) swallows exceptions anyway.
"""
from __future__ import annotations

from typing import Any

from mcp_odoo.formatters.whatsapp import _fmt_total


def format_invoice_created(result: dict | None) -> str:
    """Render the ``create_invoice`` envelope for chat channels.

    Confirms the invoice was created as a DRAFT, shows the total, and makes
    clear that the Afrodita team will review it and emit it at the SRI.
    """
    if not isinstance(result, dict) or not result.get("success"):
        if isinstance(result, dict):
            detail = (result.get("error_detail") or "").strip()
            if detail:
                return f"⚠️ {detail}"
        return "⚠️ No pude crear la factura en este momento."

    partner_name = (result.get("partner_name") or "").strip()
    name = (result.get("name") or "").strip()
    total = result.get("amount_total")
    lines = result.get("lines") or []

    out: list[str] = ["🧾 *Creé tu factura en borrador.*"]

    head_bits: list[str] = []
    if partner_name:
        head_bits.append(f"Cliente: *{partner_name}*")
    if name and name != "/":
        head_bits.append(f"Documento: *{name}*")
    if head_bits:
        out.append("\n".join(head_bits))

    detail_lines: list[str] = []
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        item = (str(ln.get("item") or "")).strip() or "(ítem)"
        try:
            qty = float(ln.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1.0
        qty_disp = f"{qty:g}"
        detail_lines.append(f"• {qty_disp} x *{item}*")
    if detail_lines:
        out.append("\n".join(detail_lines))

    if total is not None:
        out.append(f"Total: *{_fmt_total(total)}*")

    out.append(
        "_El equipo de Afrodita la revisará y la emitirá. Quedó como "
        "borrador; todavía no está autorizada en el SRI._"
    )
    return "\n\n".join(out)


__all__ = ["format_invoice_created"]
