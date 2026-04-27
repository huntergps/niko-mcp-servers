"""Tests for the exact-default_code shortcut in `search_products`.

Bug B (production): query="CAB0527" returned ACC0291 because BGE-M3
prioritised lexical similarity over exact code match. The fix adds a
deterministic shortcut at the top of `_rag_search` that, when the query
looks like a SKU, queries Odoo for `default_code =ilike <query>` first
and only falls back to semantic if no row matches.

These tests cover:
  1. Pattern detection (`_looks_like_default_code`) — the gate that
     decides "code or description?".
  2. End-to-end shortcut behaviour through `_rag_search` with mocked
     Odoo + httpx so we never hit the network.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Ensure config import works without real secrets."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret-32bytes-padding!!")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


@pytest.fixture(autouse=True)
def _clear_caches():
    """Drop the per-query cache between tests so cache-hits don't leak."""
    from mcp_odoo.transports import mcp_transport as t

    t._query_cache.clear()
    t._product_cache.clear()
    yield
    t._query_cache.clear()
    t._product_cache.clear()


# ---------------------------------------------------------------------------
# 1. Pattern detection
# ---------------------------------------------------------------------------


class TestLooksLikeDefaultCode:
    """The pattern is tenant-agnostic — must accept many SKU shapes and
    reject pure-text queries."""

    def test_alnum_with_digit_matches(self):
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("CAB0527") is True
        assert _looks_like_default_code("ACC0291") is True
        assert _looks_like_default_code("CPU0245") is True

    def test_lowercase_matches(self):
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("cab0527") is True

    def test_with_hyphen_or_underscore(self):
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("001-A1") is True
        assert _looks_like_default_code("PROD_2024_001") is True

    def test_pure_digits_match(self):
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("6300") is True

    def test_pure_letters_excluded(self):
        """A query without any digit must NOT trigger the shortcut, even
        if it's long. Otherwise "case", "laptop", "mouse" would each
        cost an extra Odoo round-trip."""
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("RAM") is False
        assert _looks_like_default_code("case") is False
        assert _looks_like_default_code("LAPTOP") is False
        assert _looks_like_default_code("MOUSE") is False

    def test_short_excluded(self):
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        # 3 chars — too short.
        assert _looks_like_default_code("A12") is False
        assert _looks_like_default_code("123") is False

    def test_with_spaces_excluded(self):
        """Free-text queries (multiple words) must go through semantic."""
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("cable patch cord") is False
        assert _looks_like_default_code("CAB 0527") is False

    def test_with_punctuation_excluded(self):
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("CAB.0527") is False
        assert _looks_like_default_code("CAB/0527") is False

    def test_empty_or_none(self):
        from mcp_odoo.transports.mcp_transport import _looks_like_default_code

        assert _looks_like_default_code("") is False
        assert _looks_like_default_code("   ") is False


# ---------------------------------------------------------------------------
# 2. End-to-end shortcut behaviour
# ---------------------------------------------------------------------------


def _decode(envelope_str: str) -> dict[str, Any]:
    """Parse the JSON envelope returned by `_format_ranked_page`."""
    return json.loads(envelope_str)


def _make_fake_client_factory():
    """Build a stand-in for ``httpx.AsyncClient`` that returns empty
    responses. Accepts any constructor kwargs (timeout=..., etc.)."""

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeResp()

        async def get(self, *a, **kw):
            return _FakeResp()

    return _FakeClient


