"""Tools package — MCP tool implementations.

Tool functions are intentionally imported lazily inside the MCP dispatcher
(``mcp_odoo/transports/mcp_transport.py``) to keep cold-start fast. The
explicit re-exports here are only for callers that prefer to import from
``mcp_odoo.tools`` directly (tests, CLI helpers, etc.).
"""

from .payments import odoo_create_payphone_link, odoo_check_payphone_status

__all__ = [
    "odoo_create_payphone_link",
    "odoo_check_payphone_status",
]
