"""C1 — Cross-partner quotation pre-check (MCP server side).

The orchestrator forwards ``X-Expected-Partner-Id`` for B2C sessions.
``mcp_odoo.transports.mcp_transport._execute_tool`` reads it BEFORE
dispatching a quotation-touching tool and rejects with
``cross_partner_quotation`` when the order's ``partner_id`` does not
match the expected one.

Production incidents (2026-05-12, Qwen3 catalog §1):

* Mario T68: ``find_quotation_by_name('S00002')`` returned a $5.00
  quotation that belonged to an unknown partner.
* Nataly T62: ``get_quotation`` exposed VENTA122586 under another
  customer's name.

The orchestrator already runs a post-tool envelope filter (defence in
depth), but the MCP-side pre-check stops the data from leaving Odoo
in the first place.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — build a minimal fake Request with custom headers + tenant config.
# ---------------------------------------------------------------------------


def _make_request(*, expected_partner: str | None) -> MagicMock:
    """Fabricate a Starlette-like Request with headers().

    Only ``request.headers.get(name, default)`` is exercised by the
    transport, so a dict-like MagicMock is enough.
    """
    headers: dict[str, str] = {}
    if expected_partner is not None:
        headers["x-expected-partner-id"] = expected_partner

    class _Headers:
        def get(self, name, default=""):
            return headers.get(name.lower(), default)

    req = MagicMock()
    req.headers = _Headers()
    return req


_TENANT_CFG = {
    "tenant_id": "tenant-001",
    "url": "http://odoo.example",
    "db": "demo",
    "user": "admin",
    "password": "pw",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCrossPartnerQuotationBlock:
    """Pre-check rejects when the order's partner != expected_partner_id."""

    @pytest.mark.asyncio
    async def test_get_quotation_blocked_when_cross_partner(self):
        from mcp_odoo.transports import mcp_transport as t

        # Stub the tenant config resolution.
        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.generic.odoo_read"
        ) as mock_read, patch(
            "mcp_odoo.tools.generic.odoo_search"
        ) as mock_search:
            # Pre-check reads partner_id of sale.order — returns partner 99.
            mock_read.return_value = [
                {"id": 12345, "partner_id": [99, "Other Customer"]}
            ]
            mock_search.return_value = []

            req = _make_request(expected_partner="42")
            result_json = await t._execute_tool(
                req, "get_quotation", {"order_id": 12345},
            )

        data = json.loads(result_json)
        assert data["success"] is False
        assert data["error_code"] == "cross_partner_quotation"
        assert data["expected_partner_id"] == 42

    @pytest.mark.asyncio
    async def test_get_quotation_passes_when_same_partner(self):
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.generic.odoo_read"
        ) as mock_read, patch(
            "mcp_odoo.tools.sales.odoo_get_quotation"
        ) as mock_get_quotation:
            mock_read.return_value = [
                {"id": 12345, "partner_id": [42, "Test Customer"]}
            ]
            mock_get_quotation.return_value = {
                "success": True, "order_id": 12345, "partner_id": 42,
            }

            req = _make_request(expected_partner="42")
            result_json = await t._execute_tool(
                req, "get_quotation", {"order_id": 12345},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        # Sanity — the dispatch reached the real tool.
        mock_get_quotation.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_quotation_passes_when_header_absent(self):
        """B2B sessions / unidentified B2C sessions skip the pre-check.

        Without ``X-Expected-Partner-Id`` we MUST NOT add any new gate
        — the orchestrator already does its own scoping for sellers,
        and unidentified B2C users have no pinned partner yet.
        """
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.sales.odoo_get_quotation"
        ) as mock_get_quotation:
            mock_get_quotation.return_value = {
                "success": True, "order_id": 12345, "partner_id": 99,
            }

            req = _make_request(expected_partner=None)
            result_json = await t._execute_tool(
                req, "get_quotation", {"order_id": 12345},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        mock_get_quotation.assert_called_once()


class TestCrossPartnerListQuotations:
    """list_quotations / get_latest_quotation / get_active_quotation
    accept ``partner_id`` directly. The LLM passing the WRONG id (or none)
    is no longer rejected — the C1b partner-scope override (2026-06-17)
    rewrites ``partner_id`` to the pinned ``expected_partner_id`` so the
    tool always runs against the identified customer's own data.
    """

    @pytest.mark.asyncio
    async def test_list_quotations_overrides_wrong_arg_partner(self):
        """LLM passes the customer's CÉDULA (wrong partner_id) → override
        rewrites it to the pinned partner and the tool RUNS with id 42.
        """
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.sales.odoo_list_quotations"
        ) as mock_list:
            mock_list.return_value = {"success": True, "orders": []}

            req = _make_request(expected_partner="42")
            # 1500501968 = the customer's cédula the LLM fumbled in as id.
            result_json = await t._execute_tool(
                req, "list_quotations", {"partner_id": "1500501968"},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        # Tool actually executed, and with the authoritative partner_id.
        mock_list.assert_called_once()
        # odoo_list_quotations(*creds, partner_id, limit, states) — the
        # partner_id is the first positional after the 5 creds.
        called_partner = mock_list.call_args.args[5]
        assert called_partner == 42

    @pytest.mark.asyncio
    async def test_list_quotations_overrides_absent_arg_partner(self):
        """LLM omits partner_id entirely → override injects the pinned id."""
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.sales.odoo_list_quotations"
        ) as mock_list:
            mock_list.return_value = {"success": True, "orders": []}

            req = _make_request(expected_partner="62")
            result_json = await t._execute_tool(
                req, "list_quotations", {},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        mock_list.assert_called_once()
        assert mock_list.call_args.args[5] == 62

    @pytest.mark.asyncio
    async def test_get_latest_quotation_overrides_wrong_arg_partner(self):
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.sales.get_latest_quotation"
        ) as mock_latest:
            mock_latest.return_value = {"success": True, "order_id": None}

            req = _make_request(expected_partner="62")
            result_json = await t._execute_tool(
                req, "get_latest_quotation", {"partner_id": "1500501968"},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        mock_latest.assert_called_once()
        # get_latest_quotation(*creds, partner_id=..., states=...) — kwarg.
        assert mock_latest.call_args.kwargs["partner_id"] == 62

    @pytest.mark.asyncio
    async def test_get_latest_quotation_overrides_absent_arg_partner(self):
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.sales.get_latest_quotation"
        ) as mock_latest:
            mock_latest.return_value = {"success": True, "order_id": None}

            req = _make_request(expected_partner="62")
            result_json = await t._execute_tool(
                req, "get_latest_quotation", {},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        mock_latest.assert_called_once()
        assert mock_latest.call_args.kwargs["partner_id"] == 62

    @pytest.mark.asyncio
    async def test_partner_scope_not_overridden_when_header_absent(self):
        """B2B sellers / unidentified B2C: no header → no override.

        The LLM-supplied partner_id is passed through untouched, so a
        seller can still list any customer's quotations.
        """
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.sales.odoo_list_quotations"
        ) as mock_list:
            mock_list.return_value = {"success": True, "orders": []}

            req = _make_request(expected_partner=None)
            result_json = await t._execute_tool(
                req, "list_quotations", {"partner_id": 99},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        mock_list.assert_called_once()
        # Untouched — seller's chosen partner is honoured.
        assert mock_list.call_args.args[5] == 99

    @pytest.mark.asyncio
    async def test_list_quotations_passes_when_same_partner(self):
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.sales.odoo_list_quotations"
        ) as mock_list:
            mock_list.return_value = {"success": True, "orders": []}

            req = _make_request(expected_partner="42")
            result_json = await t._execute_tool(
                req, "list_quotations", {"partner_id": 42},
            )

        data = json.loads(result_json)
        assert data["success"] is True
        mock_list.assert_called_once()


class TestCrossPartnerFindByName:
    """``find_quotation_by_name`` takes ``name`` — the pre-check resolves
    the partner_id from sale.order via name lookup.
    """

    @pytest.mark.asyncio
    async def test_find_by_name_blocked_when_cross_partner(self):
        from mcp_odoo.transports import mcp_transport as t

        with patch.object(
            t, "_get_tenant_config", new=AsyncMock(return_value=_TENANT_CFG)
        ), patch(
            "mcp_odoo.tools.generic.odoo_read"
        ) as mock_read, patch(
            "mcp_odoo.tools.generic.odoo_search"
        ) as mock_search, patch(
            "mcp_odoo.tools.sales.odoo_find_quotation_by_name"
        ) as mock_find:
            # No order_id in args → pre-check uses odoo_search by name.
            mock_read.return_value = []  # not used
            mock_search.return_value = [
                {"partner_id": [99, "Other Customer"]}
            ]

            req = _make_request(expected_partner="42")
            result_json = await t._execute_tool(
                req, "find_quotation_by_name", {"name": "VENTA122586"},
            )

        data = json.loads(result_json)
        assert data["success"] is False
        assert data["error_code"] == "cross_partner_quotation"
        mock_find.assert_not_called()
