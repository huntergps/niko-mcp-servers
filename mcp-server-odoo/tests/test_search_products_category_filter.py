"""Tests for Bug M4 — category_path filter on search_products.

The MCP ``search_products`` tool accepts ``category_path``: a partial
Odoo ``categ_id.complete_name`` filter. When the customer searches for
"laptops" we want to suppress monitor screens / repair parts that the
text-similarity pass surfaces.

These tests exercise ``_format_ranked_page`` directly with synthetic
ranked rows so we don't need a live Odoo connection.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


def _row(name: str, code: str, price: float, qty: int, category: str) -> dict:
    return {
        "odoo_id": hash(code) & 0xffff,
        "_live": {
            "name": name,
            "code": code,
            "price": price,
            "qty": qty,
            "category": category,
        },
    }


def _parse(envelope: str) -> dict:
    return json.loads(envelope)


def test_category_filter_drops_other_categories() -> None:
    """Asking for 'Laptops' must drop monitor rows even when they
    pgvector-rank close to laptop-related queries."""
    from mcp_odoo.transports.mcp_transport import _format_ranked_page

    ranked = [
        _row("Dell XPS 15", "LAP0100", 1500.0, 3, "Computadoras / Laptops"),
        _row(
            "Pantalla suelta MacBook 13''",
            "REP0001",
            150.0,
            2,
            "Repuestos / Pantallas",
        ),
        _row("HP Pavilion 14", "LAP0200", 800.0, 1, "Computadoras / Laptops"),
    ]
    out = _format_ranked_page(
        ranked, top_k=10, offset=0, category_path="Laptops",
    )
    payload = _parse(out)
    codes = [r["code"] for r in payload["rows"]]
    assert "LAP0100" in codes
    assert "LAP0200" in codes
    assert "REP0001" not in codes


def test_category_filter_case_insensitive() -> None:
    from mcp_odoo.transports.mcp_transport import _format_ranked_page

    ranked = [
        _row("Monitor LG", "MON0001", 200.0, 1, "Computadoras / Monitores"),
    ]
    out = _format_ranked_page(
        ranked, top_k=10, offset=0, category_path="monitores",
    )
    payload = _parse(out)
    assert any(r["code"] == "MON0001" for r in payload["rows"])


def test_no_category_filter_keeps_all() -> None:
    """category_path=None preserves the legacy behaviour (no filter)."""
    from mcp_odoo.transports.mcp_transport import _format_ranked_page

    ranked = [
        _row("Mon A", "M1", 100, 1, "Cat A"),
        _row("Mon B", "M2", 200, 1, "Cat B"),
    ]
    out = _format_ranked_page(ranked, top_k=10, offset=0)
    payload = _parse(out)
    codes = {r["code"] for r in payload["rows"]}
    assert codes == {"M1", "M2"}


def test_category_filter_combined_with_price_max() -> None:
    from mcp_odoo.transports.mcp_transport import _format_ranked_page

    ranked = [
        _row("Laptop cara", "LAP1", 2000, 1, "Computadoras / Laptops"),
        _row("Laptop barata", "LAP2", 500, 1, "Computadoras / Laptops"),
        _row("Pantalla", "REP1", 150, 1, "Repuestos / Pantallas"),
    ]
    out = _format_ranked_page(
        ranked, top_k=10, offset=0,
        price_max=1000, category_path="Laptops",
    )
    payload = _parse(out)
    codes = {r["code"] for r in payload["rows"]}
    assert codes == {"LAP2"}
