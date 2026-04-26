"""Tests for the MCP JSON-RPC transport (Sprint 2D).

Covers the protocol-layer registration of the three new B2B tools added in
Sprint 2A:
    - apply_discount
    - list_my_quotations
    - schedule_visit

These tools are exposed through the StreamableHTTP MCP endpoint at
``POST /mcp``. langchain-mcp-adapters consumes that endpoint, so a tool that
is missing from ``MCP_TOOLS`` or from the ``_execute_tool`` dispatch will be
invisible to the LLM agents.

The tests in this module hit the JSON-RPC layer directly and patch the
underlying ``mcp_odoo.tools.sales`` functions so we don't need a real Odoo.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient


JWT_SECRET = "test-jwt-secret-for-testing-only-32bytes!"

MOCK_TENANT_CONFIG = {
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
    """Spin up the FastAPI app and short-circuit tenant resolution.

    ``_get_tenant_config`` normally reaches out to Supabase + decrypts an
    encrypted Odoo password. For unit tests we patch it module-wide so every
    request resolves to ``MOCK_TENANT_CONFIG`` without touching the network.
    """
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


def _rpc(client: TestClient, method: str, params: dict | None = None,
         headers: dict | None = None) -> dict:
    """Send a JSON-RPC request to /mcp and return the parsed body."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post("/mcp", json=payload, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


# ─────────────────────────────────────────────────────────────────────
# tools/list registration
# ─────────────────────────────────────────────────────────────────────


class TestToolsListRegistration:
    """The three new B2B tools must appear in tools/list output."""

    def test_new_tools_listed_by_default(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]

        for expected in ("apply_discount", "list_my_quotations", "schedule_visit"):
            assert expected in names, (
                f"{expected} missing from MCP_TOOLS — agents won't see it"
            )

    def test_no_odoo_prefix_on_new_tools(self, client):
        """Niko's tools_enabled list omits the odoo_ prefix; mismatch breaks
        ToolNode dispatch on the Niko side."""
        body = _rpc(client, "tools/list")
        names = {t["name"] for t in body["result"]["tools"]}

        for expected in ("apply_discount", "list_my_quotations", "schedule_visit"):
            assert f"odoo_{expected}" not in names, (
                f"odoo_{expected} would shadow the registration — drop the prefix"
            )

    def test_lookup_user_by_email_backend_only(self, client):
        """Sprint 2E registers ``odoo_lookup_user_by_email`` (with prefix)
        because niko/auth/seller_otp.py invokes it over MCP JSON-RPC, not
        REST. The bare-name ``lookup_user_by_email`` stays out — only the
        prefixed backend variant exists, signalling 'not for LLMs'.

        Defense-in-depth: even though it appears in tools/list when no
        allowed-tools header is sent, the allowed_tools filter on
        tools/call (mcp_transport.py L1018-1031) blocks any agent whose
        tools_enabled does not include this name — which is every agent,
        because migration 350 only enables ``apply_discount`` /
        ``list_my_quotations`` / ``schedule_visit``."""
        body = _rpc(client, "tools/list")
        names = {t["name"] for t in body["result"]["tools"]}
        assert "lookup_user_by_email" not in names
        assert "odoo_lookup_user_by_email" in names

    def test_input_schema_present_and_valid(self, client):
        """Each new tool needs a JSONSchema-shaped inputSchema or langchain
        adapters will refuse to wrap it as a structured tool."""
        body = _rpc(client, "tools/list")
        by_name = {t["name"]: t for t in body["result"]["tools"]}

        ad = by_name["apply_discount"]
        assert ad["inputSchema"]["type"] == "object"
        assert set(ad["inputSchema"]["required"]) == {"order_id", "discount_pct"}
        props = ad["inputSchema"]["properties"]
        assert "line_id" in props and "reason" in props

        lmq = by_name["list_my_quotations"]
        assert lmq["inputSchema"]["required"] == ["salesperson_user_id"]
        assert lmq["inputSchema"]["properties"]["state"]["type"] == "array"

        sv = by_name["schedule_visit"]
        assert set(sv["inputSchema"]["required"]) == {
            "partner_id", "summary", "date_deadline", "salesperson_user_id",
        }


class TestAllowedToolsHeader:
    """X-Allowed-Tools must filter the tools/list output to that subset."""

    def test_filter_to_apply_discount_only(self, client):
        body = _rpc(client, "tools/list",
                    headers={"x-allowed-tools": "apply_discount"})
        names = [t["name"] for t in body["result"]["tools"]]
        assert names == ["apply_discount"], (
            f"X-Allowed-Tools filter failed; got {names}"
        )

    def test_filter_multiple(self, client):
        body = _rpc(
            client,
            "tools/list",
            headers={"x-allowed-tools": "apply_discount,schedule_visit"},
        )
        names = sorted(t["name"] for t in body["result"]["tools"])
        assert names == ["apply_discount", "schedule_visit"]

    def test_filter_blocks_call_for_disallowed(self, client):
        """A tool not in the allowlist must be rejected on tools/call."""
        body = _rpc(
            client,
            "tools/call",
            params={"name": "list_my_quotations",
                    "arguments": {"salesperson_user_id": 7}},
            headers={"x-allowed-tools": "apply_discount"},
        )
        assert body["result"]["isError"] is True
        text = body["result"]["content"][0]["text"]
        assert "list_my_quotations" in text


