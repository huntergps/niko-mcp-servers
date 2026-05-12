"""Tests for the order_id-vs-name-suffix guard collision case.

Production incident 2026-05-12: a customer's cotización VENTA122567 has
``order_id=113998``. The LLM correctly called
``send_quotation(order_id=113998)``. Unfortunately a SEPARATE quotation
in the same tenant has ``name='VENTA113998'`` (its real id is 105429).

The original guard rejected the call with ``order_id_looks_like_name_suffix``
because it found a record named ``VENTA113998`` and assumed confusion —
but the order_id WAS valid, just happened to numerically collide with
another record's name suffix.

The fix: before checking for name-suffix confusion, the guard performs an
authoritative id-existence check. If ``order_id`` is a real
``sale.order.id``, the guard MUST pass.

This file complements ``test_order_id_confusion.py`` (which tests the
suffix-rejection happy path) by covering the collision-pass case.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_odoo.tools import sales as sales_mod


class TestRealIdShortCircuitsGuard:
    """When ``order_id`` exists as a real sale.order.id, the guard MUST
    return None — even if a different record has ``name=VENTA{order_id}``.
    """

    def test_real_id_with_colliding_name_suffix_passes(self):
        """The exact production incident: order_id=113998 IS a real id
        (sale.order(id=113998, name='VENTA122567')) AND a different
        quotation has name='VENTA113998' (id=105429). The guard must
        return None so send_quotation can proceed."""
        mock_pool = MagicMock()
        # Only the id-existence probe runs; it returns the real record.
        # The name-suffix lookup is NEVER issued because step 2 of the
        # guard short-circuits as soon as the id is confirmed.
        mock_pool.execute.return_value = [{"id": 113998}]

        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod._guard_order_id_vs_name_suffix(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=113998,
            )

        assert result is None, (
            "guard must return None for a real order_id even when a "
            "different record has name=VENTA{order_id}"
        )
        # Exactly ONE call (the id existence check). No name lookup.
        assert mock_pool.execute.call_count == 1
        call = mock_pool.execute.call_args_list[0]
        assert call.args[5] == "sale.order"
        assert call.args[6] == "search_read"
        assert call.args[7] == [[["id", "=", 113998]]]

    def test_real_id_with_no_name_collision_passes(self):
        """Sanity check: order_id is real and there is NO record named
        VENTA{order_id}. Still only the id-existence probe runs."""
        mock_pool = MagicMock()
        mock_pool.execute.return_value = [{"id": 115000}]

        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod._guard_order_id_vs_name_suffix(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=115000,
            )

        assert result is None
        # Single probe — guard short-circuits on the first call.
        assert mock_pool.execute.call_count == 1


class TestNonExistentIdHittingNameSuffixRejected:
    """Backward compat for the original incident: when ``order_id`` is
    NOT a real id AND a record exists with ``name=VENTA{order_id}``, the
    guard still rejects with ``suggested_order_id``."""

    def test_non_existent_id_with_matching_name_suffix_rejected(self):
        """order_id=122173 is NOT a real id; VENTA122173 exists with
        real id 113604. The guard must reject and surface 113604."""
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) id-existence check → 122173 is NOT a real id
            [],
            # 2) name lookup → VENTA122173 exists with id=113604
            [{"id": 113604}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod._guard_order_id_vs_name_suffix(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=122173,
            )

        assert result is not None
        assert result["success"] is False
        assert result["error_code"] == "order_id_looks_like_name_suffix"
        assert result["suggested_order_id"] == 113604
        assert result["found_name"] == "VENTA122173"
        # Both probes ran.
        assert mock_pool.execute.call_count == 2


class TestNonExistentIdWithoutNameMatchPassesThrough:
    """When ``order_id`` is not a real id AND there is no matching name,
    the guard returns None — the caller's normal flow will then surface
    ``order_not_found`` (or similar) on its own."""

    def test_non_existent_id_no_name_collision_passes(self):
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) id-existence check → 119999 not a real id
            [],
            # 2) name lookup → no record named 'VENTA119999'
            [],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod._guard_order_id_vs_name_suffix(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=119999,
            )

        assert result is None
        # Both probes ran (neither short-circuited).
        assert mock_pool.execute.call_count == 2


class TestIdExistenceLookupErrorFallsThroughToNameCheck:
    """If the id-existence probe raises, the guard must NOT block — it
    falls through to the name-suffix check. This protects against
    transient XML-RPC errors in the new step 2 lookup blocking valid
    calls."""

    def test_id_check_exception_then_name_check_passes_through(self):
        mock_pool = MagicMock()

        # First call (id check) raises; second (name check) returns no
        # match → guard returns None.
        mock_pool.execute.side_effect = [
            RuntimeError("transient xmlrpc failure"),
            [],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod._guard_order_id_vs_name_suffix(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=120000,
            )

        assert result is None
        assert mock_pool.execute.call_count == 2


class TestSendQuotationCollisionWiredThrough:
    """End-to-end: odoo_send_quotation must succeed when called with a
    real order_id whose value collides with a different record's name
    suffix (the exact production incident)."""

    def test_send_quotation_with_real_id_colliding_name_succeeds(self):
        """Simulate the full call site: odoo_send_quotation(order_id=113998)
        where 113998 IS a real id (VENTA122567) AND a different record
        has name='VENTA113998'. The guard must pass and send_quotation
        must proceed to its preview (confirmed=False) without hitting
        the false-positive ``order_id_looks_like_name_suffix`` error."""
        mock_pool = MagicMock()

        # Sequence:
        # 1) guard id-existence check → record exists → short-circuit
        # 2) odoo_read for the preview header
        mock_pool.execute.side_effect = [
            [{"id": 113998}],                                          # guard id check
            [{                                                          # preview read
                "id": 113998, "name": "VENTA122567", "state": "draft",
                "partner_id": [42, "Cliente Prod"], "amount_total": 999.99,
            }],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            result = sales_mod.odoo_send_quotation(
                "test-tenant-001", "http://x", "db", "u", "p",
                order_id=113998,
                confirmed=False,
                session_active_quotation_id=113998,
            )

        # The key assertion: NO false-positive guard rejection.
        assert result.get("error_code") != "order_id_looks_like_name_suffix"
        # And the function reached its preview path.
        assert result.get("requires_confirmation") is True
        assert result.get("order_id") == 113998
        assert result.get("order_name") == "VENTA122567"
        # The guard called execute exactly once (id-existence probe)
        # before handing off to send_quotation's own logic.
        first_call = mock_pool.execute.call_args_list[0]
        assert first_call.args[5] == "sale.order"
        assert first_call.args[6] == "search_read"
        assert first_call.args[7] == [[["id", "=", 113998]]]
