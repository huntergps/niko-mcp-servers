"""Generic CRUD tools for any Odoo model."""

import logging
import time
from typing import Any

from mcp_odoo.transports.xmlrpc import odoo_pool

logger = logging.getLogger(__name__)

# Cache of valid field names per (db, model) to tolerate schema drift between
# Odoo versions (e.g. Odoo 17+ removed res.partner.mobile, merging it into
# phone). Keyed by (db, model) -> (set_of_field_names, expiry_epoch).
# TTL keeps the cache fresh if a tenant upgrades / installs a module.
_FIELDS_CACHE: dict[tuple[str, str], tuple[set[str], float]] = {}
_FIELDS_CACHE_TTL = 600.0  # seconds


def valid_model_fields(
    tenant_id: str, url: str, db: str, user: str, password: str,
    model: str, desired: list[str],
) -> list[str]:
    """Filter `desired` field names down to those that actually exist on `model`.

    Calls Odoo's fields_get once per (db, model) and caches the result for
    _FIELDS_CACHE_TTL seconds. Used to tolerate field drift between Odoo
    versions (e.g. res.partner.mobile removed in Odoo 17+).

    Fail-open: if fields_get cannot be resolved (network error, permission,
    etc.) the original `desired` list is returned unchanged so behaviour on
    healthy Odoo 13 instances is never degraded.
    """
    cache_key = (db, model)
    now = time.time()
    cached = _FIELDS_CACHE.get(cache_key)
    valid: set[str] | None = None
    if cached is not None and cached[1] > now:
        valid = cached[0]

    if valid is None:
        try:
            # attributes=[] keeps the payload tiny — we only need the keys.
            fields_meta = odoo_pool.execute(
                tenant_id, url, db, user, password,
                model, "fields_get", [], {"attributes": []},
            )
            valid = set(fields_meta.keys()) if isinstance(fields_meta, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "valid_model_fields: fields_get failed db=%s model=%s: %s — "
                "failing open (returning desired fields unchanged)",
                db, model, exc,
            )
            valid = None

        if valid:
            _FIELDS_CACHE[cache_key] = (valid, now + _FIELDS_CACHE_TTL)

    if not valid:
        # Fail-open: keep the caller's list intact.
        return list(desired)

    return [f for f in desired if f in valid]


def valid_partner_fields(
    tenant_id: str, url: str, db: str, user: str, password: str,
    desired: list[str],
) -> list[str]:
    """Convenience wrapper of `valid_model_fields` for res.partner."""
    return valid_model_fields(
        tenant_id, url, db, user, password, "res.partner", desired,
    )


def resolve_field(
    tenant_id: str, url: str, db: str, user: str, password: str,
    model: str, candidates: list[str],
) -> str:
    """Return the FIRST of ``candidates`` that exists on ``model``.

    Sibling of :func:`valid_model_fields` — reuses the same cached
    ``fields_get`` per (db, model). Used to bridge field renames across
    Odoo versions where a single canonical column has different names
    (e.g. ``account.move.type`` in Odoo 13 became ``move_type`` in Odoo
    16+; ``invoice_payment_state`` became ``payment_state``).

    The caller passes the candidates in PREFERENCE order. ``valid``
    preserves that order (it filters the candidate list), so the first
    surviving entry is the one to use.

    Fail-open: if ``fields_get`` cannot be resolved, or none of the
    candidates exist on the model, the FIRST candidate is returned
    unchanged. On a healthy Odoo 13 instance the modern names simply
    won't match and the legacy name (passed as a later candidate) is
    selected; if the schema lookup itself fails we degrade to the first
    candidate so behaviour is deterministic and never raises here.
    """
    if not candidates:
        raise ValueError("resolve_field requires a non-empty candidates list")

    valid = valid_model_fields(
        tenant_id, url, db, user, password, model, list(candidates),
    )
    # valid_model_fields preserves the order of the input list. When the
    # cache/fields_get is healthy it returns only existing fields; when it
    # fails open it returns the candidates unchanged. Either way the first
    # element is our best guess.
    for cand in candidates:
        if cand in valid:
            return cand
    return candidates[0]


def odoo_search(
    tenant_id: str, url: str, db: str, user: str, password: str,
    model: str, domain: list, fields: list | None = None,
    limit: int = 80, offset: int = 0, order: str | None = None,
) -> list[dict]:
    """Search and read records from any Odoo model.

    Uses search_read for efficiency (single call instead of search + read).
    """
    kwargs = {}
    if fields:
        kwargs["fields"] = fields
    if limit:
        kwargs["limit"] = limit
    if offset:
        kwargs["offset"] = offset
    if order:
        kwargs["order"] = order

    return odoo_pool.execute(
        tenant_id, url, db, user, password,
        model, "search_read", [domain], kwargs,
    )


def odoo_read(
    tenant_id: str, url: str, db: str, user: str, password: str,
    model: str, ids: list[int], fields: list | None = None,
) -> list[dict]:
    """Read specific records by IDs."""
    kwargs = {}
    if fields:
        kwargs["fields"] = fields

    return odoo_pool.execute(
        tenant_id, url, db, user, password,
        model, "read", [ids], kwargs,
    )


def odoo_create(
    tenant_id: str, url: str, db: str, user: str, password: str,
    model: str, values: dict,
) -> int:
    """Create a record. Returns the new record ID."""
    return odoo_pool.execute(
        tenant_id, url, db, user, password,
        model, "create", [values],
    )


def odoo_write(
    tenant_id: str, url: str, db: str, user: str, password: str,
    model: str, ids: list[int], values: dict,
) -> bool:
    """Update records. Returns True on success."""
    return odoo_pool.execute(
        tenant_id, url, db, user, password,
        model, "write", [ids, values],
    )


def odoo_call_method(
    tenant_id: str, url: str, db: str, user: str, password: str,
    model: str, method: str, ids: list[int],
    args: list | None = None, kwargs: dict | None = None,
) -> Any:
    """Call any public method on a model (e.g., action_confirm, button_done).

    Used for calling business logic methods that are not CRUD.
    """
    call_args = [ids]
    if args:
        call_args.extend(args)

    return odoo_pool.execute(
        tenant_id, url, db, user, password,
        model, method, call_args, kwargs or {},
    )
