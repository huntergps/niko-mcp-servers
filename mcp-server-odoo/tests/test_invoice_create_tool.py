"""Tests for the salon billing tool ``create_invoice`` (Odoo 19
``account.move`` out_invoice in DRAFT, l10n_ec_edi).

Coverage matrix:
  * helper ``create_invoice`` in ``mcp_odoo.tools.billing`` (mocked
    odoo_search / odoo_read / odoo_create / odoo_call_method — no live
    Odoo required)
  * formatter ``format_invoice_created`` (pure dict → string contract)
  * MCP transport registration (create_invoice must appear in tools/list)

Key contracts asserted:
  * lines resolve by product name (fuzzy ilike)
  * a DRAFT out_invoice is created (move_type='out_invoice', NO
    action_post is ever called)
  * an item not in the catalogue -> error_code 'item_not_found'
  * an invalid partner_id -> error_code 'invalid_partner_id'
  * the staff message_post is fired best-effort and never aborts the call
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


JWT_SECRET = "test-jwt-secret-for-testing-only-32bytes!"

MOCK_TENANT_CONFIG = {
    "tenant_id": "tenant-afrodita-test",
    "url": "http://fake-odoo:8069",
    "db": "afrodita",
    "user": "worker_api",
    "password": "secret",
}

CREDS = ("t", "http://odoo", "db", "u", "p")

# A product.product row as read by _resolve_product.
PROD_MANICURA = {"id": 23, "name": "Manicura semipermanente", "list_price": 20.0}
# A sales journal row.
JOURNAL_SALE = {"id": 1, "name": "001-001 Facturas de cliente", "code": "INV"}
# The res.partner row.
PARTNER_45 = {"id": 45, "name": "Cliente de Prueba"}
# The account.move read-back.
MOVE_READBACK = {
    "id": 100,
    "name": "Borrador",
    "state": "draft",
    "partner_id": [45, "Cliente de Prueba"],
    "amount_total": 22.4,
    "amount_untaxed": 20.0,
    "amount_tax": 2.4,
    "currency_id": [1, "USD"],
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


# ---------------------------------------------------------------------------
# tools/list registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_create_invoice_listed(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]
        assert "create_invoice" in names, "create_invoice missing from MCP_TOOLS"

    def test_input_schema_required_fields(self, client):
        body = _rpc(client, "tools/list")
        by_name = {t["name"]: t for t in body["result"]["tools"]}
        req = set(by_name["create_invoice"]["inputSchema"]["required"])
        assert req == {"partner_id", "lines"}


# ---------------------------------------------------------------------------
# create_invoice helper
# ---------------------------------------------------------------------------

def _make_search(*, products: dict | None = None,
                 journals=JOURNAL_SALE):
    """Build a fake odoo_search that routes by model.

    ``products`` maps the resolved product name lookup: returns the value
    keyed by the model 'product.product'. Pass an empty list to simulate a
    missing product.
    """
    products = PROD_MANICURA if products is None else products

    def _search(*a, **k):
        model = a[5]
        if model == "account.journal":
            return [journals] if journals else []
        if model == "product.product":
            if isinstance(products, list):
                return products
            return [products]
        return []
    return _search


class TestCreateInvoice:
    def test_creates_draft_out_invoice(self):
        from mcp_odoo.tools import billing

        created = {}

        def _fake_read(*a, **k):
            model = a[5]
            if model == "res.partner":
                return [PARTNER_45]
            if model == "account.move":
                return [MOVE_READBACK]
            return []

        def _fake_create(*a, **k):
            assert a[5] == "account.move"
            created["values"] = a[6]
            return 100

        def _fake_call(*a, **k):
            # message_post for the staff notice.
            created["method"] = a[6]
            return True

        with patch.object(billing, "odoo_search", side_effect=_make_search()), \
             patch.object(billing, "odoo_read", side_effect=_fake_read), \
             patch.object(billing, "odoo_create", side_effect=_fake_create), \
             patch.object(billing, "odoo_call_method", side_effect=_fake_call):
            r = billing.create_invoice(
                *CREDS,
                partner_id=45,
                lines=[{"item": "Manicura semipermanente", "quantity": 1}],
            )

        assert r["success"] is True
        assert r["invoice_id"] == 100
        assert r["state"] == "draft"
        assert r["currency"] == "USD"
        assert r["amount_total"] == 22.4
        assert r["partner_name"] == "Cliente de Prueba"
        assert r["staff_notice"] is True
        # Created as an out_invoice draft (NO posting).
        vals = created["values"]
        assert vals["move_type"] == "out_invoice"
        assert vals["partner_id"] == 45
        assert vals["journal_id"] == 1
        assert "invoice_date" in vals
        # Line resolved by name; price_unit NOT forced (let Odoo compute).
        cmd = vals["invoice_line_ids"][0]
        assert cmd[0] == 0 and cmd[1] == 0
        assert cmd[2]["product_id"] == 23
        assert cmd[2]["quantity"] == 1.0
        assert "price_unit" not in cmd[2]
        # Staff notice posted.
        assert created["method"] == "message_post"

    def test_never_posts_invoice(self):
        """create_invoice must NEVER call action_post (stays in draft)."""
        from mcp_odoo.tools import billing

        def _fake_read(*a, **k):
            model = a[5]
            if model == "res.partner":
                return [PARTNER_45]
            if model == "account.move":
                return [MOVE_READBACK]
            return []

        called_methods = []

        def _fake_call(*a, **k):
            called_methods.append(a[6])
            return True

        with patch.object(billing, "odoo_search", side_effect=_make_search()), \
             patch.object(billing, "odoo_read", side_effect=_fake_read), \
             patch.object(billing, "odoo_create", side_effect=lambda *a, **k: 100), \
             patch.object(billing, "odoo_call_method", side_effect=_fake_call):
            r = billing.create_invoice(
                *CREDS, partner_id=45,
                lines=[{"item": "Manicura semipermanente"}],
            )

        assert r["success"] is True
        assert "action_post" not in called_methods
        assert called_methods == ["message_post"]

    def test_forces_price_unit_when_caller_passes_it(self):
        from mcp_odoo.tools import billing

        created = {}

        def _fake_read(*a, **k):
            model = a[5]
            if model == "res.partner":
                return [PARTNER_45]
            if model == "account.move":
                return [MOVE_READBACK]
            return []

        with patch.object(billing, "odoo_search", side_effect=_make_search()), \
             patch.object(billing, "odoo_read", side_effect=_fake_read), \
             patch.object(billing, "odoo_create",
                          side_effect=lambda *a, **k: created.update(values=a[6]) or 100), \
             patch.object(billing, "odoo_call_method", side_effect=lambda *a, **k: True):
            billing.create_invoice(
                *CREDS, partner_id=45,
                lines=[{"item": "Manicura semipermanente", "price_unit": 15.5}],
            )

        cmd = created["values"]["invoice_line_ids"][0]
        assert cmd[2]["price_unit"] == 15.5

    def test_item_not_found(self):
        from mcp_odoo.tools import billing

        def _fake_read(*a, **k):
            return [PARTNER_45] if a[5] == "res.partner" else []

        # product.product search returns nothing -> item not found.
        with patch.object(billing, "odoo_search",
                          side_effect=_make_search(products=[])), \
             patch.object(billing, "odoo_read", side_effect=_fake_read), \
             patch.object(billing, "odoo_create",
                          side_effect=lambda *a, **k: pytest.fail("must not create")):
            r = billing.create_invoice(
                *CREDS, partner_id=45,
                lines=[{"item": "Servicio inexistente"}],
            )

        assert r["success"] is False
        assert r["error_code"] == "item_not_found"
        assert "Servicio inexistente" in r["error_detail"]

    def test_invalid_partner(self):
        from mcp_odoo.tools import billing
        r = billing.create_invoice(
            *CREDS, partner_id=0,
            lines=[{"item": "Manicura semipermanente"}],
        )
        assert r["success"] is False
        assert r["error_code"] == "invalid_partner_id"

    def test_partner_not_found(self):
        from mcp_odoo.tools import billing
        with patch.object(billing, "odoo_search", side_effect=_make_search()), \
             patch.object(billing, "odoo_read", side_effect=lambda *a, **k: []):
            r = billing.create_invoice(
                *CREDS, partner_id=999,
                lines=[{"item": "Manicura semipermanente"}],
            )
        assert r["success"] is False
        assert r["error_code"] == "partner_not_found"

    def test_no_sale_journal(self):
        from mcp_odoo.tools import billing

        def _fake_read(*a, **k):
            return [PARTNER_45] if a[5] == "res.partner" else []

        with patch.object(billing, "odoo_search",
                          side_effect=_make_search(journals=None)), \
             patch.object(billing, "odoo_read", side_effect=_fake_read):
            r = billing.create_invoice(
                *CREDS, partner_id=45,
                lines=[{"item": "Manicura semipermanente"}],
            )
        assert r["success"] is False
        assert r["error_code"] == "no_sale_journal"

    def test_message_post_failure_does_not_abort(self):
        from mcp_odoo.tools import billing

        def _fake_read(*a, **k):
            model = a[5]
            if model == "res.partner":
                return [PARTNER_45]
            if model == "account.move":
                return [MOVE_READBACK]
            return []

        def _fail_call(*a, **k):
            raise RuntimeError("ACL denied message_post")

        with patch.object(billing, "odoo_search", side_effect=_make_search()), \
             patch.object(billing, "odoo_read", side_effect=_fake_read), \
             patch.object(billing, "odoo_create", side_effect=lambda *a, **k: 100), \
             patch.object(billing, "odoo_call_method", side_effect=_fail_call):
            r = billing.create_invoice(
                *CREDS, partner_id=45,
                lines=[{"item": "Manicura semipermanente"}],
            )
        # Invoice still created successfully even though the notice failed.
        assert r["success"] is True
        assert r["invoice_id"] == 100


# ---------------------------------------------------------------------------
# formatter
# ---------------------------------------------------------------------------

class TestFormatter:
    def test_success_render_chat_safe(self):
        from mcp_odoo.formatters.whatsapp_billing import format_invoice_created
        result = {
            "success": True,
            "invoice_id": 100,
            "name": "FAC-001",
            "partner_name": "Cliente de Prueba",
            "state": "draft",
            "currency": "USD",
            "amount_total": 22.4,
            "lines": [
                {"item": "Manicura semipermanente", "quantity": 1},
            ],
            "staff_notice": True,
        }
        text = format_invoice_created(result)
        assert "borrador" in text.lower()
        assert "Afrodita" in text
        assert "SRI" in text
        assert "Manicura semipermanente" in text
        # Chat-safe: no markdown tables / pipes / tabs.
        assert "|" not in text
        assert "\t" not in text
        assert "---" not in text

    def test_error_render(self):
        from mcp_odoo.formatters.whatsapp_billing import format_invoice_created
        result = {
            "success": False,
            "error_code": "item_not_found",
            "error_detail": "No encontré en el catálogo: 'Xyz'.",
        }
        text = format_invoice_created(result)
        assert "Xyz" in text
