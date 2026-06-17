"""Tests for the in-stock-by-category merge in ``_rag_search``.

Bug (production, verified with real data): the semantic candidate pool
is capped at ``candidate_k`` (~100) by cosine similarity. Real catalogs
have thousands of items per category, so the few SKUs that actually have
stock can rank far beyond the pool. The downstream "in-stock first"
re-rank then never sees them and the bot wrongly reports "ninguno con
stock" even though stock exists.

Fix: after building the semantic pool + live data, we read the numeric
``categ_id`` of the pooled items and pull the in-stock items of those
categories straight from Odoo, fold them into the pool, and let the
existing re-rank float them to the front (we do NOT drop out-of-stock
items — proformas for agotados are still valid).

These tests mock Odoo (``odoo_search``), the live fetch
(``_fetch_products_live``), and httpx exactly like the existing
``test_search_products_default_code`` suite, so no network is touched.
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
def _settings_keys(monkeypatch):
    """``settings`` is instantiated at import time, before the env-var
    fixture runs, so the service key would be empty and ``_rag_search``
    would short-circuit with "SUPABASE_SERVICE_KEY not configured".
    Patch the live singleton so the semantic path runs."""
    from mcp_odoo.config import settings

    monkeypatch.setattr(settings, "supabase_service_key", "test-service-key", raising=False)
    monkeypatch.setattr(settings, "supabase_url", "http://fake-supabase:8000", raising=False)


@pytest.fixture(autouse=True)
def _clear_caches():
    from mcp_odoo.transports import mcp_transport as t

    t._query_cache.clear()
    t._product_cache.clear()
    yield
    t._query_cache.clear()
    t._product_cache.clear()


def _decode(envelope_str: str) -> dict[str, Any]:
    return json.loads(envelope_str)


def _semantic_client_factory(pool_ids: list[int]):
    """httpx.AsyncClient stand-in that returns ``pool_ids`` from the
    ``search_tenant_products`` RPC (and a non-empty embedding) so the
    semantic path produces a pool without hitting the ILIKE fallback."""

    class _EmbedResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[0.1, 0.2, 0.3]]}

    class _RpcResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{"odoo_id": pid} for pid in pool_ids]

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **kw):
            if "embed" in url:
                return _EmbedResp()
            return _RpcResp()

        async def get(self, *a, **kw):
            class _Empty:
                status_code = 200

                def json(self):
                    return []

            return _Empty()

    return _FakeClient


def _live(pid: int, name: str, qty: int, category: str) -> dict:
    return {
        "id": pid,
        "name": name,
        "code": f"SKU{pid}",
        "price": 100.0,
        "cost": 50.0,
        "qty": qty,
        "virtual": qty,
        "uom": "Units",
        "category": category,
        "description": "",
        "barcode": "",
        "active": True,
    }


class TestInStockCategoryMerge:

    @pytest.mark.asyncio
    async def test_instock_item_merged_and_ranked_first(self):
        """Semantic pool has only out-of-stock notebooks; Odoo's
        in-stock-by-category query returns an extra notebook WITH stock
        that wasn't in the pool. The final result must include it AND
        rank it ahead of the agotados."""
        from mcp_odoo.transports import mcp_transport as t

        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake", "user": "u", "password": "p",
        }

        # Semantic pool: two notebooks, BOTH out of stock.
        pool_ids = [100, 101]
        # The in-stock notebook that ranks beyond the pool.
        instock_id = 999

        live_map = {
            100: _live(100, "NOTEBOOK HP agotado A", qty=0, category="Notebooks"),
            101: _live(101, "NOTEBOOK DELL agotado B", qty=0, category="Notebooks"),
            999: _live(999, "NOTEBOOK LENOVO con stock", qty=5, category="Notebooks"),
        }

        async def _fake_live(tenant_id, ids):
            return {pid: live_map[pid] for pid in ids if pid in live_map}

        # odoo_search is called twice inside the merge block:
        #   1) read categ_id for the pooled ids        → return categ rows
        #   2) in-stock-by-category search             → return instock id
        def _fake_search(*args, **kwargs):
            # Signature: (tenant_id, url, db, user, password, model, domain, ...)
            domain = args[6]
            # The in-stock query has a qty_available clause; the categ_id
            # read does not.
            is_instock_query = any(
                isinstance(c, list) and c and c[0] == "qty_available"
                for c in domain
            )
            if is_instock_query:
                return [{"id": instock_id}]
            # categ_id read for pooled ids → both in category 7
            return [
                {"id": 100, "categ_id": [7, "Notebooks"]},
                {"id": 101, "categ_id": [7, "Notebooks"]},
            ]

        fake_client = _semantic_client_factory(pool_ids)

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
            patch.object(t, "_fetch_products_live", AsyncMock(side_effect=_fake_live)),
            patch.object(t, "_apply_pricelist_to_live", AsyncMock(return_value=None)),
            patch("mcp_odoo.tools.generic.odoo_search", side_effect=_fake_search),
            patch("httpx.AsyncClient", fake_client),
        ):
            result = await t._rag_search(
                query="laptop", top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        env = _decode(result)
        codes = [r["code"] for r in env["rows"]]
        template_ids = [r["template_id"] for r in env["rows"]]

        # The in-stock item was merged in.
        assert instock_id in template_ids, (
            f"merged in-stock item {instock_id} missing from {template_ids}"
        )
        # And it ranks FIRST (in_stock floated ahead of the agotados).
        assert template_ids[0] == instock_id, (
            f"expected in-stock item first, got order {template_ids}"
        )
        # Out-of-stock items are still present (we don't drop agotados).
        assert 100 in template_ids and 101 in template_ids
        assert codes  # sanity

    @pytest.mark.asyncio
    async def test_merge_narrows_to_intent_relevant_category(self):
        """Prod bug (2026-06-17): the pool spans an intent-relevant
        category (Laptops) and an unrelated stock-heavy one (REPUESTOS).
        Without intent narrowing the flat ``limit 80`` filled with
        REPUESTOS parts and the in-stock laptop never made it in, then
        the kind filter dropped the parts → "ninguno con stock".

        With the fix, the merge must:
          1. query in-stock ONLY for the Laptops category id,
          2. so the in-stock laptop is merged + ranked first,
          3. and the (irrelevant) REPUESTOS category is never queried."""
        from mcp_odoo.transports import mcp_transport as t

        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake", "user": "u", "password": "p",
        }

        # Pool: 1 agotado laptop (cat 19 Laptops) + 1 in-stock repuesto
        # (cat 76 REPUESTOS). Both are in the semantic pool for "laptop".
        LAPTOP_CAT = 19
        REPUESTOS_CAT = 76
        pool_ids = [100, 200]
        # The in-stock laptop that ranks beyond the pool (the one the
        # customer actually wants).
        instock_laptop = 17883
        # An in-stock repuesto Odoo WOULD return if we queried REPUESTOS.
        instock_repuesto = 50000

        live_map = {
            100: _live(100, "LAPTOP HP agotada", qty=0, category="PC / Laptops"),
            200: _live(200, "MEMORIA RAM", qty=15, category="PC / REPUESTOS"),
            17883: _live(
                17883, "LAPTOP LENOVO IDEAPAD", qty=4, category="PC / Laptops",
            ),
            50000: _live(
                50000, "DISIPADOR DE CALOR", qty=99, category="PC / REPUESTOS",
            ),
        }

        async def _fake_live(tenant_id, ids):
            return {pid: live_map[pid] for pid in ids if pid in live_map}

        instock_domains: list = []

        def _fake_search(*args, **kwargs):
            domain = args[6]
            is_instock_query = any(
                isinstance(c, list) and c and c[0] == "qty_available"
                for c in domain
            )
            if is_instock_query:
                instock_domains.append(domain)
                # Mimic Odoo honoring categ_id filter: only return rows
                # whose category is in the queried cat list.
                cat_clause = next(
                    (c for c in domain if isinstance(c, list) and c and c[0] == "categ_id"),
                    None,
                )
                queried = set(cat_clause[2]) if cat_clause else set()
                out = []
                if LAPTOP_CAT in queried:
                    out.append({"id": instock_laptop})
                if REPUESTOS_CAT in queried:
                    out.append({"id": instock_repuesto})
                return out
            # categ_id read: pool item 100 in Laptops, 200 in REPUESTOS.
            return [
                {"id": 100, "categ_id": [LAPTOP_CAT, "PC / Laptops"]},
                {"id": 200, "categ_id": [REPUESTOS_CAT, "PC / REPUESTOS"]},
            ]

        fake_client = _semantic_client_factory(pool_ids)

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
            patch.object(t, "_fetch_products_live", AsyncMock(side_effect=_fake_live)),
            patch.object(t, "_apply_pricelist_to_live", AsyncMock(return_value=None)),
            patch("mcp_odoo.tools.generic.odoo_search", side_effect=_fake_search),
            patch("httpx.AsyncClient", fake_client),
        ):
            result = await t._rag_search(
                query="laptop", top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        env = _decode(result)
        template_ids = [r["template_id"] for r in env["rows"]]

        # 1. The in-stock query targeted ONLY the Laptops category.
        assert instock_domains, "in-stock query never ran"
        cat_clause = next(
            (c for c in instock_domains[0]
             if isinstance(c, list) and c and c[0] == "categ_id"),
            None,
        )
        assert cat_clause is not None
        assert set(cat_clause[2]) == {LAPTOP_CAT}, (
            f"merge queried {cat_clause[2]} — should be only Laptops "
            f"({LAPTOP_CAT}), not REPUESTOS ({REPUESTOS_CAT})"
        )

        # 2. The in-stock laptop was merged in and ranks first.
        assert instock_laptop in template_ids, (
            f"in-stock laptop missing from {template_ids}"
        )
        assert template_ids[0] == instock_laptop, (
            f"expected in-stock laptop first, got {template_ids}"
        )

        # 3. The REPUESTOS in-stock item was NEVER merged (and the kind
        #    filter would have dropped it anyway).
        assert instock_repuesto not in template_ids
        # The agotado laptop survives; the pool repuesto (200) is dropped
        # by the kind filter, which is fine — point is the laptop has stock.
        assert 100 in template_ids

    @pytest.mark.asyncio
    async def test_merge_falls_back_to_all_cats_when_hints_miss(self):
        """If the intent hints match NO pool category name (vocab
        mismatch), the merge must NOT silently no-op: it falls back to
        querying all pool categories."""
        from mcp_odoo.transports import mcp_transport as t

        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake", "user": "u", "password": "p",
        }
        pool_ids = [100]
        instock_id = 777
        # Pool category name does NOT contain "laptop"/"notebook"/etc.,
        # so the laptop hints won't match → fallback to all cats.
        live_map = {
            100: _live(100, "EQUIPO agotado", qty=0, category="Genérico 1234"),
            777: _live(777, "EQUIPO con stock", qty=2, category="Genérico 1234"),
        }

        async def _fake_live(tenant_id, ids):
            return {pid: live_map[pid] for pid in ids if pid in live_map}

        queried_cats: list = []

        def _fake_search(*args, **kwargs):
            domain = args[6]
            is_instock_query = any(
                isinstance(c, list) and c and c[0] == "qty_available"
                for c in domain
            )
            if is_instock_query:
                cat_clause = next(
                    (c for c in domain if isinstance(c, list) and c and c[0] == "categ_id"),
                    None,
                )
                if cat_clause:
                    queried_cats.append(set(cat_clause[2]))
                return [{"id": instock_id}]
            return [{"id": 100, "categ_id": [55, "Genérico 1234"]}]

        fake_client = _semantic_client_factory(pool_ids)

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
            patch.object(t, "_fetch_products_live", AsyncMock(side_effect=_fake_live)),
            patch.object(t, "_apply_pricelist_to_live", AsyncMock(return_value=None)),
            patch("mcp_odoo.tools.generic.odoo_search", side_effect=_fake_search),
            patch("httpx.AsyncClient", fake_client),
        ):
            result = await t._rag_search(
                query="laptop", top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        env = _decode(result)
        template_ids = [r["template_id"] for r in env["rows"]]
        # Fallback queried the (only) pool category despite the hint miss.
        assert queried_cats and queried_cats[0] == {55}
        # And the in-stock item was merged.
        assert instock_id in template_ids

    @pytest.mark.asyncio
    async def test_no_dupe_when_instock_already_pooled(self):
        """If the in-stock-by-category query returns an id that's already
        in the semantic pool, it must NOT be duplicated."""
        from mcp_odoo.transports import mcp_transport as t

        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake", "user": "u", "password": "p",
        }
        pool_ids = [100, 101]
        live_map = {
            100: _live(100, "NOTEBOOK con stock", qty=3, category="Notebooks"),
            101: _live(101, "NOTEBOOK agotado", qty=0, category="Notebooks"),
        }

        async def _fake_live(tenant_id, ids):
            return {pid: live_map[pid] for pid in ids if pid in live_map}

        def _fake_search(*args, **kwargs):
            domain = args[6]
            is_instock_query = any(
                isinstance(c, list) and c and c[0] == "qty_available"
                for c in domain
            )
            if is_instock_query:
                # Verify the merge excludes already-pooled ids: the domain
                # should carry a "not in" clause with the pooled ids.
                notin = next(
                    (c for c in domain if isinstance(c, list) and c and c[0] == "id"),
                    None,
                )
                assert notin is not None and notin[1] == "not in"
                assert set(notin[2]) == {100, 101}
                # Return an id that IS already pooled (defensive double-check).
                return [{"id": 100}]
            return [
                {"id": 100, "categ_id": [7, "Notebooks"]},
                {"id": 101, "categ_id": [7, "Notebooks"]},
            ]

        fake_client = _semantic_client_factory(pool_ids)

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
            patch.object(t, "_fetch_products_live", AsyncMock(side_effect=_fake_live)),
            patch.object(t, "_apply_pricelist_to_live", AsyncMock(return_value=None)),
            patch("mcp_odoo.tools.generic.odoo_search", side_effect=_fake_search),
            patch("httpx.AsyncClient", fake_client),
        ):
            result = await t._rag_search(
                query="laptop", top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        env = _decode(result)
        template_ids = [r["template_id"] for r in env["rows"]]
        # No duplicate of id 100.
        assert template_ids.count(100) == 1
        assert set(template_ids) == {100, 101}

    @pytest.mark.asyncio
    async def test_merge_failure_is_non_fatal(self):
        """If the merge's Odoo read raises, the search must still return
        the semantic pool unchanged (defensive try/except)."""
        from mcp_odoo.transports import mcp_transport as t

        mock_tc = {
            "tenant_id": "tenant-uuid",
            "url": "http://fake:8069",
            "db": "fake", "user": "u", "password": "p",
        }
        pool_ids = [100]
        live_map = {100: _live(100, "NOTEBOOK con stock", qty=2, category="Notebooks")}

        async def _fake_live(tenant_id, ids):
            return {pid: live_map[pid] for pid in ids if pid in live_map}

        def _boom(*args, **kwargs):
            raise RuntimeError("odoo down")

        fake_client = _semantic_client_factory(pool_ids)

        with (
            patch.object(
                t, "_get_tenant_config_by_id",
                AsyncMock(return_value=mock_tc),
            ),
            patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
            patch.object(t, "_fetch_products_live", AsyncMock(side_effect=_fake_live)),
            patch.object(t, "_apply_pricelist_to_live", AsyncMock(return_value=None)),
            patch("mcp_odoo.tools.generic.odoo_search", side_effect=_boom),
            patch("httpx.AsyncClient", fake_client),
        ):
            result = await t._rag_search(
                query="laptop", top_k=10, offset=0, tenant_id="tenant-uuid",
            )

        env = _decode(result)
        template_ids = [r["template_id"] for r in env["rows"]]
        # Semantic pool preserved despite the merge blowing up.
        assert template_ids == [100]
