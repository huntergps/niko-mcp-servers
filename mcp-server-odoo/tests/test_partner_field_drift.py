"""Schema-drift tolerance for res.partner reads across Odoo versions.

Odoo 17+ removed ``res.partner.mobile`` (merged into ``phone``). The
Afrodita tenant runs Odoo 19, so any search_read/read/create that asks
for ``mobile`` raises ``ValueError: Invalid field 'mobile' on
'res.partner'``. The fix introduces ``valid_partner_fields`` /
``valid_model_fields`` (mcp_odoo.tools.generic) which call ``fields_get``
once per (db, model), cache the result, and filter desired field lists
down to those that actually exist. Odoo 13 (Tecnosmart) still has
``mobile`` and must keep working unchanged.

Tests mock ``odoo_pool.execute`` so no live Odoo is required.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mcp_odoo.tools import generic
from mcp_odoo.tools.generic import (
    resolve_field,
    valid_partner_fields,
    valid_model_fields,
)


# res.partner fields_get shapes for each Odoo version (only keys matter).
ODOO13_PARTNER = {
    f: {} for f in [
        "id", "name", "vat", "email", "phone", "mobile",
        "street", "city", "country_id", "customer_rank", "supplier_rank",
        "property_payment_term_id", "credit_limit",
    ]
}
ODOO19_PARTNER = {
    f: {} for f in [
        "id", "name", "vat", "email", "phone",  # NOTE: no "mobile"
        "street", "city", "country_id", "customer_rank", "supplier_rank",
        "property_payment_term_id", "credit_limit",
    ]
}

CREDS = ("tenant_x", "https://odoo.example", "db_x", "worker_api", "secret")
DESIRED = ["name", "vat", "email", "phone", "mobile",
           "street", "city", "country_id", "customer", "supplier"]


@pytest.fixture(autouse=True)
def _clear_fields_cache():
    """Each test starts with an empty (db, model) field cache."""
    generic._FIELDS_CACHE.clear()
    yield
    generic._FIELDS_CACHE.clear()


def _fake_execute(fields_get_return):
    """Build a MagicMock for odoo_pool.execute that answers fields_get."""
    def _exec(tenant_id, url, db, user, password, model, method,
              args=None, kwargs=None):
        if method == "fields_get":
            return fields_get_return
        raise AssertionError(f"unexpected method {method!r}")
    return MagicMock(side_effect=_exec)


# ---------------------------------------------------------------------------
# valid_model_fields / valid_partner_fields
# ---------------------------------------------------------------------------


def test_odoo19_drops_mobile():
    """On Odoo 19 (no mobile), the field is filtered out of the list."""
    fake = _fake_execute(ODOO19_PARTNER)
    with patch.object(generic.odoo_pool, "execute", fake):
        result = valid_partner_fields(*CREDS, DESIRED)
    assert "mobile" not in result
    # Real fields preserved in original order; mobile + the legacy
    # customer/supplier aliases (absent in modern Odoo) are dropped.
    assert result == ["name", "vat", "email", "phone",
                      "street", "city", "country_id"]
    # fields_get does NOT include 'mobile' (sanity on the fixture).
    assert "mobile" not in ODOO19_PARTNER


def test_odoo13_keeps_mobile():
    """On Odoo 13 the mobile field exists and is preserved."""
    fake = _fake_execute(ODOO13_PARTNER)
    with patch.object(generic.odoo_pool, "execute", fake):
        result = valid_partner_fields(*CREDS, DESIRED)
    assert "mobile" in result
    # 'customer'/'supplier' don't exist in either fixture -> dropped.
    assert result == ["name", "vat", "email", "phone", "mobile",
                      "street", "city", "country_id"]


def test_fields_get_called_once_per_db_then_cached():
    """fields_get is hit once, subsequent calls use the cache."""
    fake = _fake_execute(ODOO19_PARTNER)
    with patch.object(generic.odoo_pool, "execute", fake):
        valid_partner_fields(*CREDS, DESIRED)
        valid_partner_fields(*CREDS, ["mobile", "phone"])
        valid_partner_fields(*CREDS, DESIRED)
    assert fake.call_count == 1


def test_cache_keyed_per_db():
    """Different db -> separate fields_get call."""
    fake = _fake_execute(ODOO19_PARTNER)
    creds_other_db = ("tenant_x", "https://odoo.example", "db_y", "u", "p")
    with patch.object(generic.odoo_pool, "execute", fake):
        valid_partner_fields(*CREDS, DESIRED)
        valid_partner_fields(*creds_other_db, DESIRED)
    assert fake.call_count == 2


def test_fail_open_on_fields_get_error():
    """If fields_get raises, the original desired list is returned intact."""
    fake = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(generic.odoo_pool, "execute", fake):
        result = valid_partner_fields(*CREDS, DESIRED)
    assert result == DESIRED  # unchanged — never degrade healthy instances


def test_fail_open_does_not_poison_cache():
    """A failed fields_get is not cached; a later success filters correctly."""
    err = MagicMock(side_effect=RuntimeError("transient"))
    with patch.object(generic.odoo_pool, "execute", err):
        assert valid_partner_fields(*CREDS, DESIRED) == DESIRED
    ok = _fake_execute(ODOO19_PARTNER)
    with patch.object(generic.odoo_pool, "execute", ok):
        result = valid_partner_fields(*CREDS, DESIRED)
    assert "mobile" not in result


def test_valid_model_fields_generic_model():
    """Helper works for arbitrary models, cache keyed by (db, model)."""
    fake = _fake_execute({"id": {}, "name": {}, "list_price": {}})
    with patch.object(generic.odoo_pool, "execute", fake):
        result = valid_model_fields(
            *CREDS, "product.product", ["name", "list_price", "barcode"],
        )
    assert result == ["name", "list_price"]


# ---------------------------------------------------------------------------
# resolve_field — bridge field renames across Odoo versions
# ---------------------------------------------------------------------------

# account.move fields_get shapes for each version (keys only).
_O13_MOVE = {
    f: {} for f in [
        "id", "name", "type", "state", "invoice_payment_state", "ref",
    ]
}
_O19_MOVE = {
    f: {} for f in [
        "id", "name", "move_type", "state", "payment_state", "memo",
    ]
}


def test_resolve_field_move_type_odoo19():
    """Odoo 19 has move_type (not type) → resolves to move_type."""
    fake = _fake_execute(_O19_MOVE)
    with patch.object(generic.odoo_pool, "execute", fake):
        assert resolve_field(
            *CREDS, "account.move", ["move_type", "type"],
        ) == "move_type"
        assert resolve_field(
            *CREDS, "account.move", ["payment_state", "invoice_payment_state"],
        ) == "payment_state"


def test_resolve_field_move_type_odoo13():
    """Odoo 13 has type (not move_type) → resolves to legacy name."""
    fake = _fake_execute(_O13_MOVE)
    with patch.object(generic.odoo_pool, "execute", fake):
        assert resolve_field(
            *CREDS, "account.move", ["move_type", "type"],
        ) == "type"
        assert resolve_field(
            *CREDS, "account.move", ["payment_state", "invoice_payment_state"],
        ) == "invoice_payment_state"


def test_resolve_field_ref_picks_first_existing():
    """ref candidates: O13 keeps 'ref'; O19 schema (no ref) falls to 'memo'."""
    with patch.object(generic.odoo_pool, "execute", _fake_execute(_O13_MOVE)):
        assert resolve_field(
            *CREDS, "account.move", ["ref", "memo", "name"],
        ) == "ref"
    generic._FIELDS_CACHE.clear()
    with patch.object(generic.odoo_pool, "execute", _fake_execute(_O19_MOVE)):
        assert resolve_field(
            *CREDS, "account.move", ["ref", "memo", "name"],
        ) == "memo"


def test_resolve_field_fail_open_returns_first_candidate():
    """fields_get failure → first candidate (modern name) per spec."""
    fake = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(generic.odoo_pool, "execute", fake):
        assert resolve_field(
            *CREDS, "account.move", ["move_type", "type"],
        ) == "move_type"


def test_resolve_field_none_match_returns_first_candidate():
    """No candidate exists on the model → first candidate is returned."""
    fake = _fake_execute({"id": {}, "name": {}})
    with patch.object(generic.odoo_pool, "execute", fake):
        assert resolve_field(
            *CREDS, "account.move", ["move_type", "type"],
        ) == "move_type"


def test_resolve_field_empty_candidates_raises():
    with pytest.raises(ValueError):
        resolve_field(*CREDS, "account.move", [])


# ---------------------------------------------------------------------------
# odoo_search_partner — domain must omit the mobile clause on Odoo 19
# ---------------------------------------------------------------------------


def _capture_search(fields_get_return):
    """Patch fields_get + odoo_search; capture the domain/fields passed."""
    captured = {}

    def _search(tenant_id, url, db, user, password, model, domain,
                fields=None, limit=80, offset=0, order=None):
        captured["domain"] = domain
        captured["fields"] = fields
        return []

    exec_fake = _fake_execute(fields_get_return)
    return captured, exec_fake, _search


def test_search_partner_domain_omits_mobile_on_odoo19():
    from mcp_odoo.tools import sales

    captured, exec_fake, search_fake = _capture_search(ODOO19_PARTNER)
    with patch.object(generic.odoo_pool, "execute", exec_fake), \
         patch.object(sales, "odoo_search", side_effect=search_fake):
        sales.odoo_search_partner(*CREDS, phone="0999123456")

    domain = captured["domain"]
    # No OR operator, no mobile clause — single phone equality.
    assert domain == [["phone", "=", "0999123456"]]
    # And the field list passed to search must not include mobile.
    assert "mobile" not in captured["fields"]


def test_search_partner_domain_keeps_mobile_on_odoo13():
    from mcp_odoo.tools import sales

    captured, exec_fake, search_fake = _capture_search(ODOO13_PARTNER)
    with patch.object(generic.odoo_pool, "execute", exec_fake), \
         patch.object(sales, "odoo_search", side_effect=search_fake):
        sales.odoo_search_partner(*CREDS, phone="0999123456")

    domain = captured["domain"]
    # OR(phone, mobile) preserved.
    assert domain == ["|", ["phone", "=", "0999123456"],
                      ["mobile", "=", "0999123456"]]
    assert "mobile" in captured["fields"]


def test_search_partner_vat_and_name_unaffected():
    """Non-phone searches build the same domain regardless of version."""
    from mcp_odoo.tools import sales

    captured, exec_fake, search_fake = _capture_search(ODOO19_PARTNER)
    with patch.object(generic.odoo_pool, "execute", exec_fake), \
         patch.object(sales, "odoo_search", side_effect=search_fake):
        sales.odoo_search_partner(*CREDS, vat="1790012345001", name="Acme")

    assert captured["domain"] == [
        ["vat", "=", "1790012345001"],
        ["name", "ilike", "Acme"],
    ]
