"""Tests for product_code resolution in quotation MCP tools (v1.2 Fase 4).

Root cause being fixed: requiring `product_id: int` on the wire forced
the LLM to manage numeric template_ids, which is exactly where it
hallucinated IDs like 14791/14792 that never existed in Odoo. The fix
lets the tool accept `code` (the visible default_code, e.g. 'MON0026')
and resolve the template_id server-side via an exact, case-insensitive
match on `product.template.default_code`.

This module mocks every Odoo call at the `mcp_odoo.tools.generic.*`
boundary so the tests are hermetic — no XMLRPC, no env vars required.

Cases covered:
  1. code-only resolves the template_id and the quotation is created.
  2. code that doesn't exist returns `product_code_not_found`.
  3. code + product_id consistent → proceeds normally.
  4. code + product_id INCONSISTENT → `product_code_mismatch` (the
     guard that already existed must not regress).
  5. product_id only with no code → backward compat, still works.
  6. add_to_quotation with code-only → resolves & writes.
  7. add_to_quotation: ambiguous code returns `ambiguous_product_code`.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Avoid `config` import explosions when the module loads."""
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret-32bytes-padding!!")
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


_CREDS: tuple[str, str, str, str, str] = (
    "tenant-x", "http://fake-odoo:8069", "testdb", "admin", "admin",
)


# ─────────────────────────────────────────────────────────────────────
# Helpers — fake odoo_* implementations the tools call into.
# ─────────────────────────────────────────────────────────────────────


