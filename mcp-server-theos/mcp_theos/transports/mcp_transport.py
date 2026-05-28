"""MCP StreamableHTTP transport — JSON-RPC over POST /mcp.

Mirrors the protocol surface used by mcp-server-odoo (initialize,
notifications/initialized, tools/list, tools/call, ping). Each
``tools/call`` resolves the tenant via ``X-Tenant-Id`` header, opens a
short-lived :class:`VelneoClient`, dispatches to one of the nine tool
functions, and returns the JSON envelope as the MCP text content.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mcp_theos.tenant_resolver import get_tenant_config
from mcp_theos.tools import invoices, partners, payments, products, sales
from mcp_theos.velneo_http import VelneoClient, VelneoError

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------
# Tool registry
# --------------------------------------------------------------------------

#
# Tool naming: mirrors mcp-server-odoo. The agent's enabled_tools list
# (in tenant_<slug>.agents) is the same regardless of which backend
# serves the call — the LLM sees ``search_products`` whether the row is
# resolved against Odoo or Velneo. Keeping the names aligned means a
# tenant copied from Tecnosmart works against Velneo with zero edits
# to enabled_tools.
MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": (
            "Search products in the catalog by name (also accepts SKU/code). "
            "Returns up to ``limit`` rows with PVP1 (price with VAT) merged "
            "from the price table. Use this before any quotation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search term (name, code, or partial)"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "include_prices": {"type": "boolean", "default": True},
                "tarifa_id": {"type": "integer", "description": "tariff id to pin pricing"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "identify_customer",
        "description": (
            "Look a customer up by RUC, cédula, email, name or phone and "
            "return a merged profile (master + customer-extension fields "
            "like SALDO / CUPOC). Pass exactly one identifier."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ruc": {"type": "string"},
                "cedula": {"type": "string"},
                "email": {"type": "string"},
                "name": {"type": "string"},
                "phone": {"type": "string"},
            },
        },
    },
    {
        "name": "create_partner",
        "description": (
            "Create a new customer in the ERP. RUC or cédula goes into "
            "``cif``. Always returns the new ``partner_id`` which the "
            "caller must use for create_quotation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "cif": {"type": "string", "description": "RUC or cédula"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "address": {"type": "string"},
                "is_person": {"type": "boolean", "default": True},
                "tipo_cliente": {"type": "integer"},
            },
            "required": ["name", "cif"],
        },
    },
    {
        "name": "create_quotation",
        "description": (
            "Create a sales quotation with N lines. Each line needs "
            "``product_id`` and ``quantity``; ``unit_price`` is optional "
            "(the ERP applies the tariff default when omitted)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "quantity": {"type": "number"},
                            "unit_price": {"type": "number"},
                            "factor": {"type": "number"},
                            "warehouse_id": {"type": "integer"},
                        },
                        "required": ["product_id", "quantity"],
                    },
                },
                "salesperson_id": {"type": "integer"},
                "company_id": {"type": "integer"},
                "branch_id": {"type": "integer"},
                "tariff_id": {"type": "integer"},
                "warehouse_id": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["client_id", "lines"],
        },
    },
    {
        "name": "get_quotation",
        "description": "Fetch a quotation header + its lines by order_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "list_quotations",
        "description": "List recent quotations, optionally filtered by client_id or salesperson_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "salesperson_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "get_customer_invoices",
        "description": (
            "List invoices for a client. Set ``include_lines=true`` to "
            "also fetch each invoice's line items."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 30, "minimum": 1, "maximum": 100},
                "include_lines": {"type": "boolean", "default": False},
            },
            "required": ["client_id"],
        },
    },
    {
        "name": "check_balance",
        "description": (
            "Customer balance summary (SALDO, DEUDASC, CUPOC, etc.). Set "
            "``detailed=true`` to also pull per-invoice aging."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "detailed": {"type": "boolean", "default": False},
            },
            "required": ["client_id"],
        },
    },
    {
        "name": "get_customer_payments",
        "description": (
            "List customer payments. Set ``include_detail=true`` to also "
            "pull the payment-to-debt allocation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 30, "minimum": 1, "maximum": 100},
                "include_detail": {"type": "boolean", "default": False},
            },
            "required": ["client_id"],
        },
    },
]


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

ToolFn = Callable[..., Awaitable[dict[str, Any]]]

_DISPATCH: dict[str, tuple[str, ToolFn]] = {
    "search_products": ("products.search_products", products.search_products),
    "identify_customer": ("partners.identify_customer", partners.identify_customer),
    "create_partner": ("partners.create_partner", partners.create_partner),
    "create_quotation": ("sales.create_quotation", sales.create_quotation),
    "get_quotation": ("sales.get_quotation", sales.get_quotation),
    "list_quotations": ("sales.list_quotations", sales.list_quotations),
    "get_customer_invoices": ("invoices.get_customer_invoices", invoices.get_customer_invoices),
    "check_balance": ("invoices.check_balance", invoices.check_balance),
    "get_customer_payments": ("payments.get_customer_payments", payments.get_customer_payments),
}


def _parse_allowed_tools(request: Request) -> set[str] | None:
    raw = request.headers.get("x-allowed-tools") or request.headers.get("X-Allowed-Tools")
    if not raw:
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


def _make_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _execute_tool(request: Request, name: str, args: dict[str, Any]) -> str:
    entry = _DISPATCH.get(name)
    if entry is None:
        return json.dumps({"success": False, "error": f"unknown tool {name!r}"}, ensure_ascii=False)
    _label, fn = entry

    cfg = await get_tenant_config(request)
    async with VelneoClient(cfg) as client:
        try:
            result = await fn(client, **args)
        except TypeError as exc:
            return json.dumps(
                {"success": False, "error": f"bad arguments: {exc}"},
                ensure_ascii=False,
            )
        except VelneoError as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": f"velneo {exc.status}: {exc.message}",
                    "velneo_status": exc.status,
                },
                ensure_ascii=False,
            )
    return json.dumps(result, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    """StreamableHTTP MCP endpoint."""
    body = await request.json()
    return JSONResponse(await _handle_mcp_request(request, body) or {"jsonrpc": "2.0"})


async def _handle_mcp_request(request: Request, body: dict[str, Any]) -> dict[str, Any] | None:
    req_id = body.get("id")
    method = body.get("method", "")

    if method == "initialize":
        return _make_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mcp-server-theos", "version": "0.1.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        allowed = _parse_allowed_tools(request)
        if allowed is not None:
            tools = [t for t in MCP_TOOLS if t["name"] in allowed]
        else:
            tools = MCP_TOOLS
        return _make_response(req_id, {"tools": tools})

    if method == "tools/call":
        params = body.get("params") or {}
        tool_name = params.get("name", "")
        args = params.get("arguments") or {}

        allowed = _parse_allowed_tools(request)
        if allowed is not None and tool_name not in allowed:
            logger.warning(
                "tool %r blocked for agent %s (allowed=%s)",
                tool_name,
                request.headers.get("x-agent-slug", "?"),
                sorted(allowed),
            )
            return _make_response(req_id, {
                "content": [{"type": "text", "text":
                    f"Herramienta '{tool_name}' no está habilitada para este agente."
                }],
                "isError": True,
            })

        try:
            text = await _execute_tool(request, tool_name, args)
            return _make_response(req_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:
            import traceback
            logger.error("tool %s failed: %s\n%s", tool_name, exc, traceback.format_exc())
            return _make_response(req_id, {
                "content": [{"type": "text", "text":
                    json.dumps(
                        {"success": False, "error": f"{type(exc).__name__}: {exc}"},
                        ensure_ascii=False,
                    )
                }],
                "isError": True,
            })

    if method == "ping":
        return _make_response(req_id, {})

    return _make_error(req_id, -32601, f"unknown method: {method}")
