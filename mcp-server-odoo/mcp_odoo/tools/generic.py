"""Generic CRUD tools for any Odoo model."""

from typing import Any

from mcp_odoo.transports.xmlrpc import odoo_pool


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
