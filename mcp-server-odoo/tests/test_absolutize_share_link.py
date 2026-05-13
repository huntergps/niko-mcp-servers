"""Tests for Bug M9 — absolutise share_link in sale.order responses.

Odoo's ``share_link_so`` computed field returns a path-only string when
``ir.config_parameter.web.base.url`` is not set on the production
database. The customer should always see a clickable https URL, so the
MCP layer absolutises the link before handing it back to the LLM.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


def test_empty_returns_empty_string() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    assert _absolutize_share_link("", "https://erp.example.com") == ""
    assert _absolutize_share_link(None, "https://erp.example.com") == ""


def test_already_absolute_https_pass_through() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    link = "https://erp.example.com/files/quotations/VENTA1.pdf"
    assert _absolutize_share_link(link, "https://erp.example.com") == link


def test_already_absolute_http_pass_through() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    link = "http://10.0.0.1:8069/share/x"
    assert _absolutize_share_link(link, "https://erp.example.com") == link


def test_protocol_relative_gets_https_prefix() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    out = _absolutize_share_link(
        "//cdn.example.com/files/x.pdf",
        "https://erp.example.com",
    )
    assert out == "https://cdn.example.com/files/x.pdf"


def test_relative_path_prefixed_with_base_url() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    out = _absolutize_share_link(
        "/share/sale/123",
        "https://erp.tecnosmart.com.ec/",
    )
    assert out == "https://erp.tecnosmart.com.ec/share/sale/123"


def test_relative_path_without_slash() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    out = _absolutize_share_link(
        "share/sale/123",
        "https://erp.example.com",
    )
    assert out == "https://erp.example.com/share/sale/123"


def test_no_base_url_returns_link_as_is() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    assert _absolutize_share_link("/share/x", None) == "/share/x"
    assert _absolutize_share_link("/share/x", "") == "/share/x"


def test_trailing_slash_in_base_handled() -> None:
    from mcp_odoo.tools.sales import _absolutize_share_link

    out = _absolutize_share_link("/x/y", "https://e.com///")
    assert out == "https://e.com/x/y"