class TestSearchProductsExactCode:

    @pytest.mark.asyncio
    async def test_exact_default_code_match(self):
        """query="CAB0527" with a matching product → top 1 is that product
        and the envelope has the canonical `header / rows / footer` shape."""
        from mcp_odoo.transports import mcp_transport as t

        # Pretend tenant has Odoo wired and product.template returns one row.
        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake",
            "user": "u",
            "password": "p",
        }
        odoo_row = {
            "id": 12345,
            "name": "CABLE PATCH CORD 3 METROS CAT 6 EVL",
            "default_code": "CAB0527",
            "list_price": 4.50,
            "qty_available": 7,
        }
        # Live data returned by `_fetch_products_live`.
        live_data = {
            12345: {
                "id": 12345,
                "name": "CABLE PATCH CORD 3 METROS CAT 6 EVL",
                "code": "CAB0527",
                "price": 4.50,
                "cost": 2.00,
                "qty": 7,
                "virtual": 7,
                "uom": "Units",
                "category": "Cables",
                "description": "",
                "barcode": "",
                "active": True,
            }
        }

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(
                t, "_fetch_products_live",
                AsyncMock(return_value=live_data),
            ),
            patch(
                "mcp_odoo.tools.generic.odoo_search",
                return_value=[odoo_row],
            ),
        ):
            result = await t._rag_search(
                query="CAB0527", top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        env = _decode(result)
        # Canonical envelope shape (so the resolver of Sprint 6D doesn't break).
        assert set(env.keys()) >= {"header", "rows", "footer"}
        assert isinstance(env["rows"], list)
        assert len(env["rows"]) == 1
        assert env["rows"][0]["template_id"] == 12345
        assert env["rows"][0]["code"] == "CAB0527"
        assert "CABLE PATCH CORD 3 METROS CAT 6 EVL" in env["rows"][0]["line_text"]

    @pytest.mark.asyncio
    async def test_exact_match_is_case_insensitive(self):
        """query="cab0527" (lowercase) → same hit as uppercase."""
        from mcp_odoo.transports import mcp_transport as t

        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake", "user": "u", "password": "p",
        }
        odoo_row = {
            "id": 12345, "name": "CABLE PATCH CORD",
            "default_code": "CAB0527", "list_price": 4.50, "qty_available": 7,
        }
        live_data = {12345: {
            "id": 12345, "name": "CABLE PATCH CORD", "code": "CAB0527",
            "price": 4.50, "cost": 0, "qty": 7, "virtual": 7,
            "uom": "", "category": "", "description": "", "barcode": "",
            "active": True,
        }}
        captured_domain: list = []

        def _fake_search(*args, **kwargs):
            # Capture the domain so we can assert on the operator.
            # Signature: (tenant_id, url, db, user, password, model, domain, ...)
            captured_domain.append(args[6])
            return [odoo_row]

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(
                t, "_fetch_products_live",
                AsyncMock(return_value=live_data),
            ),
            patch(
                "mcp_odoo.tools.generic.odoo_search",
                side_effect=_fake_search,
            ),
        ):
            result = await t._rag_search(
                query="cab0527", top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        env = _decode(result)
        assert env["rows"][0]["code"] == "CAB0527"
        # The domain must use `=ilike` (case-insensitive exact) — that's
        # the whole reason this works for "cab0527" lowercase input.
        assert captured_domain, "odoo_search was not called"
        domain = captured_domain[0]
        # Find the default_code clause
        code_clause = next(
            (c for c in domain if isinstance(c, list) and c and c[0] == "default_code"),
            None,
        )
        assert code_clause is not None, f"no default_code clause in {domain!r}"
        assert code_clause[1] == "=ilike"

    @pytest.mark.asyncio
    async def test_falls_back_to_semantic_when_no_exact_match(self):
        """SKU-shaped query but no Odoo match → falls through to semantic
        ranking. We assert by verifying that `_get_tenant_slug` (semantic
        path) gets called even though the shortcut ran first."""
        from mcp_odoo.transports import mcp_transport as t

        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake", "user": "u", "password": "p",
        }
        # The shortcut runs odoo_search, gets 0 rows, then falls back to
        # the semantic path. The semantic path calls _get_tenant_slug.
        slug_mock = AsyncMock(return_value="tecnosmart")
        fake_client = _make_fake_client_factory()

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(t, "_get_tenant_slug", slug_mock),
            patch(
                "mcp_odoo.tools.generic.odoo_search",
                return_value=[],   # no exact match
            ),
            patch("httpx.AsyncClient", fake_client),
        ):
            result = await t._rag_search(
                query="PROD_NO_EXISTE_42",
                top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        # Semantic path was reached (slug was looked up).
        slug_mock.assert_awaited()
        # And the semantic path's empty-result branch returned the
        # plain-text "no encontre" message.
        assert "No encontre" in result or "no encontre" in result.lower()

    @pytest.mark.asyncio
    async def test_pure_text_query_skips_shortcut(self):
        """query="cable patch cord" → does NOT match the SKU pattern, so
        Odoo's product.template should never be queried by the shortcut."""
        from mcp_odoo.transports import mcp_transport as t

        slug_mock = AsyncMock(return_value="tecnosmart")
        odoo_search_mock = AsyncMock()  # sentinel — must NOT be called
        fake_client = _make_fake_client_factory()

        with (
            patch.object(t, "_get_tenant_slug", slug_mock),
            patch(
                "mcp_odoo.tools.generic.odoo_search",
                odoo_search_mock,
            ),
            patch("httpx.AsyncClient", fake_client),
        ):
            await t._rag_search(
                query="cable patch cord",
                top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        # Pure-text queries skip the shortcut entirely → odoo_search
        # is never invoked from `_exact_code_lookup`.
        odoo_search_mock.assert_not_called()
        # ...but the semantic path DOES run, so the slug lookup happened.
        slug_mock.assert_awaited()

    @pytest.mark.asyncio
    async def test_pure_letter_query_skips_shortcut(self):
        """query="RAM" — looks alphanumeric but has no digit → skip
        shortcut to avoid useless Odoo round-trips for common words."""
        from mcp_odoo.transports import mcp_transport as t

        slug_mock = AsyncMock(return_value="tecnosmart")
        odoo_search_mock = AsyncMock()
        fake_client = _make_fake_client_factory()

        with (
            patch.object(t, "_get_tenant_slug", slug_mock),
            patch(
                "mcp_odoo.tools.generic.odoo_search",
                odoo_search_mock,
            ),
            patch("httpx.AsyncClient", fake_client),
        ):
            await t._rag_search(
                query="RAM",
                top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        odoo_search_mock.assert_not_called()