def _make_odoo_doubles(
    *,
    products: dict[str, dict] | None = None,
    ambiguous_codes: set[str] | None = None,
    partner: dict | None = None,
    existing_orders: dict[int, dict] | None = None,
):
    """Build a coherent set of fakes for odoo_search/read/create/write.

    ``products`` maps default_code -> {template_id, variant_id, uom_id, name}.
    """
    products = products or {}
    ambiguous_codes = ambiguous_codes or set()
    partner = partner or {"id": 99, "name": "ACME", "vat": "0999999999001"}
    existing_orders = existing_orders or {}

    # Indexes by template_id for variant resolution.
    by_template: dict[int, dict] = {}
    for code, meta in products.items():
        by_template[meta["template_id"]] = {**meta, "default_code": code}

    state: dict[str, Any] = {
        "created_orders": [],
        "created_lines": [],
        "writes": [],
        "next_order_id": 5000,
        "next_line_id": 9000,
        "existing_orders": dict(existing_orders),
    }

    def fake_search(tenant_id, url, db, user, password, model, domain,
                    fields=None, limit=80, offset=0, order=None):
        # product.template search by default_code
        if model == "product.template":
            # Look for [["default_code", "=", VALUE], ...]
            target_code = None
            for clause in domain:
                if isinstance(clause, list) and len(clause) == 3 and clause[0] == "default_code":
                    target_code = clause[2]
            if target_code is None:
                return []
            normalized = target_code.upper() if isinstance(target_code, str) else target_code
            if normalized in ambiguous_codes:
                # Two matches share the same default_code (data anomaly).
                hits = []
                for c, meta in products.items():
                    if c == normalized:
                        hits.append({
                            "id": meta["template_id"],
                            "default_code": c,
                            "name": meta.get("name", ""),
                        })
                # Synthesise a second hit with a different id.
                if len(hits) == 1:
                    hits.append({
                        "id": hits[0]["id"] + 1,
                        "default_code": normalized,
                        "name": "duplicate-shadow",
                    })
                return hits
            if normalized in products:
                meta = products[normalized]
                return [{
                    "id": meta["template_id"],
                    "default_code": normalized,
                    "name": meta.get("name", ""),
                }]
            return []
        # product.product (variant) lookup
        if model == "product.product":
            # extract template_ids in domain
            tids: list[int] = []
            for clause in domain:
                if isinstance(clause, list) and len(clause) == 3 and clause[0] == "product_tmpl_id":
                    val = clause[2]
                    if isinstance(val, list):
                        tids.extend(val)
                    else:
                        tids.append(val)
            out = []
            for tid in tids:
                meta = by_template.get(tid)
                if not meta:
                    continue
                out.append({
                    "id": meta["variant_id"],
                    "product_tmpl_id": [tid, meta.get("name", "")],
                    "uom_id": [meta.get("uom_id", 1), "Unit"],
                    "name": meta.get("name", ""),
                    "lst_price": meta.get("lst_price", 100.0),
                })
            return out
        if model == "sale.order.line":
            # Used by add_to_quotation to merge — return none for simplicity.
            return []
        if model == "sale.order":
            return []
        return []

    def fake_read(tenant_id, url, db, user, password, model, ids, fields=None):
        if model == "res.partner":
            return [partner] if partner.get("id") in ids else []
        if model == "product.template":
            out = []
            for tid in ids:
                meta = by_template.get(tid)
                if not meta:
                    continue
                out.append({
                    "id": tid,
                    "default_code": meta.get("default_code", ""),
                })
            return out
        if model == "sale.order":
            out = []
            for oid in ids:
                stored = state["existing_orders"].get(oid)
                if stored:
                    out.append(stored)
                else:
                    # Created-but-not-yet-stored order: synthesise a header.
                    out.append({
                        "id": oid,
                        "name": f"SO{oid:05d}",
                        "state": "draft",
                        "partner_id": [partner["id"], partner["name"]],
                        "amount_untaxed": 100.0,
                        "amount_tax": 12.0,
                        "amount_total": 112.0,
                        "order_line": [],
                        "date_order": "2025-01-01",
                        "share_link_so": "https://odoo/test",
                    })
            return out
        if model == "sale.order.line":
            # Lines created via fake_create are stored with full snapshot.
            out = []
            for lid in ids:
                for line in state["created_lines"]:
                    if line["id"] == lid:
                        out.append(line)
                        break
            return out
        return []

    def fake_create(tenant_id, url, db, user, password, model, values):
        if model == "sale.order":
            oid = state["next_order_id"]
            state["next_order_id"] += 1
            state["created_orders"].append({"id": oid, **values})
            # Also seed existing_orders so read-after-write succeeds.
            state["existing_orders"][oid] = {
                "id": oid,
                "name": f"SO{oid:05d}",
                "state": "draft",
                "partner_id": [partner["id"], partner["name"]],
                "amount_untaxed": 100.0,
                "amount_tax": 12.0,
                "amount_total": 112.0,
                "order_line": [],
                "date_order": "2025-01-01",
                "share_link_so": "https://odoo/test",
            }
            return oid
        if model == "sale.order.line":
            lid = state["next_line_id"]
            state["next_line_id"] += 1
            line = {
                "id": lid,
                "product_id": [values.get("product_id"), "fake"],
                "product_uom_qty": values.get("product_uom_qty", 1),
                "price_unit": values.get("price_unit", 0),
                "price_subtotal": 0,
                "price_tax": 0,
                "price_total": 0,
                "discount": values.get("discount", 0),
                "name": values.get("name", ""),
            }
            state["created_lines"].append(line)
            return lid
        return 1

    def fake_write(tenant_id, url, db, user, password, model, ids, values):
        state["writes"].append({"model": model, "ids": ids, "values": values})
        return True

    return state, fake_search, fake_read, fake_create, fake_write


# ─────────────────────────────────────────────────────────────────────
# create_quotation
# ─────────────────────────────────────────────────────────────────────


