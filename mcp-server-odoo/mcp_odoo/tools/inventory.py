"""Inventory tools — uses computed fields, NOT stock.quant directly."""

from mcp_odoo.transports.xmlrpc import odoo_pool

MAX_PRODUCTS_PER_QUERY = 50


def odoo_check_stock(
    tenant_id: str, url: str, db: str, user: str, password: str,
    product_ids: list[int],
    warehouse_id: int | None = None,
) -> list[dict]:
    """Check stock availability for specific products.

    Uses qty_available and virtual_available from product.product,
    NOT stock.quant directly (which ignores reservations and location hierarchy).

    Args:
        product_ids: List of product.product IDs (max 50 per call)
        warehouse_id: Optional warehouse ID for context filtering
    """
    if len(product_ids) > MAX_PRODUCTS_PER_QUERY:
        raise ValueError(
            f"Max {MAX_PRODUCTS_PER_QUERY} products per query. "
            f"Got {len(product_ids)}."
        )

    kwargs = {
        "fields": [
            "name", "default_code", "list_price",
            "qty_available", "virtual_available",
        ],
    }

    context = {}
    if warehouse_id:
        context["warehouse"] = warehouse_id

    if context:
        kwargs["context"] = context

    # product_ids may be product.template IDs (from RAG search) or product.product IDs.
    # Try product_tmpl_id first (more common from search_products), fallback to direct ID.
    results = odoo_pool.execute(
        tenant_id, url, db, user, password,
        "product.product", "search_read",
        [[["product_tmpl_id", "in", product_ids]]],
        kwargs,
    )
    if not results:
        # Fallback: try as product.product IDs directly
        results = odoo_pool.execute(
            tenant_id, url, db, user, password,
            "product.product", "search_read",
            [[["id", "in", product_ids]]],
            kwargs,
        )
    return results
