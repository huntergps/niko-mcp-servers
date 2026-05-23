"""Tests for the WhatsApp/Telegram plain-text formatters.

These formatters exist because WhatsApp does NOT render markdown tables —
the pipes ``|`` and dashes ``---`` show up literally on the customer's
screen. Bug ticket: customer screenshot 2026-05-22 of a markdown-table
quotation list rendered as raw ASCII.

The contract these tests lock:
  * never emit ``|`` or ``---``
  * never emit tabs
  * use ``*bold*`` and ``_italic_`` (WhatsApp markdown)
  * one blank line between items
  * cap at 10 items with "y N más" footer
  * empty quotations explicitly flagged
  * date format ``DD-mmm[-YYYY] HH:MM``
  * prices ``USD 1,234.56`` (comma separator — BPE tokenizer fix)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from mcp_odoo.formatters.whatsapp import (
    format_pending_quotations,
    format_products_list,
    format_purchase_history,
    format_quotation_detail,
    format_quotations_list,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_quotation(name: str, total: float, lines_count: int = 1,
                  state: str = "draft", date_order: str | None = None,
                  order_id: int | None = None) -> dict:
    return {
        "order_id": order_id or 100,
        "name": name,
        "state": state,
        "state_label": state.title(),
        "total": total,
        "subtotal": round(total / 1.12, 2),
        "date_order": date_order or "2026-05-22 12:49:00",
        "lines_count": lines_count,
        "share_link": "https://erp.example/orders/100?token=abc",
    }


def _assert_no_markdown_tables(text: str) -> None:
    """Core contract: WhatsApp must never see these characters."""
    assert "|" not in text, f"pipe found in output: {text!r}"
    assert "---" not in text, f"horizontal rule found: {text!r}"
    assert "\t" not in text, f"tab found: {text!r}"
    # ``___`` (3+ underscores) was also reported as ugly on WA.
    assert "___" not in text, f"triple underscore: {text!r}"


# ---------------------------------------------------------------------------
# format_quotations_list
# ---------------------------------------------------------------------------

def test_quotations_list_basic_five_mixed_states():
    orders = [
        _mk_quotation("VENTA123371", 63.88, lines_count=1, state="draft"),
        _mk_quotation("VENTA123364", 155.66, lines_count=2, state="sent"),
        _mk_quotation("VENTA123359", 0.0, lines_count=0, state="draft"),
        _mk_quotation("VENTA123350", 1383.03, lines_count=5, state="sale"),
        _mk_quotation("VENTA123349", 12.50, lines_count=1, state="cancel"),
    ]
    text = format_quotations_list(orders, partner_name="Juan Pérez")
    _assert_no_markdown_tables(text)
    # All five names visible
    for name in ("VENTA123371", "VENTA123364", "VENTA123359",
                 "VENTA123350", "VENTA123349"):
        assert name in text
    # Bold marker used
    assert "*VENTA123371*" in text
    # Empty quote flagged
    assert "vacía" in text
    # Comma-separated price for the 4-digit total
    assert "USD 1,383.03" in text


def test_quotations_list_empty_emits_no_record_msg():
    text = format_quotations_list([], partner_name="Ana")
    _assert_no_markdown_tables(text)
    assert "Ana" in text
    assert "no tienes" in text.lower()


def test_quotations_list_none_orders_treated_as_empty():
    text = format_quotations_list(None)
    _assert_no_markdown_tables(text)
    assert "no tienes" in text.lower()


def test_quotations_list_truncates_above_ten():
    orders = [
        _mk_quotation(f"VENTA{300000 + i}", 10.0 * (i + 1), state="draft")
        for i in range(15)
    ]
    text = format_quotations_list(orders)
    _assert_no_markdown_tables(text)
    # First 10 visible
    assert "VENTA300000" in text
    assert "VENTA300009" in text
    # Item 11+ hidden
    assert "VENTA300010" not in text
    assert "y 5 más" in text


def test_quotations_list_uses_state_filter_label():
    orders = [_mk_quotation("VENTA1", 1.0, state="draft")]
    text = format_quotations_list(orders, state_filter="borrador")
    _assert_no_markdown_tables(text)
    assert "*borrador*" in text


def test_quotations_list_no_partner_name():
    orders = [_mk_quotation("VENTA1", 1.0)]
    text = format_quotations_list(orders)
    _assert_no_markdown_tables(text)
    assert "Tus" in text


def test_quotations_list_emoji_only_once():
    orders = [_mk_quotation("VENTA1", 1.0)]
    text = format_quotations_list(orders)
    # One opening emoji, no per-item emojis
    assert text.count("📋") == 1


def test_quotations_list_compact_date_in_subline():
    orders = [_mk_quotation("VENTA1", 1.0,
                            date_order="2026-05-22 12:49:00")]
    text = format_quotations_list(orders)
    _assert_no_markdown_tables(text)
    assert "22-may 12:49" in text
    # Year omitted when current
    assert "2026-05" not in text


def test_quotations_list_old_year_includes_year():
    orders = [_mk_quotation("VENTA1", 1.0,
                            date_order="2024-03-15 10:00:00")]
    text = format_quotations_list(orders)
    _assert_no_markdown_tables(text)
    assert "15-mar-2024" in text


def test_quotations_list_zero_total_marks_empty_only_when_no_lines():
    # Total 0 with 0 lines → "vacía". Total 0 with lines > 0 → real price.
    orders = [
        _mk_quotation("VENTA-EMPTY", 0.0, lines_count=0),
        _mk_quotation("VENTA-FREE", 0.0, lines_count=2),
    ]
    text = format_quotations_list(orders)
    _assert_no_markdown_tables(text)
    # Empty marker only attached to the no-lines order
    empty_block = [b for b in text.split("\n\n") if "VENTA-EMPTY" in b][0]
    assert "vacía" in empty_block
    free_block = [b for b in text.split("\n\n") if "VENTA-FREE" in b][0]
    assert "vacía" not in free_block
    assert "USD 0.00" in free_block


# ---------------------------------------------------------------------------
# format_products_list
# ---------------------------------------------------------------------------

def test_products_list_with_stock_variants():
    products = [
        {"code": "CPU0199", "name": "Procesador Intel Core i5",
         "price": 199.99, "qty": 5},
        {"code": "RAM0413", "name": "Memoria RAM 16GB DDR5",
         "price": 89.50, "qty": 0},
        {"code": "MOU0154", "name": "Mouse inalámbrico",
         "price": 12.99, "qty": 23},
    ]
    text = format_products_list(products, query="componentes")
    _assert_no_markdown_tables(text)
    # All products listed
    for code in ("CPU0199", "RAM0413", "MOU0154"):
        assert code in text
    # Stock info present
    assert "5 en stock" in text
    assert "agotado" in text
    assert "23 en stock" in text
    # Query echoed
    assert "componentes" in text


def test_products_list_empty():
    text = format_products_list([], query="lavadora")
    _assert_no_markdown_tables(text)
    assert "no encontré" in text.lower()
    assert "lavadora" in text


def test_products_list_prefers_line_text_when_present():
    # When line_text is already pre-rendered (e.g. from _rag_search),
    # the formatter must use it verbatim so we don't double-format the
    # row block.
    products = [{"line_text": "1️⃣  Producto X  ·  ABC123\n      💰 USD 99.00"}]
    text = format_products_list(products)
    _assert_no_markdown_tables(text)
    assert "1️⃣  Producto X" in text


# ---------------------------------------------------------------------------
# format_purchase_history
# ---------------------------------------------------------------------------

def test_purchase_history_full_envelope():
    history = {
        "success": True,
        "partner_id": 42,
        "partner_name": "Tecnosmart SA",
        "period": "2026",
        "orders_count": 7,
        "total_amount": 12500.50,
        "avg_ticket": 1785.79,
        "top_products": [],
        "recent_orders": [
            {"name": "SO001", "date": "2026-05-20", "amount": 1500.0,
             "state": "sale"},
            {"name": "SO002", "date": "2026-05-15", "amount": 850.0,
             "state": "done"},
        ],
    }
    text = format_purchase_history(history)
    _assert_no_markdown_tables(text)
    assert "Tecnosmart SA" in text
    assert "SO001" in text
    assert "SO002" in text
    assert "USD 1,500.00" in text
    # Summary line
    assert "7 órdenes" in text
    # Period
    assert "2026" in text


def test_purchase_history_empty_orders():
    history = {
        "success": True,
        "partner_name": "Empresa X",
        "period": "2026",
        "orders_count": 0,
        "total_amount": 0,
        "avg_ticket": 0,
        "recent_orders": [],
    }
    text = format_purchase_history(history)
    _assert_no_markdown_tables(text)
    assert "Empresa X" in text
    assert "no tienes" in text.lower()


def test_purchase_history_accepts_list_input():
    orders = [
        {"name": "SO001", "date": "2026-05-20", "amount": 100.0,
         "state": "sale"},
    ]
    text = format_purchase_history(orders)
    _assert_no_markdown_tables(text)
    assert "SO001" in text


# ---------------------------------------------------------------------------
# format_pending_quotations
# ---------------------------------------------------------------------------

def test_pending_quotations_with_categories():
    envelope = {
        "success": True,
        "total": 3,
        "expired": 1,
        "expiring_soon": 1,
        "active": 1,
        "quotations": [
            {"order_id": 1, "name": "Q-001", "date_sent": "2026-04-10",
             "validity_date": "2026-04-25", "days_pending": 30,
             "amount_total": 500.0, "partner": "Cliente A",
             "status": "expirada"},
            {"order_id": 2, "name": "Q-002", "date_sent": "2026-05-10",
             "validity_date": "2026-05-25", "days_pending": 10,
             "amount_total": 1200.0, "partner": "Cliente B",
             "status": "por_vencer"},
            {"order_id": 3, "name": "Q-003", "date_sent": "2026-05-15",
             "validity_date": "2026-06-15", "days_pending": 5,
             "amount_total": 800.0, "partner": "Cliente C",
             "status": "vigente"},
        ],
    }
    text = format_pending_quotations(envelope)
    _assert_no_markdown_tables(text)
    for name in ("Q-001", "Q-002", "Q-003"):
        assert name in text
    assert "Cliente A" in text
    assert "expirada" in text
    assert "por vencer" in text
    # Summary line
    assert "1 expiradas" in text
    assert "1 por vencer" in text
    assert "1 vigentes" in text


def test_pending_quotations_empty():
    text = format_pending_quotations({"quotations": []})
    _assert_no_markdown_tables(text)
    assert "no tienes" in text.lower()


def test_pending_quotations_accepts_list_input():
    items = [{"name": "Q-001", "amount_total": 100.0, "status": "vigente"}]
    text = format_pending_quotations(items)
    _assert_no_markdown_tables(text)
    assert "Q-001" in text


# ---------------------------------------------------------------------------
# format_quotation_detail
# ---------------------------------------------------------------------------

def test_quotation_detail_with_lines():
    quotation = {
        "success": True,
        "order_id": 100,
        "name": "VENTA123371",
        "state": "draft",
        "state_label": "Borrador",
        "partner": {"name": "Juan Pérez"},
        "total": 1383.03,
        "subtotal": 1234.85,
        "tax": 148.18,
        "date_order": "2026-05-22 12:49:00",
        "lines": [
            {"product": "Procesador Intel i5", "code": "CPU0199",
             "quantity": 2, "price_unit": 199.99, "subtotal": 399.98},
            {"product": "RAM 16GB", "code": "RAM0413",
             "quantity": 4, "price_unit": 89.50, "subtotal": 358.0},
        ],
    }
    text = format_quotation_detail(quotation)
    _assert_no_markdown_tables(text)
    assert "VENTA123371" in text
    assert "Juan Pérez" in text
    assert "Procesador Intel i5" in text
    assert "RAM 16GB" in text
    assert "USD 1,383.03" in text
    assert "borrador" in text


def test_quotation_detail_none_safe():
    text = format_quotation_detail(None)
    assert "no pude leer" in text.lower()


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("orders_count", [1, 5, 10, 20, 50])
def test_quotations_list_never_emits_problem_chars(orders_count: int):
    orders = [
        _mk_quotation(f"V{i}", 100.0 * (i + 1), state="draft")
        for i in range(orders_count)
    ]
    text = format_quotations_list(orders)
    _assert_no_markdown_tables(text)


def test_quotations_list_blank_line_between_items():
    orders = [
        _mk_quotation("V1", 10.0),
        _mk_quotation("V2", 20.0),
    ]
    text = format_quotations_list(orders)
    # join with \n\n means there must be at least one blank line between
    # items. Easiest assertion: each visible name preceded by a blank.
    assert "\n\n*V1*" in text or text.startswith("📋")
    # And the two items must be separated by a blank line somewhere.
    assert "*V1*" in text and "*V2*" in text
    idx1 = text.index("*V1*")
    idx2 = text.index("*V2*")
    between = text[idx1:idx2]
    assert "\n\n" in between


def test_price_in_render_uses_comma_separator():
    """Locks BPE tokenizer fix at the chat layer. See
    mcp_odoo.tools.formatters docstring for context."""
    orders = [_mk_quotation("VENTA-BIG", 1383.03)]
    text = format_quotations_list(orders)
    assert "USD 1,383.03" in text
    assert "$1383" not in text  # dollar sign is the BPE merge trigger
