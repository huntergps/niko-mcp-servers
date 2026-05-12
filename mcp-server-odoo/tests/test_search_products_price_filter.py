"""Tests for the `price_min` / `price_max` filter in `search_products`.

Bug B3 (production, 2026-05-12): the bot asked the customer for a
budget ("máximo 500"), then listed 5 CPUs of which 3 cost more than
$500. Root cause: MCP `search_products` had no price filter and the
LLM had no rule to clamp results, so it presented the raw ranked list.

The fix wires `price_min` / `price_max` through the MCP envelope. The
ranked list is filtered AT PRESENTATION TIME, so the cache key stays
``(tenant, query)`` and the same ranked list serves any budget request
issued in the same 60-second window.

These tests verify:

  1. ``price_max`` removes rows priced over the limit.
  2. ``price_min`` removes rows priced under the limit.
  3. Combined min+max behaves as an inclusive range.
  4. When the filter eliminates every candidate, the header tells the
     LLM explicitly (no silent empty result that could re-trigger the
     bug).
  5. The cache is shared across budget variants: two calls with
     different ``price_max`` against the same query hit the cache and
     return distinct slices without re-running the RAG.
  6. Rows without live data (``_live is None``) are dropped under any
     active filter — we cannot certify they fit the budget.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret-32bytes-padding!!")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


@pytest.fixture(autouse=True)
def _clear_caches():
    from mcp_odoo.transports import mcp_transport as t

    t._query_cache.clear()
    t._product_cache.clear()
    yield
    t._query_cache.clear()
    t._product_cache.clear()


def _make_row(odoo_id: int, code: str, name: str, price: float, qty: int = 5) -> dict:
    """Build a ranked-row stub matching the shape ``_rag_search`` stashes
    onto each entry before caching it (``_live`` already populated)."""
    return {
        "odoo_id": odoo_id,
        "name": name,
        "code": code,
        "_live": {
            "id": odoo_id,
            "name": name,
            "code": code,
            "price": price,
            "cost": price * 0.7,
            "qty": qty,
            "virtual": qty,
            "uom": "Units",
            "category": "CPU",
            "description": "",
            "barcode": "",
            "active": True,
        },
    }


# Five rows mimicking the B3 trace: 3 over budget, 2 under.
B3_CPUS = [
    _make_row(101, "CPU0001", "AMD Ryzen 5 5600",  269.00),
    _make_row(102, "CPU0002", "AMD Ryzen 7 5800X", 444.00),
    _make_row(103, "CPU0003", "AMD Ryzen 7 7700X", 620.00),
    _make_row(104, "CPU0004", "AMD Ryzen 9 7900X", 672.00),
    _make_row(105, "CPU0005", "AMD Ryzen 9 7950X", 798.00),
]


def _decode(envelope_str: str) -> dict[str, Any]:
    return json.loads(envelope_str)


# ---------------------------------------------------------------------------
# Direct unit tests on _format_ranked_page — pure synchronous, no IO.
# ---------------------------------------------------------------------------


class TestFormatRankedPagePriceFilter:

    def test_price_max_drops_over_budget(self):
        from mcp_odoo.transports.mcp_transport import _format_ranked_page

        result = _format_ranked_page(
            list(B3_CPUS), top_k=10, offset=0,
            price_min=None, price_max=500.0,
        )
        env = _decode(result)

        codes = [r["code"] for r in env["rows"]]
        prices = [r["price"] for r in env["rows"]]
        assert codes == ["CPU0001", "CPU0002"]
        assert all(p <= 500.0 for p in prices), prices
        assert env["filter_applied"] == {"price_min": None, "price_max": 500.0}

    def test_price_min_drops_under_budget(self):
        from mcp_odoo.transports.mcp_transport import _format_ranked_page

        result = _format_ranked_page(
            list(B3_CPUS), top_k=10, offset=0,
            price_min=500.0, price_max=None,
        )
        env = _decode(result)
        codes = [r["code"] for r in env["rows"]]
        assert codes == ["CPU0003", "CPU0004", "CPU0005"]

    def test_price_range_inclusive(self):
        from mcp_odoo.transports.mcp_transport import _format_ranked_page

        result = _format_ranked_page(
            list(B3_CPUS), top_k=10, offset=0,
            price_min=400.0, price_max=700.0,
        )
        env = _decode(result)
        codes = [r["code"] for r in env["rows"]]
        # 444, 620, 672 fit; 269 and 798 drop.
        assert codes == ["CPU0002", "CPU0003", "CPU0004"]

    def test_no_filter_keeps_all(self):
        from mcp_odoo.transports.mcp_transport import _format_ranked_page

        result = _format_ranked_page(list(B3_CPUS), top_k=10, offset=0)
        env = _decode(result)
        assert len(env["rows"]) == 5
        assert env.get("filter_applied") is None

    def test_filter_empties_result_header_is_explicit(self):
        from mcp_odoo.transports.mcp_transport import _format_ranked_page

        # Budget below every row.
        result = _format_ranked_page(
            list(B3_CPUS), top_k=10, offset=0,
            price_min=None, price_max=100.0,
        )
        env = _decode(result)
        assert env["rows"] == []
        # Header must NOT pretend everything's fine; it should say "0
        # productos en el presupuesto" so the LLM doesn't hallucinate.
        assert "0 productos en el presupuesto" in env["header"]
        # The budget itself should be echoed.
        assert "hasta USD 100" in env["header"]

    def test_unknown_price_dropped_under_filter(self):
        """Rows whose live data is None or whose price is missing must
        be dropped when a filter is active. Otherwise we'd reintroduce
        the very bug we're fixing (showing items we can't certify fit
        the budget)."""
        from mcp_odoo.transports.mcp_transport import _format_ranked_page

        rows = [
            _make_row(101, "CPU0001", "Ryzen 5 5600", 269.00),
            # No live data — simulates a tenant without Odoo wired or a
            # transient XML-RPC miss.
            {"odoo_id": 102, "name": "Ryzen unknown", "code": "CPU0002", "_live": None},
            # Live data present but price is None.
            {
                "odoo_id": 103, "name": "Ryzen 7 priced-null", "code": "CPU0003",
                "_live": {
                    "id": 103, "name": "Ryzen 7", "code": "CPU0003",
                    "price": None, "cost": 0, "qty": 1, "virtual": 1,
                    "uom": "", "category": "", "description": "", "barcode": "",
                    "active": True,
                },
            },
        ]

        result = _format_ranked_page(
            rows, top_k=10, offset=0,
            price_min=None, price_max=500.0,
        )
        env = _decode(result)
        codes = [r["code"] for r in env["rows"]]
        assert codes == ["CPU0001"]

    def test_filter_does_not_drop_unknown_when_inactive(self):
        """Unknown-price rows are kept when NO filter is active (regression
        check — the original behaviour for tenants without Odoo wired)."""
        from mcp_odoo.transports.mcp_transport import _format_ranked_page

        rows = [
            _make_row(101, "CPU0001", "Ryzen 5 5600", 269.00),
            {"odoo_id": 102, "name": "Ryzen unknown", "code": "CPU0002", "_live": None},
        ]
        result = _format_ranked_page(rows, top_k=10, offset=0)
        env = _decode(result)
        # Note: row 102 has no live data, so the formatter's fallback
        # block (price=0, qty=None) renders it as "consultar".
        codes = [r["code"] for r in env["rows"]]
        assert codes == ["CPU0001", "CPU0002"]


# ---------------------------------------------------------------------------
# End-to-end through _rag_search — verifies the param is plumbed through
# the cache-hit path AND that the cache is shared across budget variants.
# ---------------------------------------------------------------------------


class TestRagSearchPriceFilter:

    @pytest.mark.asyncio
    async def test_cache_hit_applies_price_max(self):
        """Pre-populate the cache, then call _rag_search with price_max.
        We never hit the network because the cache is warm."""
        from mcp_odoo.transports import mcp_transport as t

        tenant_id = "tenant-uuid"
        query = "ryzen amd"
        t._query_cache_set(tenant_id, query.lower(), list(B3_CPUS))

        result = await t._rag_search(
            query=query, top_k=10, offset=0,
            tenant_id=tenant_id, price_max=500.0,
        )
        env = _decode(result)
        codes = [r["code"] for r in env["rows"]]
        assert codes == ["CPU0001", "CPU0002"]

    @pytest.mark.asyncio
    async def test_cache_is_shared_across_budgets(self):
        """Two calls with different price_max against the same query
        must both hit the same cached ranked list (so the budget
        filter is purely presentation-side and does NOT bust the
        cache). We verify by warming the cache exactly once and then
        making sure two subsequent calls don't re-run RAG."""
        from mcp_odoo.transports import mcp_transport as t

        tenant_id = "tenant-uuid"
        query = "ryzen amd"
        t._query_cache_set(tenant_id, query.lower(), list(B3_CPUS))

        # Sentinel: anything that would have re-run the RAG would call
        # _get_tenant_slug. We assert it's NEVER awaited.
        slug_mock = AsyncMock(return_value="tecnosmart")

        with patch.object(t, "_get_tenant_slug", slug_mock):
            r1 = await t._rag_search(
                query=query, top_k=10, offset=0,
                tenant_id=tenant_id, price_max=500.0,
            )
            r2 = await t._rag_search(
                query=query, top_k=10, offset=0,
                tenant_id=tenant_id, price_max=700.0,
            )

        slug_mock.assert_not_awaited()  # cache served both requests
        codes1 = [r["code"] for r in _decode(r1)["rows"]]
        codes2 = [r["code"] for r in _decode(r2)["rows"]]
        assert codes1 == ["CPU0001", "CPU0002"]
        assert codes2 == ["CPU0001", "CPU0002", "CPU0003", "CPU0004"]
