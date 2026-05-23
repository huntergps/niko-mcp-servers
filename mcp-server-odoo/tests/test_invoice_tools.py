"""Tests for ZETA iter 80 invoice / payment / statement tools.

Coverage matrix:
  * helper functions in ``mcp_odoo.tools.invoices`` (mocked odoo_search /
    odoo_read so no live Odoo required)
  * formatters in ``mcp_odoo.formatters.whatsapp_invoices`` (pure dict →
    string contract)
  * MCP transport registration (the 4 tools must appear in tools/list)
  * Handler dispatch with mock OTP session + mock helper output

The financial-data OTP gate is the most important contract: every one
of the 4 tools MUST refuse to return data without a verified session.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient


JWT_SECRET = "test-jwt-secret-for-testing-only-32bytes!"

MOCK_TENANT_CONFIG = {
    "tenant_id": "tenant-zeta-test",
    "url": "http://fake-odoo:8069",
    "db": "testdb",
    "user": "admin",
    "password": "admin",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


@pytest.fixture
def client():
    from importlib import reload
    from mcp_odoo import config as _cfg
    reload(_cfg)
    from mcp_odoo.server import app

    async def _stub_get_tenant_config(_request):
        return MOCK_TENANT_CONFIG

    with patch(
        "mcp_odoo.transports.mcp_transport._get_tenant_config",
        new=_stub_get_tenant_config,
    ):
        yield TestClient(app)


def _rpc(client, method: str, params: dict | None = None,
         headers: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post("/mcp", json=payload, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def _otp_session_ok():
    async def _async(*args, **kwargs):
        return True
    return _async


def _otp_session_denied():
    async def _async(*args, **kwargs):
        return False
    return _async


# ---------------------------------------------------------------------------
# tools/list registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_four_tools_listed(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]
        for expected in (
            "get_customer_invoices",
            "get_invoice_detail",
            "get_customer_payments",
            "get_customer_statement",
        ):
            assert expected in names, f"{expected} missing from MCP_TOOLS"

    def test_input_schema_valid(self, client):
        body = _rpc(client, "tools/list")
        by_name = {t["name"]: t for t in body["result"]["tools"]}
        for name in ("get_customer_invoices", "get_customer_payments",
                     "get_customer_statement"):
            schema = by_name[name]["inputSchema"]
            assert "partner_id" in schema["properties"]
            assert "partner_id" in schema["required"]

        # get_invoice_detail expects invoice_id only.
        d = by_name["get_invoice_detail"]["inputSchema"]
        assert "invoice_id" in d["properties"]
        assert "invoice_id" in d["required"]

    def test_state_filter_enum(self, client):
        body = _rpc(client, "tools/list")
        by_name = {t["name"]: t for t in body["result"]["tools"]}
        enum = by_name["get_customer_invoices"]["inputSchema"]["properties"]["state"]["enum"]
        assert set(enum) == {"all", "paid", "not_paid", "overdue"}


# ---------------------------------------------------------------------------
# OTP gate
# ---------------------------------------------------------------------------

class TestOTPGate:
    @pytest.mark.parametrize("tool_name,args", [
        ("get_customer_invoices", {"partner_id": 62}),
        ("get_customer_payments", {"partner_id": 62}),
        ("get_customer_statement", {"partner_id": 62}),
    ])
    def test_tool_refuses_without_session(self, client, tool_name, args):
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_denied(),
        ):
            body = _rpc(client, "tools/call", {
                "name": tool_name,
                "arguments": args,
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        assert "VERIFICACION REQUERIDA" in text

    def test_invoice_detail_resolves_partner_then_gates(self, client):
        async def _fake_read(*a, **k):
            # _odoo_read called with model='account.move', ids=[404936]
            return [{"id": 404936, "partner_id": [62, "Customer X"]}]
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_denied(),
        ), patch(
            "mcp_odoo.tools.generic.odoo_read", side_effect=lambda *a, **k: _fake_read_sync(*a, **k),
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_invoice_detail",
                "arguments": {"invoice_id": 404936},
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        assert "VERIFICACION REQUERIDA" in text


def _fake_read_sync(*args, **kwargs):
    """Helper for partner lookup in get_invoice_detail OTP gate test."""
    model = args[5] if len(args) > 5 else kwargs.get("model")
    if model == "account.move":
        return [{"id": 404936, "partner_id": [62, "Customer X"]}]
    return []


# ---------------------------------------------------------------------------
# Helper functions (mocked odoo_search / odoo_read)
# ---------------------------------------------------------------------------

class TestGetCustomerInvoicesHelper:
    def test_basic_call(self):
        from mcp_odoo.tools import invoices as inv

        def _fake_search(*a, **k):
            return [
                {
                    "id": 404936,
                    "name": "FACV/2025/4897",
                    "number": "001-001-000123456",
                    "type": "out_invoice",
                    "state": "posted",
                    "partner_id": [62, "Customer X"],
                    "invoice_date": "2025-08-19",
                    "invoice_date_due": "2025-08-20",
                    "amount_total": 40.25,
                    "amount_residual": 0.0,
                    "amount_untaxed": 35.94,
                    "amount_tax": 4.31,
                    "invoice_payment_state": "paid",
                    "ref": "VENTA109335",
                    "access_token": "tok_abc123",
                    "access_url": "/my/invoices/404936",
                    "currency_id": [1, "USD"],
                },
            ]
        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search):
            r = inv.odoo_get_customer_invoices(
                "tenant-1", "http://erp.tecnosmart.com.ec", "db", "u", "p",
                partner_id=62,
            )
        assert r["success"] is True
        assert r["partner_id"] == 62
        assert r["count"] == 1
        assert r["invoices"][0]["name"] == "FACV/2025/4897"
        assert r["invoices"][0]["payment_state_label"] == "pagada"
        assert r["invoices"][0]["type_label"] == "factura"
        assert "access_token=tok_abc123" in r["invoices"][0]["portal_url"]
        assert r["invoices"][0]["ref"] == "VENTA109335"

    def test_overdue_filter_builds_domain(self):
        from mcp_odoo.tools import invoices as inv
        captured = {}

        def _fake_search(*a, **k):
            # positionals: tenant, url, db, user, password, model, domain
            captured["domain"] = a[6]
            return []
        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search):
            inv.odoo_get_customer_invoices(
                "t", "url", "db", "u", "p", partner_id=62, state="overdue",
            )
        domain = captured["domain"]
        assert ["invoice_date_due", "<", date.today().isoformat()] in domain
        assert ["invoice_payment_state", "in", ["not_paid", "partial"]] in domain

    def test_invalid_state_returns_error(self):
        from mcp_odoo.tools import invoices as inv
        r = inv.odoo_get_customer_invoices(
            "t", "url", "db", "u", "p", partner_id=62, state="bogus",
        )
        assert r["success"] is False
        assert r["error_code"] == "invalid_state"

    def test_invalid_partner_id(self):
        from mcp_odoo.tools import invoices as inv
        assert inv.odoo_get_customer_invoices(
            "t", "url", "db", "u", "p", partner_id=-1,
        )["error_code"] == "invalid_partner_id"

    def test_limit_capped_at_50(self):
        from mcp_odoo.tools import invoices as inv
        captured = {}

        def _fake_search(*a, **k):
            captured["limit"] = k.get("limit")
            return []
        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search):
            inv.odoo_get_customer_invoices(
                "t", "url", "db", "u", "p", partner_id=62, limit=999,
            )
        assert captured["limit"] == 50

    def test_days_overdue_positive_when_due_passed(self):
        from mcp_odoo.tools import invoices as inv
        past = (date.today() - timedelta(days=10)).isoformat()

        def _fake_search(*a, **k):
            return [{
                "id": 1, "name": "F1", "type": "out_invoice", "state": "posted",
                "partner_id": [62, "X"],
                "invoice_date": past, "invoice_date_due": past,
                "amount_total": 100, "amount_residual": 100,
                "amount_untaxed": 89.29, "amount_tax": 10.71,
                "invoice_payment_state": "not_paid",
                "ref": "VENTA1", "access_token": "x", "access_url": "/my/invoices/1",
                "currency_id": [1, "USD"],
            }]
        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search):
            r = inv.odoo_get_customer_invoices(
                "t", "url", "db", "u", "p", partner_id=62,
            )
        assert r["invoices"][0]["days_overdue"] == 10


class TestGetInvoiceDetailHelper:
    def test_basic_call_with_lines_and_taxes(self):
        from mcp_odoo.tools import invoices as inv

        def _fake_read(*a, **k):
            model = a[5]
            ids = a[6]
            if model == "account.move":
                return [{
                    "id": 404936,
                    "name": "FACV/2025/4897",
                    "number": "001-001-...",
                    "type": "out_invoice",
                    "state": "posted",
                    "partner_id": [62, "Customer X"],
                    "invoice_date": "2025-08-19",
                    "invoice_date_due": "2025-08-20",
                    "amount_total": 40.25,
                    "amount_residual": 0.0,
                    "amount_untaxed": 35.94,
                    "amount_tax": 4.31,
                    "invoice_payment_state": "paid",
                    "ref": "VENTA109335",
                    "access_token": "tok",
                    "access_url": "/my/invoices/404936",
                    "currency_id": [1, "USD"],
                    "invoice_line_ids": [501, 502],
                }]
            if model == "account.move.line":
                return [
                    {"id": 501, "product_id": [99, "TECLADO"], "name": "TECLADO USB",
                     "quantity": 1, "price_unit": 25.0, "discount": 0.0,
                     "price_subtotal": 25.0, "price_total": 28.0, "tax_ids": [12]},
                    {"id": 502, "product_id": [100, "MOUSE"], "name": "MOUSE OPTICO",
                     "quantity": 1, "price_unit": 10.94, "discount": 0.0,
                     "price_subtotal": 10.94, "price_total": 12.25, "tax_ids": [12]},
                ]
            if model == "account.tax":
                return [{"id": 12, "name": "IVA 12%", "amount": 12.0}]
            return []
        with patch("mcp_odoo.tools.invoices.odoo_read", side_effect=_fake_read):
            r = inv.odoo_get_invoice_detail(
                "t", "url", "db", "u", "p", invoice_id=404936,
            )
        assert r["success"] is True
        assert r["invoice"]["name"] == "FACV/2025/4897"
        assert len(r["lines"]) == 2
        assert r["lines"][0]["taxes"][0]["name"] == "IVA 12%"
        assert any(t["name"] == "IVA 12%" for t in r["taxes"])

    def test_not_found(self):
        from mcp_odoo.tools import invoices as inv
        with patch("mcp_odoo.tools.invoices.odoo_read", side_effect=lambda *a, **k: []):
            r = inv.odoo_get_invoice_detail(
                "t", "url", "db", "u", "p", invoice_id=999999,
            )
        assert r["success"] is False
        assert r["error_code"] == "invoice_not_found"

    def test_invalid_invoice_id(self):
        from mcp_odoo.tools import invoices as inv
        assert inv.odoo_get_invoice_detail(
            "t", "url", "db", "u", "p", invoice_id="not-int",
        )["error_code"] == "invalid_invoice_id"


class TestGetCustomerPaymentsHelper:
    def test_basic_call(self):
        from mcp_odoo.tools import invoices as inv

        def _fake_search(*a, **k):
            model = a[5]
            if model == "account.payment":
                return [{
                    "id": 5001,
                    "name": "PAY/2025/123",
                    "payment_date": "2025-09-15",
                    "amount": 500.0,
                    "journal_id": [3, "Banco Pichincha"],
                    "partner_id": [62, "Customer X"],
                    "payment_type": "inbound",
                    "state": "posted",
                    "communication": "Pago FACV/2025/4897",
                    "reconciled_invoice_ids": [404936],
                }]
            return []

        def _fake_read(*a, **k):
            model = a[5]
            if model == "account.move":
                return [{"id": 404936, "name": "FACV/2025/4897"}]
            return []

        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search), \
             patch("mcp_odoo.tools.invoices.odoo_read", side_effect=_fake_read):
            r = inv.odoo_get_customer_payments(
                "t", "url", "db", "u", "p", partner_id=62,
            )
        assert r["success"] is True
        assert r["count"] == 1
        assert r["payments"][0]["journal"] == "Banco Pichincha"
        assert r["payments"][0]["applied_to"][0]["name"] == "FACV/2025/4897"
        assert r["total_amount"] == 500.0

    def test_year_filter(self):
        from mcp_odoo.tools import invoices as inv
        captured = {}

        def _fake_search(*a, **k):
            # positionals: tenant, url, db, user, password, model, domain
            captured["domain"] = a[6]
            return []
        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search):
            inv.odoo_get_customer_payments(
                "t", "url", "db", "u", "p", partner_id=62, year=2025,
            )
        assert ["payment_date", ">=", "2025-01-01"] in captured["domain"]
        assert ["payment_date", "<=", "2025-12-31"] in captured["domain"]


class TestGetCustomerStatementHelper:
    def test_period_aggregates(self):
        from mcp_odoo.tools import invoices as inv

        today = date.today()
        recent = (today - timedelta(days=10)).isoformat()
        older = (today - timedelta(days=60)).isoformat()

        def _fake_search(*a, **k):
            model = a[5]
            if model == "account.move":
                return [
                    {"id": 1, "name": "F1", "invoice_date": recent,
                     "invoice_date_due": recent, "amount_total": 100.0,
                     "amount_residual": 0.0, "invoice_payment_state": "paid",
                     "type": "out_invoice"},
                    {"id": 2, "name": "F2", "invoice_date": older,
                     "invoice_date_due": older, "amount_total": 200.0,
                     "amount_residual": 200.0, "invoice_payment_state": "not_paid",
                     "type": "out_invoice"},
                ]
            if model == "account.payment":
                return [
                    {"id": 100, "name": "P1", "payment_date": recent,
                     "amount": 100.0, "reconciled_invoice_ids": [1]},
                ]
            if model == "account.move.line":
                return [
                    {"move_id": [2, "F2"], "date_maturity": older,
                     "amount_residual": 200.0},
                ]
            return []

        def _fake_read(*a, **k):
            if a[5] == "account.move":
                return [{"id": 1, "name": "F1"}]
            return []

        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search), \
             patch("mcp_odoo.tools.invoices.odoo_read", side_effect=_fake_read):
            r = inv.odoo_get_customer_statement(
                "t", "url", "db", "u", "p", partner_id=62, days_back=90,
            )
        assert r["success"] is True
        s = r["summary"]
        assert s["total_billed"] == 300.0
        assert s["total_paid"] == 100.0
        assert s["total_due_now"] == 200.0
        assert s["total_overdue_now"] == 200.0
        assert s["invoices_in_period"] == 2
        assert s["invoices_overdue"] == 1
        # avg_payment_days computed from paid F1 (invoice_date=recent,
        # payment_date=recent) → diff 0 days.
        assert s["avg_payment_days"] == 0.0
        assert len(r["recent_movements"]) == 3

    def test_days_back_clamped(self):
        from mcp_odoo.tools import invoices as inv
        captured = {}

        def _fake_search(*a, **k):
            model = a[5]
            if model == "account.move" and "invoice_domain" not in captured:
                captured["invoice_domain"] = a[6]
            return []
        with patch("mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search):
            inv.odoo_get_customer_statement(
                "t", "url", "db", "u", "p", partner_id=62, days_back=999_999,
            )
        # Clamped to 730 days back.
        d = captured["invoice_domain"]
        from_clause = [c for c in d if c[0] == "invoice_date" and c[1] == ">="][0]
        from_date = datetime.strptime(from_clause[2], "%Y-%m-%d").date()
        assert (date.today() - from_date).days == 730


# ---------------------------------------------------------------------------
# Portal URL construction (NEVER strip access_token)
# ---------------------------------------------------------------------------

class TestPortalURL:
    def test_token_preserved_relative_url(self):
        from mcp_odoo.tools.invoices import _build_portal_url
        u = _build_portal_url(
            "https://erp.tecnosmart.com.ec", 404936,
            "/my/invoices/404936", "tok_abc",
        )
        assert "access_token=tok_abc" in u
        assert u.startswith("https://erp.tecnosmart.com.ec")

    def test_token_preserved_absolute_url(self):
        from mcp_odoo.tools.invoices import _build_portal_url
        u = _build_portal_url(
            "https://erp.tecnosmart.com.ec", 404936,
            "https://erp.tecnosmart.com.ec/my/invoices/404936",
            "tok_xyz",
        )
        assert "access_token=tok_xyz" in u

    def test_fallback_when_no_access_url(self):
        from mcp_odoo.tools.invoices import _build_portal_url
        u = _build_portal_url(
            "https://erp.tecnosmart.com.ec", 404936, "", "tok",
        )
        assert u == "https://erp.tecnosmart.com.ec/my/invoices/404936?access_token=tok"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _assert_no_markdown_tables(text: str) -> None:
    assert "|" not in text, f"pipe: {text!r}"
    assert "---" not in text, f"hr: {text!r}"
    assert "\t" not in text, f"tab: {text!r}"


class TestFormatInvoicesList:
    def test_overdue_list(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_invoices_list
        invoices = [
            {"name": "FACV/2024/7293", "amount_total": 87.40,
             "amount_residual": 79.38, "payment_state_label": "pendiente",
             "type_label": "factura", "invoice_date": "2024-11-09",
             "invoice_date_due": "2024-11-10", "days_overdue": 145,
             "ref": "VENTA97316"},
        ]
        text = format_invoices_list(
            invoices, total_amount=87.40, total_residual=79.38,
            state_filter="overdue",
        )
        _assert_no_markdown_tables(text)
        assert "FACV/2024/7293" in text
        assert "vencida hace 145" in text
        assert "VENTA97316" in text
        assert "pendiente" in text
        assert "factura" in text  # plural header "facturas vencidas"

    def test_paid_invoice_hides_overdue_tag(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_invoices_list
        invoices = [
            {"name": "FACV/2025/4897", "amount_total": 40.25,
             "amount_residual": 0.0, "payment_state_label": "pagada",
             "type_label": "factura", "invoice_date": "2025-08-19",
             "invoice_date_due": "2025-08-20", "days_overdue": 145,
             "ref": "VENTA109335"},
        ]
        text = format_invoices_list(invoices, total_amount=40.25, total_residual=0.0)
        _assert_no_markdown_tables(text)
        assert "FACV/2025/4897" in text
        assert "pagada" in text
        # Residual is 0, so the overdue tag must NOT appear.
        assert "vencida hace" not in text

    def test_empty_list(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_invoices_list
        text = format_invoices_list([], state_filter="overdue")
        assert "No tienes facturas vencidas" in text
        _assert_no_markdown_tables(text)


class TestFormatInvoiceDetail:
    def test_with_lines_and_taxes(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_invoice_detail
        invoice = {
            "name": "FACV/2025/4897", "amount_total": 40.25,
            "amount_residual": 0.0, "amount_untaxed": 35.94,
            "amount_tax": 4.31, "payment_state_label": "pagada",
            "type_label": "factura", "invoice_date": "2025-08-19",
            "invoice_date_due": "2025-08-20", "days_overdue": 145,
            "ref": "VENTA109335",
            "portal_url": "https://erp.example/my/invoices/404936?access_token=tok",
        }
        lines = [
            {"product": {"id": 99, "name": "TECLADO USB"}, "quantity": 1,
             "price_unit": 25.0, "price_subtotal": 25.0, "discount": 0.0},
        ]
        taxes = [{"name": "IVA 12%", "tax_amount": 4.31}]
        text = format_invoice_detail(invoice, lines=lines, taxes=taxes)
        _assert_no_markdown_tables(text)
        assert "FACV/2025/4897" in text
        assert "TECLADO USB" in text
        assert "IVA 12%" in text
        assert "access_token=tok" in text  # never stripped


class TestFormatPaymentsList:
    def test_basic(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_payments_list
        env = {
            "count": 1,
            "total_amount": 500.0,
            "payments": [{
                "name": "PAY/2025/123", "amount": 500.0,
                "journal": "Banco Pichincha", "payment_date": "2025-09-15",
                "applied_to": [{"invoice_id": 404936, "name": "FACV/2025/4897"}],
            }],
        }
        text = format_payments_list(env)
        _assert_no_markdown_tables(text)
        assert "PAY/2025/123" in text
        assert "Banco Pichincha" in text
        assert "FACV/2025/4897" in text

    def test_empty(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_payments_list
        assert "No tienes pagos" in format_payments_list({"payments": []})


class TestFormatStatementSummary:
    def test_full(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_statement_summary
        summary = {
            "total_billed": 5000.0,
            "total_paid": 3200.0,
            "total_due_now": 4898.35,
            "total_overdue_now": 1797.07,
            "invoices_in_period": 12,
            "invoices_overdue": 51,
            "avg_payment_days": 18.3,
        }
        movements = [
            {"date": "2026-05-15", "type": "invoice", "name": "FACV/2026/100",
             "amount": 250.0, "residual": 250.0, "applied_to": None},
            {"date": "2026-05-10", "type": "payment", "name": "PAY/2026/55",
             "amount": -500.0, "applied_to": ["FACV/2026/123"]},
        ]
        text = format_statement_summary(
            summary, recent_movements=movements,
            period={"from": "2026-02-22", "to": "2026-05-23"},
        )
        _assert_no_markdown_tables(text)
        assert "Estado de cuenta" in text
        assert "vencido" in text
        assert "FACV/2026/100" in text
        assert "PAY/2026/55" in text

    def test_no_movements(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_statement_summary
        text = format_statement_summary(
            {"total_billed": 0, "total_paid": 0, "total_due_now": 0,
             "total_overdue_now": 0, "invoices_in_period": 0,
             "invoices_overdue": 0, "avg_payment_days": None},
            recent_movements=[], period={"from": "2026-01-01", "to": "2026-05-23"},
        )
        assert "Estado de cuenta" in text


# ---------------------------------------------------------------------------
# End-to-end handler dispatch (OTP allowed)
# ---------------------------------------------------------------------------

class TestDispatchWithOTP:
    def test_get_customer_invoices_full_flow(self, client):
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ), patch(
            "mcp_odoo.tools.invoices.odoo_search",
            side_effect=lambda *a, **k: [
                {
                    "id": 404936, "name": "FACV/2025/4897",
                    "number": "001-001-...", "type": "out_invoice",
                    "state": "posted", "partner_id": [62, "X"],
                    "invoice_date": "2025-08-19",
                    "invoice_date_due": "2025-08-20",
                    "amount_total": 40.25, "amount_residual": 0.0,
                    "amount_untaxed": 35.94, "amount_tax": 4.31,
                    "invoice_payment_state": "paid", "ref": "VENTA109335",
                    "access_token": "tok", "access_url": "/my/invoices/404936",
                    "currency_id": [1, "USD"],
                },
            ],
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_customer_invoices",
                "arguments": {"partner_id": 62, "state": "all"},
            }, headers={"X-Channel": "whatsapp"})

        text = body["result"]["content"][0]["text"]
        data = json.loads(text)
        assert data["success"] is True
        assert data["count"] == 1
        # display_text injected because channel=whatsapp
        assert "display_text" in data
        assert "FACV/2025/4897" in data["display_text"]

    def test_get_customer_statement_full_flow(self, client):
        today = date.today()
        recent = (today - timedelta(days=5)).isoformat()

        def _fake_search(*a, **k):
            model = a[5]
            if model == "account.move":
                return [{"id": 1, "name": "F1", "invoice_date": recent,
                         "invoice_date_due": recent, "amount_total": 50.0,
                         "amount_residual": 50.0, "type": "out_invoice",
                         "invoice_payment_state": "not_paid"}]
            if model == "account.payment":
                return []
            if model == "account.move.line":
                return [{"move_id": [1, "F1"], "date_maturity": recent,
                         "amount_residual": 50.0}]
            return []

        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ), patch(
            "mcp_odoo.tools.invoices.odoo_search", side_effect=_fake_search,
        ), patch(
            "mcp_odoo.tools.invoices.odoo_read", side_effect=lambda *a, **k: [],
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_customer_statement",
                "arguments": {"partner_id": 62, "days_back": 90},
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        data = json.loads(text)
        assert data["success"] is True
        assert data["summary"]["total_billed"] == 50.0
        assert "display_text" in data
        assert "Estado de cuenta" in data["display_text"]


# ---------------------------------------------------------------------------
# Cross-partner enforcement (defence in depth)
# ---------------------------------------------------------------------------

class TestCrossPartnerEnforcement:
    def test_rejects_other_partner(self, client):
        # OTP session is fine, but X-Expected-Partner-Id=99 doesn't match
        # partner_id=62 in the args → reject before reading data.
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_customer_invoices",
                "arguments": {"partner_id": 62},
            }, headers={
                "X-Channel": "whatsapp",
                "X-Expected-Partner-Id": "99",
            })
        text = body["result"]["content"][0]["text"]
        data = json.loads(text)
        assert data["success"] is False
        assert data["error_code"] == "cross_partner_invoice"
