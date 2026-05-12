"""Tests for the order_id vs name-suffix anti-confusion guard.

Production incident 2026-05-12: the LLM extracted the numeric suffix of
``VENTA122173`` (i.e. ``122173``) and passed it as ``order_id`` to
``get_quotation`` — the real ``sale.order.id`` of ``VENTA122173`` was
``113604``. The MCP returned ``order_not_found`` and the LLM drifted
off-task.

The fix lives in ``mcp_odoo.tools.sales._guard_order_id_vs_name_suffix``
and rejects suspicious order_ids before they reach the read/write path,
pointing the caller at the real id with ``suggested_order_id``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_odoo.tools import sales as sales_mod


# ─────────────────────────────────────────────────────────────────────
# Pure-Python heuristic
# ─────────────────────────────────────────────────────────────────────


class TestLooksLikeNameSuffix:
    """The cheap value-only pre-filter."""

    def test_low_id_not_suspicious(self):
        # Real sale.order.id from years ago — definitely not a suffix.
        assert sales_mod._looks_like_name_suffix(42) is False
        assert sales_mod._looks_like_name_suffix(1) is False
        assert sales_mod._looks_like_name_suffix(99_999) is False

    def test_suspicious_range_matches(self):
        # 110000-130000 is the in-use range for VENTA suffix.
        assert sales_mod._looks_like_name_suffix(110_000) is True
        assert sales_mod._looks_like_name_suffix(122_173) is True
        assert sales_mod._looks_like_name_suffix(125_000) is True
        assert sales_mod._looks_like_name_suffix(130_000) is True

    def test_above_range_not_suspicious(self):
        # Future tenants / out-of-band ids.
        assert sales_mod._looks_like_name_suffix(150_000) is False
        assert sales_mod._looks_like_name_suffix(999_999) is False


# ─────────────────────────────────────────────────────────────────────
# odoo_get_quotation — guard behaviour
# ─────────────────────────────────────────────────────────────────────


class TestGetQuotationWithNameSuffixRejected:
    """Test 1 — the canonical production bug."""

    def test_get_quotation_with_name_suffix_rejected(self):
        """order_id=122173 (the suffix of VENTA122173 whose real id is 113604)
        must be rejected with the precise error pointing at real id 113604."""
        mock_pool = MagicMock()
        # The guard's odoo_search('sale.order', [['name','=','VENTA122173']])
        # finds a record with id=113604. That confirms confusion.
        mock_pool.execute.side_effect = [
            [{"id": 113604}],  # guard lookup → name VENTA122173 exists, id=113604
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_get_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=122173,
            )

        assert result["success"] is False
        assert result["error_code"] == "order_id_looks_like_name_suffix"
        assert result["suggested_order_id"] == 113604
        assert result["found_name"] == "VENTA122173"
        # The guard ran a single odoo_search; the real odoo_read on the
        # bad id was never attempted (the bug: would have returned
        # order_not_found and let the LLM drift).
        assert mock_pool.execute.call_count == 1
        first_call = mock_pool.execute.call_args_list[0]
        # search_read on sale.order with domain name=VENTA122173
        assert first_call.args[5] == "sale.order"
        assert first_call.args[6] == "search_read"
        assert first_call.args[7] == [[["name", "=", "VENTA122173"]]]


class TestGetQuotationWithRealIdWorks:
    """Test 2 — backward compat for actual sale.order.id values."""

    def test_get_quotation_with_real_id_proceeds_normally(self):
        """order_id=113604 (a real id, also in the suspicious range) when
        VENTA113604 does NOT exist in the tenant must proceed normally."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) guard lookup → no record named 'VENTA113604' → pass
            [],
            # 2) regular odoo_read on sale.order header
            [{
                "id": 113604, "name": "VENTA122173", "state": "draft",
                "partner_id": [1, "Cliente"], "amount_total": 200.0,
                "amount_untaxed": 178.57, "amount_tax": 21.43,
                "date_order": "2026-05-12", "create_date": "2026-05-12",
                "order_line": [],
                "share_link_so": "",
            }],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_get_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=113604,
            )

        assert result["success"] is True
        assert result["order_id"] == 113604
        assert result["name"] == "VENTA122173"
        # Two execute calls: guard + read.
        assert mock_pool.execute.call_count == 2


class TestGetQuotationWithLowIdUnaffected:
    """Test 3 — pre-filter short-circuit for ids outside [110k, 130k]."""

    def test_get_quotation_with_low_id_skips_guard(self):
        """order_id=42 is not in the suspicious range so the guard
        lookup is NEVER issued. The order_not_found result is produced
        by the real read, exactly like before the patch."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # Only one call: odoo_read returning empty.
            [],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_get_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=42,
            )

        assert result["success"] is False
        assert result["error_code"] == "order_not_found"
        # Single execute (the regular read), no guard probe.
        assert mock_pool.execute.call_count == 1
        only_call = mock_pool.execute.call_args_list[0]
        assert only_call.args[6] == "read"


class TestNoFalsePositiveWhenSelfReferential:
    """Test 4 — when order_id IS its own name suffix.

    Edge case: sale.order.id=125000 and the same record has
    name='VENTA125000'. The guard sees a match where the candidate
    real_id equals the value passed in — that's NOT confusion, just a
    coincidental alignment of id and name suffix. The guard must let
    the call through.
    """

    def test_no_false_positive_when_id_equals_name_suffix(self):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) guard lookup → 'VENTA125000' exists with id=125000 (same).
            [{"id": 125000}],
            # 2) regular odoo_read proceeds — guard did NOT short-circuit.
            [{
                "id": 125000, "name": "VENTA125000", "state": "draft",
                "partner_id": [1, "Cliente"], "amount_total": 100.0,
                "amount_untaxed": 89.29, "amount_tax": 10.71,
                "date_order": "2026-05-12", "create_date": "2026-05-12",
                "order_line": [],
                "share_link_so": "",
            }],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_get_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=125000,
            )

        assert result["success"] is True
        assert result["order_id"] == 125000
        assert mock_pool.execute.call_count == 2


# ─────────────────────────────────────────────────────────────────────
# Guard applied to other write entrypoints
# ─────────────────────────────────────────────────────────────────────


class TestTransitionQuotationGuard:
    """The same guard fires on transition_quotation."""

    def test_transition_quotation_rejects_name_suffix(self):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # Guard search hits 'VENTA122173' → id=113604, neq 122173.
            [{"id": 113604}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_transition_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=122173, action="confirm", confirmed=True,
            )

        assert result["success"] is False
        assert result["error_code"] == "order_id_looks_like_name_suffix"
        assert result["suggested_order_id"] == 113604
        # Only the guard search ran — no transition was attempted.
        assert mock_pool.execute.call_count == 1


class TestAddToQuotationGuard:
    """add_to_quotation also rejects name-suffix order_id."""

    def test_add_to_quotation_rejects_name_suffix(self):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            [{"id": 113604}],  # guard lookup
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_add_to_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=122173,
                lines=[{"product_id": 999, "quantity": 1}],
                confirmed=False,
            )

        assert result["success"] is False
        assert result["error_code"] == "order_id_looks_like_name_suffix"
        assert result["suggested_order_id"] == 113604
        # We stopped at the guard — no real order read happened.
        assert mock_pool.execute.call_count == 1