# ─────────────────────────────────────────────────────────────────────
# tools/call dispatch
# ─────────────────────────────────────────────────────────────────────


class TestApplyDiscountDispatch:
    """Verify /mcp tools/call routes to mcp_odoo.tools.sales.odoo_apply_discount
    with the correct creds + kwargs."""

    def test_basic_dispatch(self, client):
        fake_result = {
            "success": True,
            "order_id": 88,
            "lines_updated": 3,
            "discount_pct": 10.0,
            "new_amount_total": 90.0,
            "new_amount_untaxed": 78.26,
        }
        with patch(
            "mcp_odoo.tools.sales.odoo_apply_discount",
            return_value=fake_result,
        ) as mock_fn:
            body = _rpc(
                client,
                "tools/call",
                params={
                    "name": "apply_discount",
                    "arguments": {"order_id": 88, "discount_pct": 10.0},
                },
            )

        # Envelope shape — content list with text payload, no isError flag.
        result = body["result"]
        assert "content" in result
        assert "isError" not in result
        text = result["content"][0]["text"]
        assert '"success": true' in text
        assert '"new_amount_total": 90.0' in text

        # Confirm the dispatch unpacks creds + forwards kwargs verbatim.
        mock_fn.assert_called_once()
        called_args, called_kwargs = mock_fn.call_args
        assert called_args[0] == "test-tenant-001"
        assert called_args[1] == "http://fake-odoo:8069"
        assert called_args[2] == "testdb"
        assert called_kwargs == {
            "order_id": 88,
            "discount_pct": 10.0,
            "line_id": None,
            "reason": None,
        }

    def test_optional_args_propagate(self, client):
        with patch(
            "mcp_odoo.tools.sales.odoo_apply_discount",
            return_value={"success": True},
        ) as mock_fn:
            _rpc(
                client,
                "tools/call",
                params={
                    "name": "apply_discount",
                    "arguments": {
                        "order_id": 88,
                        "discount_pct": 5.0,
                        "line_id": 11,
                        "reason": "Cliente premium",
                    },
                },
            )
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["line_id"] == 11
        assert kwargs["reason"] == "Cliente premium"


class TestListMyQuotationsDispatch:
    def test_basic_dispatch(self, client):
        fake_result = {"success": True, "count": 0, "quotations": []}
        with patch(
            "mcp_odoo.tools.sales.odoo_list_my_quotations",
            return_value=fake_result,
        ) as mock_fn:
            body = _rpc(
                client,
                "tools/call",
                params={
                    "name": "list_my_quotations",
                    "arguments": {"salesperson_user_id": 7},
                },
            )
        assert "isError" not in body["result"]
        assert '"count": 0' in body["result"]["content"][0]["text"]

        kwargs = mock_fn.call_args.kwargs
        assert kwargs["salesperson_user_id"] == 7
        assert kwargs["state"] is None
        assert kwargs["limit"] == 20  # default

    def test_state_and_limit_forwarded(self, client):
        with patch(
            "mcp_odoo.tools.sales.odoo_list_my_quotations",
            return_value={"success": True, "count": 0, "quotations": []},
        ) as mock_fn:
            _rpc(
                client,
                "tools/call",
                params={
                    "name": "list_my_quotations",
                    "arguments": {
                        "salesperson_user_id": 7,
                        "state": ["sale", "done"],
                        "limit": 5,
                    },
                },
            )
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["state"] == ["sale", "done"]
        assert kwargs["limit"] == 5


class TestScheduleVisitDispatch:
    def test_basic_dispatch(self, client):
        fake_result = {
            "success": True,
            "activity_id": 999,
            "partner_name": "Cliente VIP",
        }
        with patch(
            "mcp_odoo.tools.sales.odoo_schedule_visit",
            return_value=fake_result,
        ) as mock_fn:
            body = _rpc(
                client,
                "tools/call",
                params={
                    "name": "schedule_visit",
                    "arguments": {
                        "partner_id": 42,
                        "summary": "Visita comercial",
                        "date_deadline": "2026-05-10",
                        "salesperson_user_id": 7,
                        "note": "Llevar muestras",
                    },
                },
            )

        assert "isError" not in body["result"]
        text = body["result"]["content"][0]["text"]
        assert '"activity_id": 999' in text

        kwargs = mock_fn.call_args.kwargs
        assert kwargs == {
            "partner_id": 42,
            "summary": "Visita comercial",
            "date_deadline": "2026-05-10",
            "salesperson_user_id": 7,
            "note": "Llevar muestras",
        }

    def test_note_optional(self, client):
        with patch(
            "mcp_odoo.tools.sales.odoo_schedule_visit",
            return_value={"success": True, "activity_id": 1, "partner_name": "X"},
        ) as mock_fn:
            _rpc(
                client,
                "tools/call",
                params={
                    "name": "schedule_visit",
                    "arguments": {
                        "partner_id": 42,
                        "summary": "Visita",
                        "date_deadline": "2026-05-10",
                        "salesperson_user_id": 7,
                    },
                },
            )
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["note"] is None
