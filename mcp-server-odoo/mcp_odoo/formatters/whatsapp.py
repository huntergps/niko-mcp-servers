"""Plain-text formatters for chat channels (WhatsApp, Telegram).

These channels render markdown but NOT tables — pipes ``|`` and dashes
``---`` show up literally on the customer's screen. Every renderer here
produces a string that uses only what WhatsApp/Telegram understand:

  * ``*texto*`` -> bold
  * ``_texto_`` -> italic
  * line breaks for separation
  * bullet glyphs: ``•`` ``▪``
  * one optional opening emoji (📋 cotizaciones, 🛒 productos,
    📦 historial)

Conventions:
  * one blank line between items
  * date format ``DD-mmm HH:MM`` (year omitted when current)
  * prices ``USD 1,234.56`` (comma thousands separator — see
    ``mcp_odoo.tools.formatters.format_price_display`` for the BPE
    tokenizer rationale)
  * max ~10 items per list; the renderer appends a ``y N más`` line
    when more remain
  * empty-total quotations marked ``_(vacía, sin productos)_``

Nothing here calls an LLM, an HTTP service, or Odoo — pure string
formatting from a dict. Safe to import in tests.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from mcp_odoo.tools.formatters import format_price_display


_MONTHS_ES = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
]

# Soft cap. Lists longer than this get truncated with a "y N más" note.
_MAX_ITEMS_PER_LIST = 10

# Hint shown when a list is paginated and more items remain.
_MORE_HINT = "_dime «siguiente» para ver más_"


def _parse_dt(raw: Any) -> datetime | None:
    """Parse Odoo datetime strings ("YYYY-MM-DD HH:MM:SS") or date strings.

    Returns None for anything we can't parse. The formatter must NEVER
    raise — we'd rather show a slightly ugly date than break the user's
    message.
    """
    if raw in (None, "", False):
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # Try common formats: full datetime, then date-only.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 0], fmt)
        except ValueError:
            continue
    # Last attempt: cut to the first 19 chars and retry full datetime.
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _fmt_date_compact(raw: Any, *, with_time: bool = True) -> str:
    """``22-may 12:49`` style. Year added when not the current one.

    Returns ``""`` when the input cannot be parsed (caller decides whether
    to skip the field entirely).
    """
    dt = _parse_dt(raw)
    if dt is None:
        return ""
    month = _MONTHS_ES[dt.month - 1]
    today = date.today()
    if dt.year == today.year:
        base = f"{dt.day:02d}-{month}"
    else:
        base = f"{dt.day:02d}-{month}-{dt.year}"
    if with_time and (dt.hour or dt.minute or dt.second):
        base += f" {dt.hour:02d}:{dt.minute:02d}"
    return base


def _fmt_total(amount: Any) -> str:
    """``USD 63.88`` or ``consultar`` for None. Reuses the BPE-safe
    formatter so prices keep the comma thousands separator that prevents
    the leading-digit drop bug on Qwen tokenizers."""
    return format_price_display(amount)


def _is_empty_quotation(total: Any, lines_count: Any) -> bool:
    try:
        t = float(total) if total is not None else 0.0
    except (TypeError, ValueError):
        t = 0.0
    try:
        lc = int(lines_count) if lines_count is not None else 0
    except (TypeError, ValueError):
        lc = 0
    return t == 0.0 and lc == 0


_STATE_LABEL_ES = {
    "draft": "borrador",
    "sent": "enviada",
    "sale": "confirmada",
    "done": "completada",
    "cancel": "cancelada",
}


def _state_label_es(state: str | None) -> str:
    if not state:
        return ""
    return _STATE_LABEL_ES.get(state.lower(), state)


def _truncate(items: list[Any], cap: int = _MAX_ITEMS_PER_LIST) -> tuple[list[Any], int]:
    """Cap a list to ``cap`` items and return (visible, hidden_count)."""
    if cap <= 0 or len(items) <= cap:
        return list(items), 0
    return list(items[:cap]), len(items) - cap


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

def format_quotations_list(
    orders: list[dict] | None,
    partner_name: str | None = None,
    state_filter: str | None = None,
) -> str:
    """Render the result of ``odoo_list_quotations`` for chat channels.

    ``orders`` is the ``orders`` array of the tool envelope (each item has
    ``order_id, name, state, state_label, total, subtotal, date_order,
    lines_count``).

    ``state_filter`` is a free-form label like ``"borrador"`` for the
    title (e.g. "Tus 3 cotizaciones *en borrador*"). When None, the
    title says simply "tus cotizaciones".
    """
    orders = orders or []
    if not orders:
        if partner_name:
            return f"📋 *{partner_name}*, no tienes cotizaciones registradas."
        return "📋 No tienes cotizaciones registradas."

    visible, hidden = _truncate(orders)
    n_shown = len(visible)
    suffix = ""
    if state_filter:
        suffix = f" en *{state_filter}*"

    who = f" de *{partner_name}*" if partner_name else ""
    header = (
        f"📋 *Tus {n_shown} cotización* (más reciente primero){suffix}"
        if n_shown == 1
        else f"📋 *Tus {n_shown} cotizaciones*{who}{suffix} (más reciente primero)"
    )

    lines: list[str] = [header]
    for o in visible:
        name = (o.get("name") or "").strip() or "(sin número)"
        total = o.get("total")
        lines_count = o.get("lines_count") or 0
        date_str = _fmt_date_compact(o.get("date_order"))
        state = (o.get("state") or "").strip().lower()

        # Header row: bold name + total (or empty-quote tag).
        if _is_empty_quotation(total, lines_count):
            head = f"*{name}* — _{'vacía, sin productos'}_"
        else:
            head = f"*{name}* — {_fmt_total(total)}"

        # Subline: lines count + date + state (skip default 'draft' to
        # avoid noise; everything else is informative).
        parts: list[str] = []
        try:
            lc_int = int(lines_count) if lines_count is not None else 0
        except (TypeError, ValueError):
            lc_int = 0
        if lc_int == 1:
            parts.append("1 línea")
        elif lc_int > 1:
            parts.append(f"{lc_int} líneas")
        if date_str:
            parts.append(date_str)
        if state and state != "draft":
            label = _state_label_es(state)
            if label:
                parts.append(label)

        sub = "   " + " · ".join(parts) if parts else ""
        item = head + ("\n" + sub if sub else "")
        lines.append(item)

    if hidden:
        lines.append(f"_y {hidden} más._ " + _MORE_HINT)

    lines.append(
        "Dime el número (ej.: _" + (visible[0].get("name") or "VENTA…") +
        "_) para ver detalle o generar PDF."
    )
    return "\n\n".join(lines)


def format_products_list(
    products: list[dict] | None,
    query: str | None = None,
) -> str:
    """Render a list of products (search_products / generic catalog rows).

    Each product dict can hold any of: ``code, name, price, qty,
    in_stock, category, line_text``. When ``line_text`` is already
    pre-formatted (as in the RAG response), we trust it verbatim and
    only stitch a header + footer.
    """
    products = products or []
    if not products:
        if query:
            return (
                f"🛒 No encontré productos para _{query}_. "
                "Dime con qué frase quieres que vuelva a buscar."
            )
        return "🛒 No encontré productos para esa búsqueda."

    visible, hidden = _truncate(products)
    n_shown = len(visible)
    header = (
        f"🛒 *{n_shown} producto* encontrado"
        if n_shown == 1
        else f"🛒 *{n_shown} productos* encontrados"
    )
    if query:
        header += f" para _{query}_"

    lines: list[str] = [header]
    for p in visible:
        pre = (p.get("line_text") or "").strip()
        if pre:
            lines.append(pre)
            continue

        code = (p.get("code") or "").strip()
        name = (p.get("name") or "").strip() or "(sin nombre)"
        price = p.get("price")
        qty = p.get("qty")
        in_stock = p.get("in_stock")

        title_bits = [f"*{name}*"]
        if code:
            title_bits.append(f"_{code}_")
        title = " · ".join(title_bits)

        sub_bits: list[str] = []
        if price not in (None, "", 0):
            sub_bits.append(_fmt_total(price))
        elif price == 0:
            sub_bits.append(_fmt_total(0))
        if qty is not None:
            try:
                q_f = float(qty)
                if q_f > 0:
                    q_str = str(int(q_f)) if q_f == int(q_f) else f"{q_f:.2f}"
                    sub_bits.append(f"{q_str} en stock")
                else:
                    sub_bits.append("agotado")
            except (TypeError, ValueError):
                pass
        elif in_stock is False:
            sub_bits.append("agotado")

        sub = "   " + " · ".join(sub_bits) if sub_bits else ""
        item = title + ("\n" + sub if sub else "")
        lines.append(item)

    if hidden:
        lines.append(f"_y {hidden} más._ " + _MORE_HINT)

    lines.append("Dime el nombre o código para ver detalle o cotizar.")
    return "\n\n".join(lines)


def format_purchase_history(
    history: dict | None,
) -> str:
    """Render ``odoo_get_customer_purchase_history`` for chat channels.

    Accepts the full envelope (with ``partner_name, period, orders_count,
    total_amount, avg_ticket, top_products, recent_orders``) so we can
    show a brief summary on top of the recent orders.

    Also accepts a plain list of orders for compatibility — the caller
    can pass ``result["recent_orders"]`` directly and we'll skip the
    summary.
    """
    if history is None:
        return "📦 No tienes historial de compras registrado."

    # Tolerate list-only input.
    if isinstance(history, list):
        recent = history
        partner_name = None
        summary_line: str | None = None
    else:
        recent = history.get("recent_orders") or []
        partner_name = history.get("partner_name") or None
        period = history.get("period") or ""
        orders_count = history.get("orders_count") or 0
        total_amount = history.get("total_amount") or 0
        avg_ticket = history.get("avg_ticket") or 0
        summary_bits = []
        if orders_count:
            summary_bits.append(
                f"{orders_count} {'orden' if orders_count == 1 else 'órdenes'}"
            )
        if total_amount:
            summary_bits.append(f"total {_fmt_total(total_amount)}")
        if avg_ticket:
            summary_bits.append(f"ticket promedio {_fmt_total(avg_ticket)}")
        summary_line = (
            f"_{period}: " + " · ".join(summary_bits) + "_"
            if summary_bits else None
        )

    if not recent:
        if partner_name:
            return f"📦 *{partner_name}*, no tienes compras registradas en el periodo."
        return "📦 No tienes compras registradas en el periodo."

    visible, hidden = _truncate(recent)
    n_shown = len(visible)
    who = f" de *{partner_name}*" if partner_name else ""
    header = (
        f"📦 *Tu compra reciente*{who}"
        if n_shown == 1
        else f"📦 *Tus {n_shown} compras recientes*{who}"
    )

    lines: list[str] = [header]
    if summary_line:
        lines.append(summary_line)

    for o in visible:
        name = (o.get("name") or "").strip() or "(sin número)"
        amount = o.get("amount") if "amount" in o else o.get("amount_total")
        state = (o.get("state") or "").strip().lower()
        date_str = _fmt_date_compact(o.get("date") or o.get("date_order"), with_time=False)

        head = f"*{name}* — {_fmt_total(amount)}"
        sub_bits: list[str] = []
        if date_str:
            sub_bits.append(date_str)
        if state and state not in ("sale",):
            label = _state_label_es(state)
            if label:
                sub_bits.append(label)
        sub = "   " + " · ".join(sub_bits) if sub_bits else ""
        lines.append(head + ("\n" + sub if sub else ""))

    if hidden:
        lines.append(f"_y {hidden} más._ " + _MORE_HINT)

    return "\n\n".join(lines)


def format_pending_quotations(
    quotations: list[dict] | dict | None,
) -> str:
    """Render ``odoo_get_pending_quotations`` for chat channels.

    Accepts either the full envelope (with ``total, expired,
    expiring_soon, active, quotations``) or just the ``quotations``
    list. Categorizes each row by ``status`` (expirada / por_vencer /
    vigente).
    """
    if quotations is None:
        return "📋 No tienes cotizaciones pendientes de respuesta."

    if isinstance(quotations, list):
        items = quotations
        summary: dict[str, Any] = {}
    else:
        items = quotations.get("quotations") or []
        summary = quotations

    if not items:
        return "📋 No tienes cotizaciones pendientes de respuesta."

    visible, hidden = _truncate(items)
    n_shown = len(visible)
    header = (
        f"📋 *{n_shown} cotización pendiente* de respuesta"
        if n_shown == 1
        else f"📋 *{n_shown} cotizaciones pendientes* de respuesta"
    )

    summary_parts = []
    if summary.get("expired"):
        summary_parts.append(f"{summary['expired']} expiradas")
    if summary.get("expiring_soon"):
        summary_parts.append(f"{summary['expiring_soon']} por vencer")
    if summary.get("active"):
        summary_parts.append(f"{summary['active']} vigentes")
    summary_line = (
        "_" + " · ".join(summary_parts) + "_" if summary_parts else None
    )

    lines: list[str] = [header]
    if summary_line:
        lines.append(summary_line)

    for q in visible:
        name = (q.get("name") or "").strip() or "(sin número)"
        amount = q.get("amount_total")
        partner = (q.get("partner") or "").strip()
        days = q.get("days_pending")
        status = (q.get("status") or "").strip().lower()
        validity = _fmt_date_compact(q.get("validity_date"), with_time=False)
        date_sent = _fmt_date_compact(q.get("date_sent"), with_time=False)

        head = f"*{name}* — {_fmt_total(amount)}"
        if partner:
            head += f"\n   {partner}"

        sub_bits: list[str] = []
        if date_sent:
            sub_bits.append(f"enviada {date_sent}")
        try:
            d_int = int(days) if days is not None else 0
        except (TypeError, ValueError):
            d_int = 0
        if d_int > 0:
            sub_bits.append(
                f"{d_int} día pendiente" if d_int == 1 else f"{d_int} días pendiente"
            )
        if validity:
            sub_bits.append(f"vence {validity}")
        if status == "expirada":
            sub_bits.append("_expirada_")
        elif status == "por_vencer":
            sub_bits.append("_por vencer_")

        sub = "   " + " · ".join(sub_bits) if sub_bits else ""
        lines.append(head + ("\n" + sub if sub else ""))

    if hidden:
        lines.append(f"_y {hidden} más._ " + _MORE_HINT)

    return "\n\n".join(lines)


def format_quotation_detail(quotation: dict | None) -> str:
    """Render the result of ``odoo_get_quotation`` (single quotation).

    Optional but useful: lets the LLM copy a clean detail block when the
    user asks "muéstrame la VENTA…".
    """
    if not quotation or not isinstance(quotation, dict):
        return "📋 No pude leer esa cotización."

    name = (quotation.get("name") or "").strip() or "(sin número)"
    state = (quotation.get("state") or "").strip().lower()
    total = quotation.get("total")
    subtotal = quotation.get("subtotal")
    tax = quotation.get("tax")
    date_str = _fmt_date_compact(quotation.get("date_order"))

    partner_raw = quotation.get("partner")
    if isinstance(partner_raw, dict):
        partner_name = partner_raw.get("name") or ""
    elif isinstance(partner_raw, str):
        partner_name = partner_raw
    else:
        partner_name = ""

    lines: list[str] = [f"📋 *{name}* — {_fmt_total(total)}"]

    meta_bits: list[str] = []
    if partner_name:
        meta_bits.append(partner_name.strip())
    if date_str:
        meta_bits.append(date_str)
    if state:
        label = _state_label_es(state)
        if label:
            meta_bits.append(label)
    if meta_bits:
        lines.append("   " + " · ".join(meta_bits))

    items = quotation.get("lines") or []
    if isinstance(items, list) and items:
        lines.append("*Productos:*")
        visible, hidden = _truncate(items)
        for ln in visible:
            qty = ln.get("quantity") or 0
            try:
                qty_int = int(qty) if float(qty) == int(float(qty)) else None
            except (TypeError, ValueError):
                qty_int = None
            qty_str = str(qty_int) if qty_int is not None else f"{float(qty or 0):.2f}"
            name_ln = (ln.get("product") or ln.get("name") or "").strip()
            code_ln = (ln.get("code") or "").strip()
            price = ln.get("price_unit")
            sub_ln = ln.get("subtotal") or ln.get("total")
            head_bits = [f"• {qty_str} × *{name_ln or '(sin nombre)'}*"]
            if code_ln:
                head_bits.append(f"_{code_ln}_")
            lines.append(" ".join(head_bits))
            mini: list[str] = []
            if price not in (None, "", 0):
                mini.append(f"c/u {_fmt_total(price)}")
            elif price == 0:
                mini.append(f"c/u {_fmt_total(0)}")
            if sub_ln not in (None, "", 0):
                mini.append(f"subtotal {_fmt_total(sub_ln)}")
            elif sub_ln == 0:
                mini.append(f"subtotal {_fmt_total(0)}")
            if mini:
                lines.append("   " + " · ".join(mini))
        if hidden:
            lines.append(f"_y {hidden} línea(s) más._")

    totals_bits: list[str] = []
    if subtotal not in (None, ""):
        totals_bits.append(f"subtotal {_fmt_total(subtotal)}")
    if tax not in (None, "") and tax:
        totals_bits.append(f"impuesto {_fmt_total(tax)}")
    if total not in (None, ""):
        totals_bits.append(f"*total {_fmt_total(total)}*")
    if totals_bits:
        lines.append(" · ".join(totals_bits))

    return "\n\n".join(lines)


__all__ = [
    "format_quotations_list",
    "format_products_list",
    "format_purchase_history",
    "format_pending_quotations",
    "format_quotation_detail",
]