class TestCreateQuotationWithCode:

    def test_code_only_resolves_template_id(self):
        """Passing only `code` should resolve the template_id and write."""
        from mcp_odoo.tools.sales import odoo_create_quotation

        state, f_search, f_read, f_create, f_write = _make_odoo_doubles(
            products={
                "MON0026": {"template_id": 7777, "variant_id": 7800,
                            "uom_id": 1, "name": "Monitor 24"},
            },
        )

        with patch("mcp_odoo.tools.sales.odoo_search", side_effect=f_search), \
             patch("mcp_odoo.tools.sales.odoo_read", side_effect=f_read), \
             patch("mcp_odoo.tools.sales.odoo_create", side_effect=f_create), \
             patch("mcp_odoo.tools.sales.odoo_write", side_effect=f_write):
            result = odoo_create_quotation(
                *_CREDS,
                partner_id=99,
                lines=[{"code": "MON0026", "quantity": 1}],
            )

        assert result["success"] is True, result
        # The sale.order create must have used the resolved variant (7800),
        # NOT the raw template id we never asked the LLM about.
        order_create = state["created_orders"][0]
        order_lines = order_create["order_line"]
        assert order_lines, "no order_line tuples passed to odoo_create"
        first = order_lines[0][2]  # (0, 0, vals)
        assert first["product_id"] == 7800

    def test_code_not_found(self):
        """An unknown code returns product_code_not_found, no order created."""
        from mcp_odoo.tools.sales import odoo_create_quotation

        state, f_search, f_read, f_create, f_write = _make_odoo_doubles(products={})

        with patch("mcp_odoo.tools.sales.odoo_search", side_effect=f_search), \
             patch("mcp_odoo.tools.sales.odoo_read", side_effect=f_read), \
             patch("mcp_odoo.tools.sales.odoo_create", side_effect=f_create), \
             patch("mcp_odoo.tools.sales.odoo_write", side_effect=f_write):
            result = odoo_create_quotation(
                *_CREDS,
                partner_id=99,
                lines=[{"code": "NOPE9999", "quantity": 1}],
            )

        assert result["success"] is False
        assert result["error_code"] == "product_code_not_found"
        assert state["created_orders"] == []

    def test_code_and_product_id_consistent(self):
        """Passing both with matching default_code proceeds normally."""
        from mcp_odoo.tools.sales import odoo_create_quotation

        state, f_search, f_read, f_create, f_write = _make_odoo_doubles(
            products={
                "MON0026": {"template_id": 7777, "variant_id": 7800,
                            "uom_id": 1, "name": "Monitor 24"},
            },
        )

        with patch("mcp_odoo.tools.sales.odoo_search", side_effect=f_search), \
             patch("mcp_odoo.tools.sales.odoo_read", side_effect=f_read), \
             patch("mcp_odoo.tools.sales.odoo_create", side_effect=f_create), \
             patch("mcp_odoo.tools.sales.odoo_write", side_effect=f_write):
            result = odoo_create_quotation(
                *_CREDS,
                partner_id=99,
                lines=[{"product_id": 7777, "code": "MON0026", "quantity": 2}],
            )

        assert result["success"] is True, result
        assert state["created_orders"], "no order was created"

    def test_code_and_product_id_inconsistent(self):
        """Pre-existing mismatch guard must fire when code != real default_code."""
        from mcp_odoo.tools.sales import odoo_create_quotation

        state, f_search, f_read, f_create, f_write = _make_odoo_doubles(
            products={
                "MON0026": {"template_id": 7777, "variant_id": 7800,
                            "uom_id": 1, "name": "Monitor 24"},
            },
        )

        with patch("mcp_odoo.tools.sales.odoo_search", side_effect=f_search), \
             patch("mcp_odoo.tools.sales.odoo_read", side_effect=f_read), \
             patch("mcp_odoo.tools.sales.odoo_create", side_effect=f_create), \
             patch("mcp_odoo.tools.sales.odoo_write", side_effect=f_write):
            result = odoo_create_quotation(
                *_CREDS,
                partner_id=99,
                # template_id is real, but the LLM declared the wrong code.
                lines=[{"product_id": 7777, "code": "WEBCAM0001", "quantity": 1}],
            )

        assert result["success"] is False
        assert result["error_code"] == "product_code_mismatch"
        assert state["created_orders"] == []

    def test_product_id_only_still_works(self):
        """Backward compat: no `code`, raw template_id still produces a quote."""
        from mcp_odoo.tools.sales import odoo_create_quotation

        state, f_search, f_read, f_create, f_write = _make_odoo_doubles(
            products={
                "LAP0176": {"template_id": 4242, "variant_id": 4300,
                            "uom_id": 1, "name": "Laptop"},
            },
        )

        with patch("mcp_odoo.tools.sales.odoo_search", side_effect=f_search), \
             patch("mcp_odoo.tools.sales.odoo_read", side_effect=f_read), \
             patch("mcp_odoo.tools.sales.odoo_create", side_effect=f_create), \
             patch("mcp_odoo.tools.sales.odoo_write", side_effect=f_write):
            result = odoo_create_quotation(
                *_CREDS,
                partner_id=99,
                lines=[{"product_id": 4242, "quantity": 1}],
            )

        assert result["success"] is True, result
        first = state["created_orders"][0]["order_line"][0][2]
        assert first["product_id"] == 4300


