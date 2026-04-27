"""Tests for Sprint 2 B2B Sales Assistant tools.

Covers:
  - odoo_lookup_user_by_email
  - odoo_create_quotation salesperson_user_id forwarding
  - odoo_add_to_quotation salesperson_user_id no-op
  - odoo_apply_discount
  - odoo_list_my_quotations
  - odoo_schedule_visit

All tools route their I/O through ``odoo_pool.execute`` (via the
``odoo_search``, ``odoo_read``, ``odoo_create`` and ``odoo_call_method``
helpers in ``mcp_odoo.tools.generic``). That single boundary lets us mock
the network calls with one ``patch``-and-``side_effect`` per test.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient


# Match the constants that test_server.py uses so we can re-use the same
# fixture style (env vars + dependency override).
JWT_SECRET = "test-jwt-secret-for-testing-only-32bytes!"
MOCK_CONFIG = {
    "tenant_id": "test-tenant-001",
    "url": "http://fake-odoo:8069",
    "db": "testdb",
    "user": "admin",
    "password": "admin",
}


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


@pytest.fixture
def client():
    from importlib import reload

    from mcp_odoo import config

    reload(config)
    from mcp_odoo.server import app, get_tenant_odoo_config

    async def mock_config():
        return MOCK_CONFIG

    app.dependency_overrides[get_tenant_odoo_config] = mock_config
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def make_jwt(tenant_id: str = "test-tenant-001") -> str:
    return jwt.encode(
        {"tenant_id": tenant_id, "role": "service"},
        JWT_SECRET,
        algorithm="HS256",
    )


# ─────────────────────────────────────────────────────────────────────
# odoo_lookup_user_by_email
# ─────────────────────────────────────────────────────────────────────


class TestLookupUserByEmail:
    """Tool: locate a salesperson by email (login OR partner.email)."""

    def test_login_match_happy_path(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # search_read on res.users by login
            [{
                "id": 7,
                "name": "Vendedora Ana",
                "login": "ana@tecnosmart.ec",
                "partner_id": [42, "Vendedora Ana"],
                "active": True,
            }],
            # read partner for canonical email/name
            [{"id": 42, "email": "ana@tecnosmart.ec", "name": "Vendedora Ana"}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_lookup_user_by_email",
                json={"email": "ana@tecnosmart.ec"},
            )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True
        user = result["user"]
        assert user["user_id"] == 7
        assert user["login"] == "ana@tecnosmart.ec"
        assert user["email"] == "ana@tecnosmart.ec"
        assert user["partner_id"] == 42
        assert user["partner_name"] == "Vendedora Ana"

    def test_falls_back_to_partner_email(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) login search → empty
            [],
            # 2) partner_id.email search → match
            [{
                "id": 9,
                "name": "Vendedor Juan",
                "login": "jdoe",  # username, not email
                "partner_id": [55, "Vendedor Juan"],
                "active": True,
            }],
            # 3) read partner for canonical email
            [{"id": 55, "email": "juan@tecnosmart.ec", "name": "Vendedor Juan"}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_lookup_user_by_email",
                json={"email": "juan@tecnosmart.ec"},
            )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True
        assert result["user"]["user_id"] == 9
        assert result["user"]["email"] == "juan@tecnosmart.ec"
        assert result["user"]["login"] == "jdoe"

    def test_user_not_found(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [[], []]  # login + partner.email both empty
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_lookup_user_by_email",
                json={"email": "ghost@nope.ec"},
            )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "user_not_found"

    def test_inactive_filtered_out(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # login match but inactive
            [{
                "id": 5, "name": "Ex empleado", "login": "ex@x.ec",
                "partner_id": [3, "Ex"], "active": False,
            }],
            # fallback partner.email also empty
            [],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_lookup_user_by_email",
                json={"email": "ex@x.ec"},
            )

        # Inactive users are filtered out; the function returns user_not_found
        # WITHOUT issuing a fallback search (only triggers on empty rows).
        # So we should NOT have called partner.email — but our mock has 2
        # responses queued. The first call consumed the inactive list, then
        # the active filter rejected it → user_not_found.
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "user_not_found"

    def test_invalid_email_rejected(self, client):
        # Empty-string ⇒ rejected before any Odoo call.
        mock_pool = MagicMock()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_lookup_user_by_email",
                json={"email": "   "},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "invalid_email"
        mock_pool.execute.assert_not_called()

    def test_multiple_actives_picks_lowest_id(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [
                {"id": 12, "name": "Dup B", "login": "shared@x.ec",
                 "partner_id": [22, "Dup B"], "active": True},
                {"id": 4, "name": "Dup A", "login": "shared@x.ec",
                 "partner_id": [11, "Dup A"], "active": True},
            ],
            # read partner for the chosen (id=4 → partner 11)
            [{"id": 11, "email": "shared@x.ec", "name": "Dup A"}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_lookup_user_by_email",
                json={"email": "shared@x.ec"},
            )
        result = response.json()["result"]
        assert result["success"] is True
        assert result["user"]["user_id"] == 4


# ─────────────────────────────────────────────────────────────────────
# odoo_create_quotation salesperson_user_id forwarding
# ─────────────────────────────────────────────────────────────────────


class TestCreateQuotationSalesperson:
    """The ``salesperson_user_id`` argument must land in the create vals."""

    def _build_pool(self) -> MagicMock:
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) partner check
            [{"id": 1, "name": "Cliente", "vat": "1234567890"}],
            # 2) variant lookup (legacy product_id path → no SKU lookup)
            [{"id": 50, "product_tmpl_id": [10, "Producto"], "uom_id": [1, "Units"]}],
            # 3) create returns order_id
            101,
            # 4) read sale.order header
            [{"id": 101, "name": "SO-2026-0099", "state": "draft",
              "partner_id": [1, "Cliente"], "amount_untaxed": 100.0,
              "amount_tax": 15.0, "amount_total": 115.0,
              "order_line": [1], "date_order": "2026-04-01",
              "share_link_so": ""}],
            # 5) read sale.order.line
            [{"product_id": [50, "Producto"], "name": "Producto",
              "product_uom_qty": 1, "price_unit": 100.0, "discount": 0,
              "price_subtotal": 100.0, "price_tax": 15.0,
              "price_total": 115.0}],
        ]
        return mock_pool

    def test_salesperson_user_id_in_create_vals(self, client):
        mock_pool = self._build_pool()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [{"product_id": 10, "quantity": 1}],
                    "salesperson_user_id": 7,
                },
            )
        assert response.status_code == 200
        assert response.json()["result"]["success"] is True

        # The 3rd call (index 2) is sale.order create. Inspect its values.
        create_call = mock_pool.execute.call_args_list[2]
        # Signature: (tenant, url, db, user, pwd, model, method, args, kwargs)
        positional = create_call.args
        assert positional[5] == "sale.order"
        assert positional[6] == "create"
        values = positional[7][0]  # args list, then the values dict
        assert values["user_id"] == 7
        assert values["partner_id"] == 1

    def test_salesperson_omitted_when_none(self, client):
        mock_pool = self._build_pool()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [{"product_id": 10, "quantity": 1}],
                },
            )
        assert response.status_code == 200
        assert response.json()["result"]["success"] is True

        create_call = mock_pool.execute.call_args_list[2]
        values = create_call.args[7][0]
        assert "user_id" not in values, (
            "When salesperson_user_id is None, user_id must NOT be in create vals "
            "so Odoo defaults to the connection user."
        )


# ─────────────────────────────────────────────────────────────────────
# odoo_add_to_quotation salesperson_user_id no-op
# ─────────────────────────────────────────────────────────────────────


class TestAddToQuotationSalesperson:
    """``odoo_add_to_quotation`` accepts the param but never overwrites
    ``user_id`` — it only ever merges into an existing order."""

    def test_salesperson_param_does_not_modify_user_id(self):
        # Call the python function directly (not via HTTP) because the
        # add_to_quotation endpoint isn't registered in server.py — the
        # MCP transport is the only entry point.
        from mcp_odoo.tools import sales as sales_mod

        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read existing order header (state=draft, editable)
            [{"id": 88, "state": "draft", "partner_id": [1, "Cliente"],
              "name": "SO-EXISTING"}],
            # 2) variant lookup for the new line
            [{"id": 50, "product_tmpl_id": [10, "Producto"], "uom_id": [1, "Units"]}],
            # 3) create new sale.order.line → returns id
            500,
            # 4) read updated header
            [{"id": 88, "name": "SO-EXISTING", "state": "draft",
              "partner_id": [1, "Cliente"], "amount_untaxed": 100.0,
              "amount_tax": 15.0, "amount_total": 115.0,
              "order_line": [500], "share_link_so": ""}],
            # 5) read order line
            [{"product_id": [50, "Producto"], "product_uom_qty": 1,
              "price_unit": 100.0, "price_subtotal": 100.0, "price_total": 115.0}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_add_to_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                88,
                [{"product_id": 10, "quantity": 1}],
                salesperson_user_id=99,
            )

        assert result["success"] is True

        # Across all calls, never write user_id on sale.order.
        for call in mock_pool.execute.call_args_list:
            args = call.args
            model = args[5]
            method = args[6]
            if model == "sale.order" and method in ("write", "create"):
                # No "user_id" should be in the payload from this tool.
                payload = args[7][0] if isinstance(args[7][0], dict) else args[7][1]
                if isinstance(payload, dict):
                    assert "user_id" not in payload


# ─────────────────────────────────────────────────────────────────────
# odoo_apply_discount
# ─────────────────────────────────────────────────────────────────────


class TestApplyDiscount:
    """Apply % discount to one line or all lines of a quotation."""

    def test_apply_to_all_lines_happy_path(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read sale.order header
            [{"id": 88, "name": "SO-1", "state": "draft",
              "order_line": [10, 11, 12], "user_id": [7, "Ana"]}],
            # 2) write discount on lines (returns True)
            True,
            # 3) message_post (best-effort, but reason is not provided here)
            # ↳ skipped because reason=None
            # 4) re-read totals
            [{"amount_total": 90.0, "amount_untaxed": 78.26, "amount_tax": 11.74}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_apply_discount",
                json={"order_id": 88, "discount_pct": 10.0},
            )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True
        assert result["lines_updated"] == 3
        assert result["discount_pct"] == 10.0
        assert result["new_amount_total"] == 90.0

        # Confirm write payload
        write_call = mock_pool.execute.call_args_list[1]
        # signature: (tenant, url, db, user, pwd, model, method, args, kwargs)
        assert write_call.args[5] == "sale.order.line"
        assert write_call.args[6] == "write"
        # args = [target_ids, {discount: pct}]  (note: write payload comes via
        # the args list passed to execute as the 8th positional)
        ids_arg = write_call.args[7][0]
        vals_arg = write_call.args[7][1]
        assert ids_arg == [10, 11, 12]
        assert vals_arg == {"discount": 10.0}

    def test_apply_to_single_line_happy_path(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read sale.order
            [{"id": 88, "name": "SO-1", "state": "draft",
              "order_line": [10, 11], "user_id": [7, "Ana"]}],
            # 2) read sale.order.line for ownership check
            [{"id": 11, "order_id": [88, "SO-1"]}],
            # 3) write discount
            True,
            # 4) re-read totals
            [{"amount_total": 50.0, "amount_untaxed": 43.48, "amount_tax": 6.52}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_apply_discount",
                json={"order_id": 88, "discount_pct": 5.0, "line_id": 11},
            )

        result = response.json()["result"]
        assert result["success"] is True
        assert result["lines_updated"] == 1

        write_call = mock_pool.execute.call_args_list[2]
        ids_arg = write_call.args[7][0]
        assert ids_arg == [11]

    def test_line_belongs_to_other_order(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read order
            [{"id": 88, "name": "SO-1", "state": "draft",
              "order_line": [10, 11], "user_id": [7, "Ana"]}],
            # 2) line check returns a line whose order_id != 88
            [{"id": 999, "order_id": [12345, "OTHER"]}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_apply_discount",
                json={"order_id": 88, "discount_pct": 5.0, "line_id": 999},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "line_mismatch"

    def test_order_not_editable(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 88, "name": "SO-CONFIRMED", "state": "sale",
              "order_line": [10], "user_id": [7, "Ana"]}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_apply_discount",
                json={"order_id": 88, "discount_pct": 5.0},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "order_not_editable"

    @pytest.mark.parametrize("bad_pct", [-1, 101, 150, -0.5])
    def test_invalid_discount_range(self, client, bad_pct):
        mock_pool = MagicMock()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_apply_discount",
                json={"order_id": 88, "discount_pct": bad_pct},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "invalid_discount"
        mock_pool.execute.assert_not_called()  # rejected before Odoo call

    def test_reason_posts_chatter_note_best_effort(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read order
            [{"id": 88, "name": "SO-1", "state": "draft",
              "order_line": [10], "user_id": [7, "Ana"]}],
            # 2) write discount
            True,
            # 3) message_post (called because reason is set)
            True,
            # 4) re-read totals
            [{"amount_total": 90.0, "amount_untaxed": 78.26, "amount_tax": 11.74}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_apply_discount",
                json={
                    "order_id": 88,
                    "discount_pct": 10.0,
                    "reason": "Cliente premium",
                },
            )
        assert response.json()["result"]["success"] is True
        # Find the message_post call.
        post_calls = [
            c for c in mock_pool.execute.call_args_list
            if c.args[5] == "sale.order" and c.args[6] == "message_post"
        ]
        assert len(post_calls) == 1
        # body kwarg carries the reason text
        body = post_calls[0].args[8]["body"]
        assert "Cliente premium" in body
        assert "10.0" in body

    def test_message_post_failure_does_not_break(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 88, "name": "SO-1", "state": "draft",
              "order_line": [10], "user_id": [7, "Ana"]}],
            True,                                           # write OK
            Exception("chatter-down"),                       # message_post fails
            [{"amount_total": 90.0, "amount_untaxed": 78.26, "amount_tax": 11.74}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_apply_discount",
                json={"order_id": 88, "discount_pct": 10.0,
                      "reason": "Whatever"},
            )
        # Tool should still succeed.
        result = response.json()["result"]
        assert result["success"] is True


# ─────────────────────────────────────────────────────────────────────
# odoo_list_my_quotations
# ─────────────────────────────────────────────────────────────────────


class TestListMyQuotations:
    """List quotations belonging to a salesperson."""

    def test_default_filter_returns_quotations(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) sale.order search
            [
                {"id": 200, "name": "SO-200", "partner_id": [1, "ACME"],
                 "amount_total": 1500.0, "state": "draft",
                 "date_order": "2026-04-20 10:00:00",
                 "order_line": [10, 11]},
                {"id": 201, "name": "SO-201", "partner_id": [2, "Foo SA"],
                 "amount_total": 800.0, "state": "sent",
                 "date_order": "2026-04-19 11:00:00",
                 "order_line": [12]},
            ],
            # 2) batch read partners
            [
                {"id": 1, "name": "ACME", "vat": "0992345678001"},
                {"id": 2, "name": "Foo SA", "vat": "0991111111001"},
            ],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_list_my_quotations",
                json={"salesperson_user_id": 7},
            )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True
        assert result["count"] == 2
        first = result["quotations"][0]
        assert first["order_id"] == 200
        assert first["partner_name"] == "ACME"
        assert first["partner_vat"] == "0992345678001"
        assert first["line_count"] == 2

        # Verify the search domain used
        search_call = mock_pool.execute.call_args_list[0]
        # signature: (..., model, method, [domain], kwargs)
        assert search_call.args[5] == "sale.order"
        assert search_call.args[6] == "search_read"
        domain = search_call.args[7][0]
        assert ["user_id", "=", 7] in domain
        assert ["state", "in", ["draft", "sent"]] in domain

    def test_invalid_state(self, client):
        mock_pool = MagicMock()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_list_my_quotations",
                json={"salesperson_user_id": 7, "state": ["bogus"]},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "invalid_state"
        mock_pool.execute.assert_not_called()

    def test_invalid_salesperson_id(self, client):
        mock_pool = MagicMock()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_list_my_quotations",
                json={"salesperson_user_id": 0},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "invalid_salesperson_user_id"

    def test_empty_result(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [[]]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_list_my_quotations",
                json={"salesperson_user_id": 7},
            )
        result = response.json()["result"]
        assert result["success"] is True
        assert result["count"] == 0
        assert result["quotations"] == []

    def test_custom_state_widens_filter(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 300, "name": "SO-300", "partner_id": [1, "ACME"],
              "amount_total": 1000.0, "state": "sale",
              "date_order": "2026-04-15 09:00:00",
              "order_line": [33]}],
            [{"id": 1, "name": "ACME", "vat": "0992345678001"}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_list_my_quotations",
                json={"salesperson_user_id": 7, "state": ["sale", "done"]},
            )
        result = response.json()["result"]
        assert result["success"] is True
        domain = mock_pool.execute.call_args_list[0].args[7][0]
        assert ["state", "in", ["sale", "done"]] in domain


# ─────────────────────────────────────────────────────────────────────
# odoo_schedule_visit
# ─────────────────────────────────────────────────────────────────────


class TestScheduleVisit:
    """Create a Meeting activity on a partner."""

    def test_happy_path_creates_activity(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) mail.activity.type Meeting search
            [{"id": 3, "name": "Meeting"}],
            # 2) ir.model res.partner search
            [{"id": 7, "model": "res.partner"}],
            # 3) read partner
            [{"id": 42, "name": "Cliente VIP"}],
            # 4) create mail.activity → returns activity_id
            999,
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_schedule_visit",
                json={
                    "partner_id": 42,
                    "summary": "Visita comercial",
                    "date_deadline": "2026-05-10",
                    "salesperson_user_id": 7,
                    "note": "Llevar muestras de oferta",
                },
            )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True
        assert result["activity_id"] == 999
        assert result["partner_name"] == "Cliente VIP"

        # Inspect create vals
        create_call = mock_pool.execute.call_args_list[3]
        assert create_call.args[5] == "mail.activity"
        assert create_call.args[6] == "create"
        vals = create_call.args[7][0]
        assert vals["activity_type_id"] == 3
        assert vals["res_model_id"] == 7
        assert vals["res_model"] == "res.partner"
        assert vals["res_id"] == 42
        assert vals["user_id"] == 7
        assert vals["summary"] == "Visita comercial"
        assert vals["date_deadline"] == "2026-05-10"
        # note auto-wrapped in <p>
        assert vals["note"] == "<p>Llevar muestras de oferta</p>"

    def test_html_note_passes_through(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 3, "name": "Meeting"}],
            [{"id": 7, "model": "res.partner"}],
            [{"id": 42, "name": "Cliente"}],
            999,
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_schedule_visit",
                json={
                    "partner_id": 42,
                    "summary": "Visita",
                    "date_deadline": "2026-05-10",
                    "salesperson_user_id": 7,
                    "note": "<div>Pre-rendered <b>HTML</b></div>",
                },
            )
        assert response.json()["result"]["success"] is True
        vals = mock_pool.execute.call_args_list[3].args[7][0]
        # Already HTML → not wrapped again
        assert vals["note"] == "<div>Pre-rendered <b>HTML</b></div>"

    def test_meeting_type_fallback_ilike(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) exact name lookup fails
            [],
            # 2) ilike fallback succeeds (e.g. "Reunion - Meeting")
            [{"id": 4, "name": "Reunion (meeting)"}],
            # 3) ir.model
            [{"id": 7, "model": "res.partner"}],
            # 4) partner
            [{"id": 42, "name": "Cliente"}],
            # 5) create
            555,
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_schedule_visit",
                json={
                    "partner_id": 42,
                    "summary": "Visita",
                    "date_deadline": "2026-05-10",
                    "salesperson_user_id": 7,
                },
            )
        result = response.json()["result"]
        assert result["success"] is True
        assert result["activity_id"] == 555

    def test_meeting_type_completely_missing(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [[], []]  # both lookups empty
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_schedule_visit",
                json={
                    "partner_id": 42,
                    "summary": "Visita",
                    "date_deadline": "2026-05-10",
                    "salesperson_user_id": 7,
                },
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "meeting_activity_type_missing"

    def test_partner_not_found(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 3, "name": "Meeting"}],
            [{"id": 7, "model": "res.partner"}],
            [],  # partner lookup empty
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_schedule_visit",
                json={
                    "partner_id": 9999,
                    "summary": "Visita",
                    "date_deadline": "2026-05-10",
                    "salesperson_user_id": 7,
                },
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "partner_not_found"

    @pytest.mark.parametrize(
        "bad_date",
        ["2026/05/10", "10-05-2026", "2026-13-01", "not-a-date", "", "2026-05"],
    )
    def test_invalid_date_format(self, client, bad_date):
        mock_pool = MagicMock()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_schedule_visit",
                json={
                    "partner_id": 42,
                    "summary": "Visita",
                    "date_deadline": bad_date,
                    "salesperson_user_id": 7,
                },
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "invalid_date"
        mock_pool.execute.assert_not_called()

    def test_missing_summary(self, client):
        mock_pool = MagicMock()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_schedule_visit",
                json={
                    "partner_id": 42,
                    "summary": "   ",
                    "date_deadline": "2026-05-10",
                    "salesperson_user_id": 7,
                },
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "invalid_summary"
        mock_pool.execute.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Sprint 2F — odoo_get_discount_policy
# ─────────────────────────────────────────────────────────────────────


class TestGetDiscountPolicy:
    """Generic ERP-agnostic 'get discount policy' contract.

    Reads ``ir.config_parameter`` + the supervisor group, then returns a
    plugin-agnostic shape. Niko core never sees the Odoo specifics.
    """

    def test_happy_path_with_two_supervisors(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) ir.config_parameter.get_param
            "15",
            # 2) ir.model.data search for account.group_account_manager
            [{"id": 100, "res_id": 77}],
            # 3) res.users search for supervisors in group 77
            [
                {"id": 1, "name": "Sup A", "login": "supa@x.ec",
                 "partner_id": [10, "Sup A"]},
                {"id": 2, "name": "Sup B", "login": "supb",
                 "partner_id": [11, "Sup B"]},
            ],
            # 4) res.partner read for canonical emails
            [
                {"id": 10, "email": "supa.canonical@x.ec"},
                {"id": 11, "email": "supb.canonical@x.ec"},
            ],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post("/tools/odoo_get_discount_policy", json={})
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True
        policy = result["policy"]
        assert policy["max_pct"] == 15.0
        assert len(policy["supervisors"]) == 2
        emails = {s["email"] for s in policy["supervisors"]}
        assert emails == {"supa.canonical@x.ec", "supb.canonical@x.ec"}
        # Source metadata is the dashboard's "from Odoo" badge
        assert policy["source"]["max_pct_key"] == \
            "ir.config_parameter:sale.partner_max_sale_discount"
        assert policy["source"]["supervisors_group_xmlid"] == \
            "account.group_account_manager"

    def test_max_pct_missing_returns_zero(self, client):
        """ir.config_parameter.get_param returns the default '0' when the
        key has never been set; we coerce to 0.0 (== 'no control')."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) get_param → '0' default
            "0",
            # 2) supervisor group resolves
            [{"id": 100, "res_id": 77}],
            # 3) one active supervisor
            [{"id": 1, "name": "Sup", "login": "sup@x.ec",
              "partner_id": [10, "Sup"]}],
            # 4) partner email
            [{"id": 10, "email": "sup@x.ec"}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post("/tools/odoo_get_discount_policy", json={})
        result = response.json()["result"]
        assert result["success"] is True
        assert result["policy"]["max_pct"] == 0.0

    def test_max_pct_non_numeric_falls_back_to_zero(self, client):
        """Defensive: an admin typo ('abc') in the parameter must NOT crash.
        Plugin treats it as 'no control configured' (0.0)."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            "abc",
            [{"id": 100, "res_id": 77}],
            [],  # no supervisors
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post("/tools/odoo_get_discount_policy", json={})
        result = response.json()["result"]
        assert result["success"] is True
        assert result["policy"]["max_pct"] == 0.0

    def test_supervisor_group_missing(self, client):
        """If account module is not installed, ir.model.data lookup is
        empty — return error_code='supervisor_group_missing'."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            "10",
            [],  # no row in ir.model.data → group not present
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post("/tools/odoo_get_discount_policy", json={})
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "supervisor_group_missing"
        assert "account" in result["error_detail"].lower()

    def test_zero_supervisors_in_group(self, client):
        """Group exists but has no users assigned → success=True with empty
        supervisors list (no one can authorize discounts, but that is a
        config decision, not an error)."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            "10",
            [{"id": 100, "res_id": 77}],
            [],  # no supervisors
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post("/tools/odoo_get_discount_policy", json={})
        result = response.json()["result"]
        assert result["success"] is True
        assert result["policy"]["max_pct"] == 10.0
        assert result["policy"]["supervisors"] == []

    def test_supervisor_login_used_when_partner_email_empty(self, client):
        """When res.partner.email is empty BUT res.users.login looks like an
        email, fall back to login as the canonical address."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            "5",
            [{"id": 100, "res_id": 77}],
            [{"id": 1, "name": "Sup", "login": "sup@x.ec",
              "partner_id": [10, "Sup"]}],
            [{"id": 10, "email": False}],  # partner has no email
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post("/tools/odoo_get_discount_policy", json={})
        result = response.json()["result"]
        sup = result["policy"]["supervisors"][0]
        assert sup["email"] == "sup@x.ec"
        assert sup["login"] == "sup@x.ec"


# ─────────────────────────────────────────────────────────────────────
# Sprint 2F — odoo_verify_seller_authorization
# ─────────────────────────────────────────────────────────────────────


class TestVerifySellerAuthorization:
    """Generic 'is this email an authorized seller in the ERP?' contract.

    Layered on top of ``odoo_lookup_user_by_email``: lookup + group
    membership check, all in one round-trip.
    """

    def test_authorized_seller(self, client):
        """Email matches an active res.users AND user is in the seller group."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # --- odoo_lookup_user_by_email ---
            # 1) login search
            [{"id": 7, "name": "Vendedora Ana", "login": "ana@x.ec",
              "partner_id": [42, "Vendedora Ana"], "active": True}],
            # 2) read partner
            [{"id": 42, "email": "ana@x.ec", "name": "Vendedora Ana"}],
            # --- group resolution ---
            # 3) ir.model.data sales_team.group_sale_salesman
            [{"id": 200, "res_id": 88}],
            # 4) read res.users.groups_id
            [{"id": 7, "groups_id": [1, 2, 88, 99]}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_verify_seller_authorization",
                json={"email": "ana@x.ec"},
            )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True
        assert result["authorized"] is True
        assert result["reason"] is None
        assert result["user"]["user_id"] == 7
        assert result["user"]["email"] == "ana@x.ec"
        assert result["user"]["partner_id"] == 42

    def test_user_exists_but_not_in_seller_group(self, client):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 7, "name": "Soporte", "login": "soporte@x.ec",
              "partner_id": [42, "Soporte"], "active": True}],
            [{"id": 42, "email": "soporte@x.ec", "name": "Soporte"}],
            [{"id": 200, "res_id": 88}],
            # user has groups but NOT 88 (the seller group)
            [{"id": 7, "groups_id": [1, 2, 99]}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_verify_seller_authorization",
                json={"email": "soporte@x.ec"},
            )
        result = response.json()["result"]
        assert result["success"] is True
        assert result["authorized"] is False
        assert result["user"] is not None
        assert result["user"]["user_id"] == 7
        assert "Ventas" in result["reason"]

    def test_user_not_found(self, client):
        """No res.users with that email — authorized=false with explicit reason."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [],  # login search empty
            [],  # partner.email fallback also empty
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_verify_seller_authorization",
                json={"email": "ghost@nope.ec"},
            )
        result = response.json()["result"]
        assert result["success"] is True
        assert result["authorized"] is False
        assert result["user"] is None
        assert "registrado" in result["reason"].lower()

    def test_seller_group_missing(self, client):
        """sales_team module not installed → error_code='seller_group_missing'."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 7, "name": "Ana", "login": "ana@x.ec",
              "partner_id": [42, "Ana"], "active": True}],
            [{"id": 42, "email": "ana@x.ec", "name": "Ana"}],
            [],  # ir.model.data lookup empty → group not present
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_verify_seller_authorization",
                json={"email": "ana@x.ec"},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "seller_group_missing"

    def test_invalid_email_propagates(self, client):
        """Empty / whitespace email is rejected by the underlying lookup."""
        mock_pool = MagicMock()
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_verify_seller_authorization",
                json={"email": "   "},
            )
        result = response.json()["result"]
        assert result["success"] is False
        assert result["error_code"] == "invalid_email"
        mock_pool.execute.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Sprint 6 — line_id per-line envelope invariant
# ─────────────────────────────────────────────────────────────────────
#
# After Sprint 6 in niko core (commit 0d90ba7), the agent loop maintains
# a persistent ``active_quotation_lines`` state that mirrors the
# ``lines[]`` array of the most recent quotation envelope. The extractor
# requires ``line_id`` per line and silently drops lines that lack it
# (see niko.agent.active_lines._normalize_line). These tests pin down
# the envelope shape for the three quotation tools that mutate / read
# the order so the self-healing loop never starves for line_id.


class TestGetQuotationLineIdEnvelope:
    """``odoo_get_quotation`` must emit ``line_id`` for every line."""

    def test_get_quotation_emits_line_id_per_line(self):
        """A 3-line SO returns lines[] with line_id=ln.id on each row."""
        from mcp_odoo.tools import sales as sales_mod

        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read sale.order header
            [{
                "id": 555, "name": "VENTA-3LINES", "state": "draft",
                "partner_id": [1, "Cliente"], "amount_total": 345.0,
                "amount_untaxed": 300.0, "amount_tax": 45.0,
                "date_order": "2026-04-27", "create_date": "2026-04-27",
                "order_line": [10, 11, 12],
            }],
            # 2) read sale.order.line — MUST include 'id' so we can
            # echo it as 'line_id' in the envelope.
            [
                {"id": 10, "product_id": [50, "Cable UTP"], "name": "Cable UTP",
                 "product_uom_qty": 1, "price_unit": 100.0, "discount": 0,
                 "price_subtotal": 100.0, "price_tax": 15.0, "price_total": 115.0},
                {"id": 11, "product_id": [51, "Switch 8p"], "name": "Switch 8p",
                 "product_uom_qty": 2, "price_unit": 50.0, "discount": 0,
                 "price_subtotal": 100.0, "price_tax": 15.0, "price_total": 115.0},
                {"id": 12, "product_id": [52, "Patch panel"], "name": "Patch panel",
                 "product_uom_qty": 1, "price_unit": 100.0, "discount": 0,
                 "price_subtotal": 100.0, "price_tax": 15.0, "price_total": 115.0},
            ],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_get_quotation(
                "test-tenant-001", "http://x", "db", "u", "p", 555,
            )

        assert result["success"] is True
        assert len(result["lines"]) == 3
        assert [ln["line_id"] for ln in result["lines"]] == [10, 11, 12]
        # Sanity: each line carries the canonical fields that the
        # niko extractor reads (product, quantity, price_unit, total).
        for ln in result["lines"]:
            assert "product" in ln
            assert "quantity" in ln
            assert "price_unit" in ln
            assert "total" in ln

        # The read on sale.order.line MUST request 'id' — without it
        # the regression resurfaces. odoo_read passes fields via the
        # kwargs slot (index 8): execute(..., model, method, [ids],
        # {"fields": [...]}). See mcp_odoo.tools.generic.odoo_read.
        line_read_call = mock_pool.execute.call_args_list[1]
        assert line_read_call.args[5] == "sale.order.line"
        assert line_read_call.args[6] == "read"
        fields_kwargs = line_read_call.args[8]
        assert "id" in fields_kwargs.get("fields", []), (
            "sale.order.line read must include 'id' so the envelope can "
            "expose it as line_id (Sprint 6 invariant)."
        )


class TestUpdateQuotationLineEnvelope:
    """``odoo_update_quotation_line`` must return the full ``lines[]``
    array of the SO after the write — not just the modified line."""

    def test_update_quotation_line_returns_full_lines_array(self):
        """A 3-line SO + update on line 11 returns all 3 lines refreshed."""
        from mcp_odoo.tools import sales as sales_mod

        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read sale.order header (state, user_id, name, order_line)
            [{
                "id": 555, "state": "draft", "name": "VENTA-3LINES",
                "user_id": [7, "Ana"], "order_line": [10, 11, 12],
            }],
            # 2) write on sale.order.line (returns True)
            True,
            # 3) read sale.order header AFTER the write
            [{
                "id": 555, "name": "VENTA-3LINES", "state": "draft",
                "partner_id": [1, "Cliente"], "amount_untaxed": 350.0,
                "amount_tax": 52.5, "amount_total": 402.5,
                "order_line": [10, 11, 12], "share_link_so": "",
            }],
            # 4) read ALL sale.order.line rows of the SO (line 11 now
            # has the new qty=5).
            [
                {"id": 10, "product_id": [50, "Cable UTP"], "name": "Cable UTP",
                 "product_uom_qty": 1, "price_unit": 100.0, "discount": 0,
                 "price_subtotal": 100.0, "price_tax": 15.0, "price_total": 115.0},
                {"id": 11, "product_id": [51, "Switch 8p"], "name": "Switch 8p",
                 "product_uom_qty": 5, "price_unit": 50.0, "discount": 0,
                 "price_subtotal": 250.0, "price_tax": 37.5, "price_total": 287.5},
                {"id": 12, "product_id": [52, "Patch panel"], "name": "Patch panel",
                 "product_uom_qty": 1, "price_unit": 100.0, "discount": 0,
                 "price_subtotal": 100.0, "price_tax": 15.0, "price_total": 115.0},
            ],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_update_quotation_line(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=555, line_id=11, quantity=5,
            )

        assert result["success"] is True
        # Backwards-compat top-level fields about the modified line
        assert result["line_id"] == 11
        assert result["quantity"] == 5
        # NEW (Sprint 6): full lines[] array with all live lines + line_id
        assert "lines" in result
        assert len(result["lines"]) == 3
        assert [ln["line_id"] for ln in result["lines"]] == [10, 11, 12]
        # Modified line shows the new qty in the array as well.
        modified = next(ln for ln in result["lines"] if ln["line_id"] == 11)
        assert modified["quantity"] == 5

    def test_update_quotation_line_quantity_zero_envelope_preserved(self):
        """qty=0 still returns the quantity_zero envelope so the LLM
        asks for explicit confirmation before remove_quotation_line."""
        from mcp_odoo.tools import sales as sales_mod

        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # read sale.order header
            [{
                "id": 555, "state": "draft", "name": "VENTA-3LINES",
                "user_id": [7, "Ana"], "order_line": [10, 11, 12],
            }],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_update_quotation_line(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=555, line_id=11, quantity=0,
            )

        assert result["success"] is False
        assert result["error_code"] == "quantity_zero"
        assert result["order_id"] == 555
        assert result["line_id"] == 11
        # quantity_zero short-circuits BEFORE the write — only one
        # odoo call should have happened (the header read).
        assert mock_pool.execute.call_count == 1


class TestRemoveQuotationLineEnvelope:
    """``odoo_remove_quotation_line`` must return the full ``lines[]``
    array of the SO after the unlink so niko's active_quotation_lines
    state stays in sync without a follow-up get_quotation."""

    def test_remove_quotation_line_returns_full_lines_array(self):
        """Removing line 11 from a 3-line SO returns lines[] with the 2
        remaining lines, each carrying line_id."""
        from mcp_odoo.tools import sales as sales_mod

        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) read sale.order header
            [{
                "id": 555, "state": "draft", "name": "VENTA-3LINES",
                "user_id": [7, "Ana"], "order_line": [10, 11, 12],
            }],
            # 2) unlink sale.order.line (returns True)
            True,
            # 3) read sale.order header AFTER the unlink — line 11 is gone
            [{
                "id": 555, "amount_untaxed": 200.0, "amount_tax": 30.0,
                "amount_total": 230.0, "order_line": [10, 12],
            }],
            # 4) read remaining sale.order.line rows
            [
                {"id": 10, "product_id": [50, "Cable UTP"], "name": "Cable UTP",
                 "product_uom_qty": 1, "price_unit": 100.0, "discount": 0,
                 "price_subtotal": 100.0, "price_tax": 15.0, "price_total": 115.0},
                {"id": 12, "product_id": [52, "Patch panel"], "name": "Patch panel",
                 "product_uom_qty": 1, "price_unit": 100.0, "discount": 0,
                 "price_subtotal": 100.0, "price_tax": 15.0, "price_total": 115.0},
            ],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_remove_quotation_line(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=555, line_id=11,
            )

        assert result["success"] is True
        assert result["removed_line_id"] == 11
        assert result["remaining_lines"] == 2
        # NEW (Sprint 6): full lines[] array with the surviving lines.
        assert "lines" in result
        assert len(result["lines"]) == 2
        assert [ln["line_id"] for ln in result["lines"]] == [10, 12]
        # Sanity: removed line is NOT in the array.
        assert 11 not in [ln["line_id"] for ln in result["lines"]]
