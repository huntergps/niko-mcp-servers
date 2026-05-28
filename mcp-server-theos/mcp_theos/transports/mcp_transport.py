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

from mcp_theos.otp import OTP_REQUIRED_MSG, check_session
from mcp_theos.tenant_resolver import get_tenant_config
from mcp_theos.tools import (
    admin_ops,
    admin_search,
    invoices,
    otp_tools,
    partners,
    payments,
    products,
    sales,
)
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
            "Search products in the catalog. If the query is a code or "
            "barcode (alphanumeric, contains digits, no spaces) it goes "
            "straight to the ERP's product card. For natural-language "
            "queries (e.g. 'arroz Gustadina 1kg', 'detergente') it embeds "
            "the query and pulls top-K nearest products from pgvector, "
            "then enriches each with live price/presentations/family. "
            "Use this BEFORE any quotation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "code, barcode, or free text"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 20},
                "include_image": {"type": "boolean", "default": False,
                                    "description": "include image_base64 — heavy payload, only when the LLM is about to render a card"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product_details",
        "description": (
            "Fetch the full product card by code or barcode (one ERP "
            "round-trip). Returns the family, every presentation with "
            "its own price/factor/barcode/discount, IVA description, and "
            "optionally the image (base64 PNG)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "product code or barcode"},
                "include_image": {"type": "boolean", "default": False},
            },
            "required": ["code"],
        },
    },
    {
        "name": "identify_customer",
        "description": (
            "Look a customer up by RUC, cédula, email, name or phone and "
            "return a merged profile (master + customer-extension fields "
            "like SALDO / CUPOC). Pass exactly one identifier. The tool "
            "walks 4 paths in order, falling back only if the prior path "
            "returned 0 rows:\n"
            "  1) per-chat cache (5 min TTL) — if you already identified "
            "this customer in the same conversation, the answer comes "
            "back instantly with ``from_cache=true``; you do NOT need to "
            "re-ask the user for their RUC. Pass NO identifier (or the "
            "same one) to get the cached partner;\n"
            "  2) exact-match in ENT — CIF tries the typed value, then "
            "with the SRI \"001\" suffix added or stripped (covers the "
            "common 10-digit empresa RUC shortcut); phone strips +593, "
            "spaces, dashes and the leading 0 before lookup; name is "
            "tried in PARALLEL on NAME and NOM_COM (commercial name);\n"
            "  3) Velneo WORDS index on ENT — fast token search using "
            "the same index the ERP UI uses, catches small typos "
            "(\"KLEIN\" → \"KLEINTURS\"). Hits carry ``_match_via='words'``;\n"
            "  4) pgvector RAG — last-resort fuzzy via partner_embeddings. "
            "Hits carry ``_match_via='rag'`` + ``_similarity`` score "
            "[0..1].\n"
            "When you see ``_match_via='words'`` or ``'rag'`` matches, "
            "DO NOT pick one silently — ask the user to confirm "
            "(\"encontré KLEINTURS Y REPRESENTACIONES, ¿es éste?\") "
            "before billing or quoting against that partner_id."
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
        "name": "update_partner",
        "description": (
            "Update an existing customer's contact info (email, phone, "
            "address) in ENT. CURRENT LIMITATION: Mepriga's API key has "
            "PATCH disabled, so this tool returns "
            "``error_code=not_supported_yet`` with a verbatim message "
            "the LLM must read to the customer (do NOT promise to do "
            "it manually). When the operator enables PATCH on the "
            "Velneo Seguridad panel, the tool starts working without "
            "code changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "address": {"type": "string"},
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "get_product_image",
        "description": (
            "Fetch the product image as base64 PNG (one ERP round-trip "
            "via the visor_datos process with dar_imagen=1). HEAVY "
            "payload (~400KB per product); only call when the customer "
            "explicitly asks to see a product photo or the bot is "
            "about to render a product card with image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "product code or barcode"},
            },
            "required": ["code"],
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
        "name": "request_otp",
        "description": (
            "Send a 6-digit verification code to the customer's email "
            "(ENT.MAIL_PRINCIPAL). REQUIRED before any financial tool "
            "(check_balance, get_customer_invoices, get_customer_payments) "
            "will return data. Only pass ``partner_id`` — channel and "
            "channel_user_id come from the chat context headers. If the "
            "customer already has a valid 24h session, this short-circuits "
            "with ``already_verified=true`` instead of spamming a new email."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ENT.ID (= ENT_ERP_CLI.ID) returned by identify_customer"},
                "email": {"type": "string", "description": "Optional override; default reads ENT.MAIL_PRINCIPAL"},
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "verify_otp",
        "description": (
            "Verify the 6-digit code the customer typed in the chat. On "
            "success, a 24h verified session opens for that "
            "(tenant, partner, channel) tuple so the financial tools "
            "stop refusing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer"},
                "code": {"type": "string", "description": "6-digit code"},
            },
            "required": ["partner_id", "code"],
        },
    },
    {
        "name": "get_customer_invoices",
        "description": (
            "List invoices for a client. REQUIRES a verified OTP session "
            "for the customer — call request_otp + verify_otp first if "
            "the gate rejects the call. Set ``include_lines=true`` to "
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
            "Customer balance summary (SALDO, DEUDASC, CUPOC, etc.). "
            "REQUIRES a verified OTP session — call request_otp + "
            "verify_otp first if the gate rejects the call. Set "
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
        "name": "get_customer_statement",
        "description": (
            "Customer account statement (estado de cuenta) — the open "
            "debt list with FECHA, VENCIMIENTO, REFERENCIA (SRI invoice "
            "number), DIAS, TOTAL_DEUDA, PAGADO, SALDO. By default only "
            "shows debts with saldo > 0; pass ``only_overdue=true`` to "
            "restrict to overdue rows (DIAS > 0). REQUIRES a verified "
            "OTP session — call request_otp + verify_otp first if the "
            "gate rejects the call. Equivalent to the Theos 'Detalle de "
            "Deudas del Cliente' form."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "only_with_balance": {"type": "boolean", "default": True},
                "only_overdue": {"type": "boolean", "default": False},
                "cutoff_date": {"type": "string", "description": "ISO YYYY-MM-DD; rows with FECHA after this are skipped"},
            },
            "required": ["client_id"],
        },
    },
    {
        "name": "get_customer_statement_pdf",
        "description": (
            "Same as get_customer_statement but returns the statement "
            "rendered as a PDF (base64). Use this when the customer "
            "asks for an official document or asks the bot to send "
            "their estado de cuenta. REQUIRES a verified OTP session. "
            "Output: { pdf_base64, pdf_filename, totals, item_count }; "
            "the channel layer attaches the PDF to the chat."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "only_with_balance": {"type": "boolean", "default": True},
                "only_overdue": {"type": "boolean", "default": False},
                "cutoff_date": {"type": "string"},
            },
            "required": ["client_id"],
        },
    },
    {
        "name": "get_customer_payments",
        "description": (
            "List customer payments. REQUIRES a verified OTP session — "
            "call request_otp + verify_otp first if the gate rejects "
            "the call. Set ``include_detail=true`` to also pull the "
            "payment-to-debt allocation."
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
    # ----------------------------------------------------------------
    # Internal support tools (admin_ops + admin_search).
    #
    # These are NOT OTP-protected: they are meant for the internal
    # support agent, which is authenticated by the channel layer
    # (Telegram group + admin allow-list in Lila's chat-id-map), not
    # by per-customer OTP. The agent's ``enabled_tools`` config decides
    # which agent gets to see them; the MCP itself exposes them flat.
    # ----------------------------------------------------------------
    {
        "name": "inspect_partner",
        "description": (
            "360° view of a customer for support diagnosis: ENT + "
            "ENT_ERP_CLI extension + flags (SIN_CREDITO, NO_VENDER, "
            "días vencidos) + a small snapshot of recent invoices, "
            "orders, open debts and payments. Identify by ``partner_id`` "
            "(ENT.ID) or by ``cif`` (RUC / cédula). Use this BEFORE "
            "diving into specific tools — it tells the agent which "
            "thread to pull."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ENT.ID = ENT_ERP_CLI.ID"},
                "cif": {"type": "string", "description": "RUC or cédula"},
            },
        },
    },
    {
        "name": "list_pending_invoices",
        "description": (
            "Active invoices with SALDO > 0 (unpaid). Delegates to "
            "Velneo proceso ``VENT_FACT_BUSQ_3P`` (newest first, "
            "server-side WORDS+PARTS index on NAME, date filter via "
            "FCH_FACT flag) with REST fallback. Each row comes back "
            "with: SERIE+SECUENCIA (combined into NRO_FAC), "
            "RAZONSOCIALCOMPRADOR + SRI_IDENTIFICACION (the customer), "
            "TOTAL/PAGADO/SALDO, and the SRI/Datil block: LAST_STATUS "
            "(human-readable: \"AUTORIZADO\", \"DEVUELTA\", \"NO "
            "AUTORIZADO\", \"PENDIENTE\"), VCACCESOSRI (49-digit clave "
            "de acceso), AUTORIZACION (autorización SRI number), "
            "TIENE_ELECTRONICA. Filters: ``customer_query`` for the "
            "WORDS index, ``client_id`` for FK filter, "
            "``salesperson_id``, ``branch_id``, date range "
            "(``date_from`` / ``date_to`` are honored server-side), "
            "and ``sri_status`` (substring match against LAST_STATUS — "
            "case-insensitive)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_query": {"type": "string"},
                "client_id": {"type": "integer"},
                "salesperson_id": {"type": "integer"},
                "branch_id": {"type": "integer"},
                "date_from": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "sri_status": {"type": "string",
                                "description": "substring of LAST_STATUS, e.g. \"AUTORIZADO\", \"DEVUELTA\""},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "list_recent_invoices",
        "description": (
            "Recent invoices, newest first. Same engine as "
            "``list_pending_invoices`` minus the SALDO > 0 filter — "
            "use for \"qué pasó hoy con KLEINTURS\" or \"facturas "
            "devueltas por SRI esta semana\". Filter by SRI state via "
            "``sri_status`` (substring match against LAST_STATUS, "
            "e.g. \"DEVUELTA\", \"NO AUTORIZADO\", \"PENDIENTE\", "
            "\"AUTORIZADO\")."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_query": {"type": "string"},
                "client_id": {"type": "integer"},
                "salesperson_id": {"type": "integer"},
                "branch_id": {"type": "integer"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "sri_status": {"type": "string"},
                "include_off": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "find_invoice",
        "description": (
            "Flexible invoice lookup. Pass ONE of: ``invoice_id`` "
            "(VENT_FACT_VENT.ID), ``nro_fac`` (SRI document number "
            "\"001-001-565825\"), or free-text ``query``. The query "
            "path auto-detects SRI shape, then numeric secuencia, then "
            "falls back to ``filter[words]=`` over NAME so the same "
            "tool can find by partial customer name, partial SRI "
            "access key, or REFERENCIA. Each invoice row comes back "
            "with the derived NRO_FAC + the SRI/Datil block "
            "(LAST_STATUS, VCACCESOSRI, AUTORIZACION, KEY)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "SRI number, secuencia, or free text"},
                "invoice_id": {"type": "integer"},
                "nro_fac": {"type": "string",
                            "description": "canonical \"001-001-565825\""},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "list_documents_window",
        "description": (
            "Multi-document cross-section in a date window. Pulls in "
            "parallel a customer's recent activity across types: "
            "invoices, orders, debts, payments, credit notes (NC), "
            "withholdings (RE). Filter by ``customer_query`` "
            "(WORDS index — \"klein\" matches the customer's invoices "
            "via the denormalized NAME), or ``client_id``, or both. "
            "Pass ``types=[...]`` to narrow the doc types pulled. "
            "Returns ``{documents: {invoices: {count, items}, ...}}``."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_query": {"type": "string"},
                "client_id": {"type": "integer"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["invoices", "orders", "debts",
                                 "payments", "credit_notes", "withholdings"],
                    },
                },
                "limit_per_type": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "search_by_amount",
        "description": (
            "Find docs whose TOTAL (invoices/credit notes) or VALOR "
            "(payments) sits in ``[amount_min, amount_max]``. Velneo "
            "has no range operator; we apply the amount filter "
            "in-memory after pulling a wide window. PASS a "
            "``customer_query`` or a date window — without narrowing, "
            "the result is limited to the first page of the table and "
            "may miss your target. ``doc_type`` is one of "
            "\"invoices\", \"payments\", \"credit_notes\"."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string",
                              "enum": ["invoices", "payments", "credit_notes"],
                              "default": "invoices"},
                "amount_min": {"type": "number"},
                "amount_max": {"type": "number"},
                "customer_query": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "search_invoice_lines_by_product",
        "description": (
            "INV_MOVIMIENTOS rows belonging to a sales invoice "
            "(VENT_FACT_VENT != 0) and a specific PRODUCTOS.ID. Used "
            "for return / claim research: \"en qué facturas vendimos "
            "este producto\". Each line carries its parent "
            "VENT_FACT_VENT id so the agent can chain "
            "``get_invoice_detail`` to enrich."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "list_invoice_lines_window",
        "description": (
            "Líneas de venta (INV_MOVIMIENTOS) en una ventana de "
            "fechas — la herramienta correcta para \"detalle de "
            "ventas del día / semana / mes\". Delega al proceso "
            "VENT_FACT_MOV_BUSQ_3P con SUCURSAL + FCH_FACT=1 + "
            "FCH_DES/HST (filtrado server-side). Filtra por cliente "
            "vía ``customer_query`` (WORDS sobre NAME, opcional) o "
            "``branch_id``. ``date_basis='conta'`` cambia a fecha "
            "contable (FCH_CONTA=1) en vez de fecha emisión. El "
            "proceso topa en 1000 líneas por llamada — si "
            "``truncated=true`` en el response, el LLM debe narrow "
            "la ventana o agregar customer_query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "date_to": {"type": "string"},
                "customer_query": {"type": "string"},
                "branch_id": {"type": "integer"},
                "date_basis": {"type": "string",
                                "enum": ["fact", "conta"],
                                "default": "fact"},
                "include_off": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
            },
        },
    },
    {
        "name": "list_credit_notes",
        "description": (
            "Notas de crédito de ventas (VENT_NOTA_CRED), newest "
            "first. Each row carries VENT_FACT_VENT — the parent "
            "factura — plus the SRI block (LAST_STATUS, VCACCESOSRI, "
            "AUTORIZACION, KEY). Filter by customer via "
            "``customer_query`` (WORDS) or ``client_id`` (FK), plus "
            "optional date window and ``sri_status`` (substring of "
            "LAST_STATUS — \"AUTORIZADO\", \"DEVUELTA\", etc.)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_query": {"type": "string"},
                "client_id": {"type": "integer"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "sri_status": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "list_withholdings",
        "description": (
            "Comprobantes de retención emitidos a proveedores "
            "(COMP_RETENCIONES). Devuelve los montos retenidos por "
            "categoría (RET_FUE1..3 fuente, RET_IVA1..2 IVA) más el "
            "código SRI (CODE_RET_FUE1 / CODE_RET_IVA1). El número "
            "del comprobante vive en NAME (SERIE/SECUENCIA están "
            "bloqueados de proyección para esta tabla). Filtra por "
            "proveedor via ``supplier_query`` (WORDS sobre NAME) o "
            "``supplier_id`` (ENT_ERP_PROV)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supplier_query": {"type": "string"},
                "supplier_id": {"type": "integer"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "sri_status": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "list_purchase_invoices",
        "description": (
            "Facturas de compras (CONT_COMPRAS). Espejo de "
            "``list_recent_invoices`` para el lado proveedor. Filtra "
            "por proveedor via ``supplier_query`` o ``supplier_id`` "
            "(ENT_ERP_PROV). ``only_unpaid=true`` retiene rows con "
            "SALDO > 0 (vista de cuentas por pagar)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supplier_query": {"type": "string"},
                "supplier_id": {"type": "integer"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "sri_status": {"type": "string"},
                "only_unpaid": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "list_supplier_debts",
        "description": (
            "Deudas a proveedores (COMP_DEUD_PROV) — el equivalente "
            "compras de ``list_pending_invoices``. Filtra por "
            "proveedor via ``supplier_query`` (WORDS sobre NAME, "
            "que carga RUC + razón social del proveedor inline — "
            "\"DISPROINCO\" → 2 deudas $1124.74 + $1073.01) o "
            "``supplier_id`` (ENT_ERP_PROV). Pasa ``cont_compras_id`` "
            "para ver las deudas de UNA factura específica. "
            "``only_with_balance=true`` (default) muestra solo SALDO "
            "> 0; ``only_overdue=true`` filtra DIAS_VENCIDOS > 0. "
            "Devuelve totales agregados + breakdown por proveedor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "supplier_query": {"type": "string"},
                "supplier_id": {"type": "integer"},
                "cont_compras_id": {"type": "integer"},
                "only_with_balance": {"type": "boolean", "default": True},
                "only_overdue": {"type": "boolean", "default": False},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "customer_full_view",
        "description": (
            "Vista panorámica de un cliente en una sola llamada. "
            "Resuelve la identidad (mismo path order que "
            "``identify_customer`` — exact / WORDS / RAG) y luego "
            "trae en PARALELO la actividad de los últimos ``days`` "
            "(default 90): facturas + órdenes + deudas + cobros + "
            "notas de crédito, todo del mismo cliente. Devuelve "
            "totales agregados (saldo abierto, # vencidas) sobre "
            "los items. Si el match es por WORDS o RAG, retorna "
            "``error_code=needs_disambiguation`` con los candidatos "
            "para que el LLM pida confirmación antes de exponer "
            "datos del cliente equivocado."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_query": {"type": "string"},
                "client_id": {"type": "integer"},
                "cif": {"type": "string"},
                "days": {"type": "integer", "default": 90, "minimum": 7, "maximum": 365},
            },
        },
    },
    {
        "name": "list_invoices_pending_dispatch",
        "description": (
            "Facturas con líneas pendientes de despacho "
            "(CAN_NO_DESP != 0). Usa el flag ``MOSTRAR_POR_DESPACHAR`` "
            "del proceso ``ERP_APP/VENT_FACT_BUSQ_3P``; no hay "
            "equivalente REST. Si el permiso de proceso no está "
            "habilitado el tool devuelve ``error_code=proceso_permission_denied``."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_query": {"type": "string"},
                "branch_id": {"type": "integer"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "strict": {"type": "boolean", "default": True,
                           "description": "true = sólo facturas con CAN_NO_DESP > 0"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "get_invoice_detail",
        "description": (
            "Single invoice with everything bolted on: header + line "
            "items (INV_MOVIMIENTOS) + debt rows it generated "
            "(VENT_DEUD_CLIE) + payment applications against those "
            "debts (DETALLE_COBROS). Identify by ``invoice_id`` "
            "(VENT_FACT_VENT.ID) or ``nro_fac`` (SRI invoice number "
            "like ``001-001-581914``)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "integer"},
                "nro_fac": {"type": "string"},
            },
        },
    },
    {
        "name": "list_recent_stock_movements",
        "description": (
            "Latest ``INV_MOVIMIENTOS`` rows narrowed by product "
            "and/or bodega. Used for stock forensics — \"por qué "
            "el stock dice X cuando ayer hicimos Y\". At least ONE "
            "of ``product_id``, ``bodega_id`` is required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer"},
                "bodega_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
        },
    },
    {
        "name": "check_stock",
        "description": (
            "Stock disponibility for one or many products in one call. "
            "Reads PRODUCTOS.EXS (total existencia) for each id. With "
            "``include_warehouses=true`` also returns per-bodega "
            "breakdown from the denormalized ``EXS_BOD1..12`` / "
            "``INV_BODEGA1..12`` columns on the master row (Theos-"
            "native pattern, no separate EXISTENCIAS join). Identify "
            "products by ``product_ids`` (Velneo ids) or by ``codes`` "
            "(CODIGO strings). For *forensics* on a single product "
            "(stock vs movement history) use "
            "``list_recent_stock_movements`` after this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "include_warehouses": {
                    "type": "boolean",
                    "default": False,
                    "description": "include per-bodega breakdown",
                },
            },
        },
    },
    {
        "name": "search_velneo",
        "description": (
            "Whitelisted lookup against a single Velneo entry-point "
            "table with optional FK / child-collection expansion. The "
            "catch-all for support questions the dedicated tools do "
            "not cover. Allowed tables: PRODUCTOS, VENT_FACT_VENT, "
            "VENT_ORDEN_VENTA, VENT_DEUD_CLIE, VENT_COBR_DEUD. Allowed "
            "expansions per table — VENT_FACT_VENT: client / lines / "
            "debts. VENT_ORDEN_VENTA: client / lines. VENT_DEUD_CLIE: "
            "client / invoice / payments. VENT_COBR_DEUD: client / "
            "applications / forms. PRODUCTOS: existencias. Filters are "
            "exact-match (no LIKE); for free-text product search use "
            "``search_products`` instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "enum": [
                        "PRODUCTOS",
                        "VENT_FACT_VENT",
                        "VENT_ORDEN_VENTA",
                        "VENT_DEUD_CLIE",
                        "VENT_COBR_DEUD",
                    ],
                },
                "filters": {
                    "type": "object",
                    "description": "FIELD=value pairs (exact match)",
                    "additionalProperties": True,
                },
                "expand": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "FK / child aliases to expand inline",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Field projection; omit to use the table's default",
                },
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
            },
            "required": ["table"],
        },
    },
]


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

ToolFn = Callable[..., Awaitable[dict[str, Any]]]

_DISPATCH: dict[str, tuple[str, ToolFn]] = {
    "search_products": ("products.search_products", products.search_products),
    "get_product_details": ("products.get_product_details", products.get_product_details),
    "identify_customer": ("partners.identify_customer", partners.identify_customer),
    "create_partner": ("partners.create_partner", partners.create_partner),
    "update_partner": ("partners.update_partner", partners.update_partner),
    "get_product_image": ("products.get_product_image", products.get_product_image),
    "create_quotation": ("sales.create_quotation", sales.create_quotation),
    "get_quotation": ("sales.get_quotation", sales.get_quotation),
    "list_quotations": ("sales.list_quotations", sales.list_quotations),
    "request_otp": ("otp_tools.request_otp", otp_tools.request_otp),
    "verify_otp": ("otp_tools.verify_otp", otp_tools.verify_otp),
    "get_customer_invoices": ("invoices.get_customer_invoices", invoices.get_customer_invoices),
    "check_balance": ("invoices.check_balance", invoices.check_balance),
    "get_customer_statement": ("invoices.get_customer_statement", invoices.get_customer_statement),
    "get_customer_statement_pdf": ("invoices.get_customer_statement_pdf", invoices.get_customer_statement_pdf),
    "get_customer_payments": ("payments.get_customer_payments", payments.get_customer_payments),
    # Internal support tools — no OTP gate; the agent's enabled_tools
    # config (in tenant_<slug>.agents) decides which agent gets them.
    "inspect_partner": ("admin_ops.inspect_partner", admin_ops.inspect_partner),
    "list_pending_invoices": ("admin_ops.list_pending_invoices", admin_ops.list_pending_invoices),
    "list_recent_invoices": ("admin_ops.list_recent_invoices", admin_ops.list_recent_invoices),
    "get_invoice_detail": ("admin_ops.get_invoice_detail", admin_ops.get_invoice_detail),
    "list_invoices_pending_dispatch": ("admin_ops.list_invoices_pending_dispatch", admin_ops.list_invoices_pending_dispatch),
    "find_invoice": ("admin_ops.find_invoice", admin_ops.find_invoice),
    "list_documents_window": ("admin_ops.list_documents_window", admin_ops.list_documents_window),
    "search_by_amount": ("admin_ops.search_by_amount", admin_ops.search_by_amount),
    "search_invoice_lines_by_product": ("admin_ops.search_invoice_lines_by_product", admin_ops.search_invoice_lines_by_product),
    "list_invoice_lines_window": ("admin_ops.list_invoice_lines_window", admin_ops.list_invoice_lines_window),
    "list_credit_notes": ("admin_ops.list_credit_notes", admin_ops.list_credit_notes),
    "list_withholdings": ("admin_ops.list_withholdings", admin_ops.list_withholdings),
    "list_purchase_invoices": ("admin_ops.list_purchase_invoices", admin_ops.list_purchase_invoices),
    "list_supplier_debts": ("admin_ops.list_supplier_debts", admin_ops.list_supplier_debts),
    "customer_full_view": ("admin_ops.customer_full_view", admin_ops.customer_full_view),
    "list_recent_stock_movements": ("admin_ops.list_recent_stock_movements", admin_ops.list_recent_stock_movements),
    "check_stock": ("products.check_stock", products.check_stock),
    "search_velneo": ("admin_search.search_velneo", admin_search.search_velneo),
}


# Tools that refuse to return data until the customer's identity has
# been verified through request_otp + verify_otp. The gate runs in
# _execute_tool, BEFORE the dispatch hits the underlying function.
OTP_PROTECTED_TOOLS: frozenset[str] = frozenset({
    "check_balance",
    "get_customer_invoices",
    "get_customer_statement",
    "get_customer_statement_pdf",
    "get_customer_payments",
})
# Note: get_invoice_detail / inspect_partner / list_pending_invoices /
# list_recent_invoices / list_recent_stock_movements / search_velneo
# are admin/staff tools without OTP. They MUST stay out of customer-
# facing agents' enabled_tools (Anny). Only an internal support agent
# (not active on Mepriga yet) should have access.


def _parse_allowed_tools(request: Request) -> set[str] | None:
    raw = request.headers.get("x-allowed-tools") or request.headers.get("X-Allowed-Tools")
    if not raw:
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


def _make_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _read_channel_ctx(request: Request) -> tuple[str, str]:
    """Pull X-Channel / X-Channel-User-Id from request headers."""
    channel = (
        request.headers.get("x-channel")
        or request.headers.get("X-Channel")
        or ""
    ).strip().lower()
    cuid = (
        request.headers.get("x-channel-user-id")
        or request.headers.get("X-Channel-User-Id")
        or ""
    ).strip()
    return channel, cuid


async def _execute_tool(request: Request, name: str, args: dict[str, Any]) -> str:
    entry = _DISPATCH.get(name)
    if entry is None:
        return json.dumps({"success": False, "error": f"unknown tool {name!r}"}, ensure_ascii=False)
    _label, fn = entry

    cfg = await get_tenant_config(request)
    channel, channel_user_id = _read_channel_ctx(request)

    # OTP gate — refuse financial tools until a verified session exists.
    if name in OTP_PROTECTED_TOOLS:
        partner_id = args.get("client_id") or args.get("partner_id")
        try:
            partner_id_int = int(partner_id) if partner_id else 0
        except (TypeError, ValueError):
            partner_id_int = 0
        if not partner_id_int:
            # Important: the LLM (deepseek-v4-pro) can misread a
            # "client_id required" message as "ask the customer for
            # their email". The wording below is verbose on purpose
            # — it tells the LLM EXACTLY what to do next so it does
            # not invent its own follow-up.
            return json.dumps({
                "success": False,
                "error_code": "needs_customer_identification",
                "next_action": "identify_customer",
                "error": (
                    "ANTES de mostrar datos financieros (saldo, facturas, "
                    "pagos, estado de cuenta) DEBES tener el partner_id del "
                    "cliente identificado en este chat. AHORA NO LO TIENES. "
                    "PASOS A SEGUIR (en este turno, en orden):\n"
                    "1) Si el cliente AÚN no te ha dado cédula o RUC, "
                    "PÍDELE: 'Para consultar tu saldo necesito identificarte. "
                    "¿Me compartes tu cédula (10 dígitos) o RUC (13 dígitos)?'\n"
                    "2) Cuando el cliente te dé el número, llama la tool "
                    "identify_customer(ruc=<numero>) o "
                    "identify_customer(cedula=<numero>). Esa tool devuelve "
                    "partner_id en el top-level del JSON.\n"
                    "3) Solo DESPUÉS de obtener un partner_id válido, "
                    "llama esta tool de nuevo pasándolo como client_id.\n"
                    "PROHIBIDO: pedir email, asumir identidad desde memoria "
                    "del chat, o decir 'tu ficha' sin haber llamado "
                    "identify_customer primero."
                ),
            }, ensure_ascii=False)
        if not channel:
            return json.dumps({
                "success": False,
                "error_code": "missing_channel",
                "error": (
                    "El orchestrator no envio X-Channel. Datos financieros "
                    "no se pueden mostrar sin contexto de canal."
                ),
            }, ensure_ascii=False)
        has_session = await check_session(cfg.tenant_id, partner_id_int, channel)
        if not has_session:
            return json.dumps({
                "success": False,
                "error_code": "otp_required",
                "next_action": "request_otp",
                "error": OTP_REQUIRED_MSG,
            }, ensure_ascii=False)

    # OTP tools need channel context injected from headers (the LLM
    # should NOT have to know how to address the chat session).
    if name in ("request_otp", "verify_otp"):
        args = dict(args)
        args.setdefault("channel", channel)
        if name == "request_otp":
            args.setdefault("channel_user_id", channel_user_id)

    # identify_customer uses channel_user_id for the per-chat partner
    # cache (skips re-lookup when the agent re-asks about the same
    # customer within 5 minutes). Hidden from the LLM schema.
    if name == "identify_customer":
        args = dict(args)
        args.setdefault("channel_user_id", channel_user_id)

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
