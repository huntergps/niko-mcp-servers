"""Tests for defensive type coercion of LLM-supplied tool args.

Root-cause fix (2026-06-17): Llama-3.3-70B / the MCP arg-passing path
frequently emit numeric / array / boolean arguments as STRINGS, while the
tool functions expect the schema types declared in ``MCP_TOOLS``. The
``_coerce_args_by_schema`` helper normalises args at the transport boundary
BEFORE schema validation and BEFORE the C1b partner-scope override.

Two layers of testing:
  1. Unit tests on the pure ``_coerce_args_by_schema`` function.
  2. End-to-end ``tools/call`` dispatch tests confirming coerced values
     reach the tool function, AND that the C1b override still wins.
"""

from __future__ import annotations

import os
from unittest.mock import patch

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
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post("/mcp", json=payload, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


# ─────────────────────────────────────────────────────────────────────
# Unit tests on the pure coercion function
# ─────────────────────────────────────────────────────────────────────


class TestCoerceArgsBySchemaUnit:
    def _coerce(self, tool_name, args):
        from mcp_odoo.transports.mcp_transport import _coerce_args_by_schema
        return _coerce_args_by_schema(tool_name, args)

    def test_integer_string_to_int(self):
        # list_quotations: partner_id + limit are both "integer" in schema.
        args = {"partner_id": "62", "limit": "10"}
        out = self._coerce("list_quotations", args)
        assert out["partner_id"] == 62 and isinstance(out["partner_id"], int)
        assert out["limit"] == 10 and isinstance(out["limit"], int)

    def test_array_json_string_to_list(self):
        # states is an "array" in list_quotations' schema.
        args = {"partner_id": 62, "states": '["draft", "sent"]'}
        out = self._coerce("list_quotations", args)
        assert out["states"] == ["draft", "sent"]

    def test_get_latest_quotation_states_json_string(self):
        # The exact prod incident: states arrived as a JSON string.
        args = {"partner_id": "1500501968", "states": '["draft", "sent"]'}
        out = self._coerce("get_latest_quotation", args)
        assert out["partner_id"] == 1500501968
        assert out["states"] == ["draft", "sent"]

    def test_array_comma_fallback(self):
        # Non-JSON but comma-separated string → simple split.
        args = {"partner_id": 62, "states": "draft, sent"}
        out = self._coerce("list_quotations", args)
        assert out["states"] == ["draft", "sent"]

    def test_boolean_string_true(self):
        # get_partner_profile.include_activity is "boolean".
        args = {"partner_id": 62, "include_activity": "true"}
        out = self._coerce("get_partner_profile", args)
        assert out["include_activity"] is True

    def test_boolean_string_false_and_zero(self):
        out = self._coerce("get_partner_profile",
                           {"partner_id": 62, "include_activity": "false"})
        assert out["include_activity"] is False
        out2 = self._coerce("get_partner_profile",
                            {"partner_id": 62, "include_activity": "0"})
        assert out2["include_activity"] is False

    def test_number_string_to_float(self):
        # search_products.price_max is "number".
        args = {"query": "laptop", "price_max": "500"}
        out = self._coerce("search_products", args)
        assert out["price_max"] == 500.0 and isinstance(out["price_max"], float)

    def test_non_convertible_kept_without_raising(self):
        # partner_id is integer-typed but value is non-numeric garbage.
        args = {"partner_id": "abc", "limit": "not-a-number"}
        out = self._coerce("list_quotations", args)
        assert out["partner_id"] == "abc"  # untouched, no raise
        assert out["limit"] == "not-a-number"

    def test_already_correct_types_untouched(self):
        args = {"partner_id": 62, "limit": 10, "states": ["draft"]}
        out = self._coerce("list_quotations", args)
        assert out == {"partner_id": 62, "limit": 10, "states": ["draft"]}

    def test_arg_not_in_schema_untouched(self):
        args = {"partner_id": "62", "bogus_field": "123"}
        out = self._coerce("list_quotations", args)
        assert out["partner_id"] == 62
        assert out["bogus_field"] == "123"  # left as string

    def test_unknown_tool_returns_args_unchanged(self):
        args = {"partner_id": "62"}
        out = self._coerce("__no_such_tool__", args)
        assert out == {"partner_id": "62"}

    def test_string_field_left_as_is(self):
        # find_quotation_by_name.name is "string"; an all-digit string must
        # NOT be coerced to int.
        args = {"name": "122173"}
        out = self._coerce("find_quotation_by_name", args)
        assert out["name"] == "122173"

    def test_bool_value_for_integer_field_not_clobbered(self):
        # A genuine bool passed to an integer field stays a bool (we skip it
        # because bool is an int subclass; coercion would be lossy).
        args = {"partner_id": True}
        out = self._coerce("list_quotations", args)
        assert out["partner_id"] is True


# ─────────────────────────────────────────────────────────────────────
# End-to-end dispatch: coerced args reach the tool function
# ─────────────────────────────────────────────────────────────────────


class TestCoercionEndToEnd:
    def test_list_quotations_string_args_coerced_to_int(self, client):
        """partner_id="62", limit="10" must reach odoo_list_quotations as
        ints (and states JSON string as a list)."""
        with patch(
            "mcp_odoo.tools.sales.odoo_list_quotations",
            return_value={"success": True, "orders": []},
        ) as mock_fn:
            body = _rpc(
                client,
                "tools/call",
                params={
                    "name": "list_quotations",
                    "arguments": {
                        "partner_id": "62",
                        "limit": "10",
                        "states": '["draft", "sent"]',
                    },
                },
            )
        assert "isError" not in body["result"], body["result"]
        # odoo_list_quotations(*creds, partner_id, limit, states)
        called_args, _ = mock_fn.call_args
        # creds are the first 5 positional args.
        assert called_args[5] == 62 and isinstance(called_args[5], int)
        assert called_args[6] == 10 and isinstance(called_args[6], int)
        assert called_args[7] == ["draft", "sent"]

    def test_coercion_lets_validation_pass(self, client):
        """Without coercion, partner_id="62" would fail the required-field
        integer type check in _validate_args_against_schema. With coercion
        it passes and the tool runs."""
        with patch(
            "mcp_odoo.tools.sales.odoo_list_quotations",
            return_value={"success": True, "orders": []},
        ):
            body = _rpc(
                client,
                "tools/call",
                params={
                    "name": "list_quotations",
                    "arguments": {"partner_id": "62"},
                },
            )
        # The structured invalid_arguments error must NOT be returned.
        assert "isError" not in body["result"], body["result"]
        text = body["result"]["content"][0]["text"]
        assert "invalid_arguments" not in text

    def test_c1b_override_still_wins_after_coercion(self, client):
        """When x-expected-partner-id is set, the C1b override must still
        overwrite partner_id with the pinned id — even though the LLM's
        (now-coerced) value differs. Coercion runs first; override wins."""
        with patch(
            "mcp_odoo.tools.sales.odoo_list_quotations",
            return_value={"success": True, "orders": []},
        ) as mock_fn:
            body = _rpc(
                client,
                "tools/call",
                params={
                    "name": "list_quotations",
                    # LLM fumbled: passed the cédula (as string) instead of
                    # the real partner_id.
                    "arguments": {"partner_id": "1500501968"},
                },
                headers={"x-expected-partner-id": "62"},
            )
        assert "isError" not in body["result"], body["result"]
        called_args, _ = mock_fn.call_args
        # Override wins: 62, not the coerced 1500501968.
        assert called_args[5] == 62