# ─────────────────────────────────────────────────────────────────────
# add_to_quotation
# ─────────────────────────────────────────────────────────────────────


class TestAddToQuotationWithCode:

    def test_code_only_resolves_and_appends(self):
        from mcp_odoo.tools.sales import odoo_add_to_quotation

        existing = {
            123: {
                "id": 123,
                "name": "SO00123",
                "state": "draft",
                "partner_id": [99, "ACME"],
                "amount_untaxed": 0.0,
                "amount_tax": 0.0,
                "amount_total": 0.0,
                "order_line": [],
                "share_link_so": "https://odoo/test",
            },
        }
        state, f_search, f_read, f_create, f_write = _make_odoo_doubles(
            products={
                "MON0026": {"template_id": 7777, "variant_id": 7800,
                            "uom_id": 1, "name": "Monitor 24"},
            },
            existing_orders=existing,
        )

        with patch("mcp_odoo.tools.sales.odoo_search", side_effect=f_search), \
             patch("mcp_odoo.tools.sales.odoo_read", side_effect=f_read), \
             patch("mcp_odoo.tools.sales.odoo_create", side_effect=f_create), \
             patch("mcp_odoo.tools.sales.odoo_write", side_effect=f_write):
            # confirmed=True so we actually write.
            result = odoo_add_to_quotation(
                *_CREDS,
                order_id=123,
                lines=[{"code": "MON0026", "quantity": 3}],
                confirmed=True,
            )

        assert result["success"] is True, result
        # The created sale.order.line should reference the resolved variant.
        assert state["created_lines"], "no line was written"
        assert state["created_lines"][0]["product_id"][0] == 7800

    def test_ambiguous_code_rejected(self):
        from mcp_odoo.tools.sales import odoo_add_to_quotation

        existing = {
            123: {
                "id": 123, "name": "SO00123", "state": "draft",
                "partner_id": [99, "ACME"], "amount_untaxed": 0.0,
                "amount_tax": 0.0, "amount_total": 0.0,
                "order_line": [], "share_link_so": "",
            },
        }
        state, f_search, f_read, f_create, f_write = _make_odoo_doubles(
            products={
                "DUP0001": {"template_id": 1, "variant_id": 11,
                            "uom_id": 1, "name": "dup A"},
            },
            ambiguous_codes={"DUP0001"},
            existing_orders=existing,
        )

        with patch("mcp_odoo.tools.sales.odoo_search", side_effect=f_search), \
             patch("mcp_odoo.tools.sales.odoo_read", side_effect=f_read), \
             patch("mcp_odoo.tools.sales.odoo_create", side_effect=f_create), \
             patch("mcp_odoo.tools.sales.odoo_write", side_effect=f_write):
            result = odoo_add_to_quotation(
                *_CREDS,
                order_id=123,
                lines=[{"code": "DUP0001", "quantity": 1}],
                confirmed=True,
            )

        assert result["success"] is False
        assert result["error_code"] == "ambiguous_product_code"
        assert "candidates" in result and len(result["candidates"]) == 2
        assert state["created_lines"] == []
