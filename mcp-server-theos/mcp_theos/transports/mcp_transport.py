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
    inventory,
    invoices,
    otp_tools,
    partners,
    payments,
    products,
    sales,
    signature_queue,
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
            "Resolve a CUSTOMER (ENT_ERP_CLI) from a free-text query.\n"
            "**IMPORTANT — this tool is CUSTOMERS ONLY.** Do NOT call "
            "it for suppliers / proveedores / cuentas por pagar / "
            "compras — for those use ``identify_supplier``. The RAG "
            "partner_embeddings only indexes customers; calling this "
            "for a supplier query returns the WRONG party silently.\n\n"
            "Look a customer up by RUC, cédula, email, name or phone "
            "and return a merged profile (master + customer-extension "
            "fields like SALDO / CUPOC). Pass exactly one identifier. "
            "The tool walks 4 paths in order, falling back only if the "
            "prior path returned 0 rows:\n"
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
            "``product_id`` and ``quantity``. Optional per-line: "
            "``presentation_codbar`` (a specific empaque codbar like "
            "``'01PPQ'`` for QUINTAL X 95 instead of the default unit "
            "empaque), ``unit_price`` (overrides the tariff price — sets "
            "PRECIO_BRUTO_EMPAQUE + PRECIO_ACORDADO=true). Header EMP / "
            "SUC / INV_BODEGA / INV_TARIFAS / payment fields are filled "
            "from the tenant's ``erp_api_extra`` config when omitted."
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
                            "presentation_codbar": {
                                "type": "string",
                                "description": (
                                    "Optional empaque codbar (e.g. '01PPQ' "
                                    "for QUINTAL). Default = the FACTOR=1 "
                                    "empaque (single unit)."
                                ),
                            },
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
        "name": "render_quotation_pdf",
        "description": (
            "Render a quotation (proforma) as a PDF and return it base64-"
            "encoded. The chat layer decodes the bytes and attaches the "
            "file to the customer's message. Call AFTER create_quotation "
            "succeeds, so the customer gets a clean record of what was "
            "just quoted. Tool name matches the canonical odoo name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "add_quotation_line",
        "description": (
            "Append a single line to an existing quotation. Use this "
            "when the customer says 'agrega también X', 'súmame Y', or "
            "after browsing the catalog and picking another product to "
            "the active quotation. Inherits EMP / SUC / INV_BODEGA / "
            "INV_TARIFAS from the order header. Optional "
            "``presentation_codbar`` picks a non-unit empaque (e.g. "
            "'01PPQ' for QUINTAL); ``unit_price`` overrides the tariff "
            "price (sets PRECIO_BRUTO_EMPAQUE + PRECIO_ACORDADO=true)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "product_id": {"type": "integer"},
                "quantity": {"type": "number"},
                "presentation_codbar": {"type": "string"},
                "unit_price": {"type": "number"},
            },
            "required": ["order_id", "product_id", "quantity"],
        },
    },
    {
        "name": "update_quotation_line",
        "description": (
            "Update an existing quotation line — change the quantity, "
            "unit price, or switch to a different empaque via "
            "``presentation_codbar``. Implemented as DELETE+POST "
            "under the hood (the API key does not grant PATCH on "
            "VENT_ORDEN_MOVIMIENTOS), which keeps the NUM_LINEA slot "
            "but yields a new ``ID`` — the response shows both."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "integer"},
                "quantity": {"type": "number"},
                "unit_price": {"type": "number"},
                "presentation_codbar": {"type": "string"},
            },
            "required": ["line_id"],
        },
    },
    {
        "name": "remove_quotation_line",
        "description": (
            "Remove a quotation line. The parent VENT_ORDEN_VENTA "
            "header stays intact, so the customer can keep adding "
            "lines or close the cotización."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"line_id": {"type": "integer"}},
            "required": ["line_id"],
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
        "name": "identify_supplier",
        "description": (
            "Resolve a PROVEEDOR (NOT a customer) from a free-text "
            "query. Use this — NOT ``identify_customer`` — whenever "
            "the user asks about a supplier: \"cuánto le debo a X\", "
            "\"cuentas por pagar de X\", \"compras a X\", \"deuda a "
            "X\", \"proveedor X\". ``identify_customer`` only knows "
            "customers (ENT_ERP_CLI + partner_embeddings RAG); using "
            "it for a supplier query returns the WRONG party. This "
            "tool walks COMP_DEUD_PROV.NAME (WORDS index — name + "
            "RUC denormalized inline) and falls back to CONT_COMPRAS "
            "for suppliers without open debts. Returns each match "
            "with supplier_id (ENT_ERP_PROV), display_name, ruc, "
            "open_debt_saldo and recent_invoice_count for "
            "disambiguation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "supplier name, RUC, or token"},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
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
        "name": "get_open_debts",
        "description": (
            "Customer debts with SALDO > 0 via the native Velneo "
            "``_query/deudas_cli_con_saldo`` (server-side SALDO+OFF "
            "pre-filter — accurate, won't miss rows past page 1). "
            "Filter por ``client_id`` (más rápido) o ``customer_query`` "
            "(WORDS fallback in-memory). Devuelve totales agregados "
            "más un ``by_age_bucket`` (0_30, 31_60, 61_90, 90_plus, "
            "not_yet_due) para reportes de antigüedad."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "integer"},
                "customer_query": {"type": "string"},
                "only_overdue": {"type": "boolean", "default": False},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
            },
        },
    },
    {
        "name": "velneo_query",
        "description": (
            "Generic Velneo Búsqueda invoker via ``_query/<name>``. "
            "Whitelisted to: deudas_cli_con_saldo, vent_deud_clie_busq*, "
            "vent_fact_vent_busq*, productos_busq, presen_busq, "
            "buscar_cod_bar, cod_bar_parts, corte_deudas_* (verified "
            "vs untested status returned in the response). Pass "
            "``filters`` for REST-style ``filter[FIELD]=value`` and/or "
            "``params`` for proceso-style ``param[VAR]=value`` — some "
            "Búsquedas accept both. Use this when none of the dedicated "
            "tools fits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "filters": {"type": "object", "additionalProperties": True},
                "params": {"type": "object", "additionalProperties": True},
                "page_size": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                "page": {"type": "integer", "default": 1, "minimum": 1},
            },
            "required": ["name"],
        },
    },
    {
        "name": "partner_360",
        "description": (
            "Audit-grade panoramic of a customer: ENT + ENT_ERP_CLI "
            "extension + flags + deudas abiertas vía "
            "``_query/deudas_cli_con_saldo`` + aging buckets "
            "(0_30, 31_60, 61_90, 90_plus, not_yet_due) + últimos "
            "10 cobros. Identifica por partner_id, cif o "
            "customer_query (mismo orden de paths que "
            "identify_customer). Si el match es por WORDS o RAG, "
            "retorna ``error_code=needs_disambiguation``."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer"},
                "cif": {"type": "string"},
                "customer_query": {"type": "string"},
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
        "name": "sales_quick_summary",
        "description": (
            "Resumen agregado de ventas (y opcionalmente notas de credito) "
            "en JSON, SIN generar XLSX. USAR ESTE TOOL cuando el usuario "
            "pida un analisis/resumen GERENCIAL EN VIVO en el chat — ej. "
            "'como van las ventas de hoy', 'que cajas vendieron mas', "
            "'top clientes de la semana', 'ventas por hora', 'familias "
            "que mas vendieron', 'ventas vs notas de credito de hoy', "
            "'saldo neto de hoy'.\n\n"
            "**Cuando NO usar este tool**: si el usuario pide 'informe', "
            "'reporte', 'detalle completo' o cualquier cosa que sugiera "
            "un archivo descargable, usar ``generate_sales_report`` (que "
            "produce XLSX adjuntado al chat).\n\n"
            "**Dimensiones que devuelve**:\n"
            "  totals: pvp, neto, n_lineas, n_facturas, ticket_promedio_pvp,\n"
            "          nc_total, saldo_neto (si include_credit_notes=True)\n"
            "  por_hora: [{hora, pvp, n_facturas, n_lineas}] (HH:00 ECU)\n"
            "  por_familia: [{familia, pvp, pct, n_lineas}] desc\n"
            "  por_bodega: [{bodega, pvp, pct, n_facturas, n_lineas}] desc\n"
            "  por_pto_emision: [{establecimiento_pto, pvp, n_facturas}] desc\n"
            "  top_clientes: [{nombre, cif, pvp, n_facturas, n_lineas}] (N=10)\n"
            "  credit_notes: {total_nc, subtotal_nc, iva_nc, n_ncs,\n"
            "                 por_pto_emision}\n\n"
            "Sincronico (cabe en 120s para 1-3 dias). Para rangos mas "
            "amplios va a tardar pero aun cabe; si truncated=true narrow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "YYYY-MM. Para 'ventas de mayo 2026'.",
                },
                "year": {
                    "type": "string",
                    "description": "YYYY. Para 'ventas de 2026'.",
                },
                "date_from": {
                    "type": "string",
                    "description": "ISO YYYY-MM-DD. Default = today (ECU).",
                },
                "date_to": {
                    "type": "string",
                    "description": "ISO YYYY-MM-DD. Default = date_from.",
                },
                "sucursal": {"type": "string"},
                "top_n_clientes": {
                    "type": "integer", "default": 10, "minimum": 3, "maximum": 50,
                },
                "include_credit_notes": {
                    "type": "boolean", "default": True,
                    "description": (
                        "Si True (default), incluye total de NCs del rango y "
                        "expone saldo_neto = ventas_pvp - nc_total. Tambien "
                        "agrega ``por_dia_combinado`` con neto_ajustado por "
                        "dia (neto - NC del dia) y ``avg_neto_ajustado_por_dia``."
                    ),
                },
                "cutoff_hour": {
                    "type": "integer", "minimum": 0, "maximum": 23,
                    "description": (
                        "Corte horario (0-23) aplicado UNIFORMEMENTE a TODOS "
                        "los dias del rango. Las lineas con FECHA_CONTA.hour "
                        "> cutoff_hour se descartan. Usar para comparar "
                        "'ventas a esta misma hora' contra dias anteriores "
                        "(apples-to-apples). El response trae ``por_dia`` "
                        "con totales hasta esa hora por cada dia."
                    ),
                },
                "match_current_hour": {
                    "type": "boolean", "default": False,
                    "description": (
                        "Si True, ignora ``cutoff_hour`` y usa la hora actual "
                        "ECU como cutoff. ATAJO para la consulta tipica "
                        "'cuanto se ha vendido cada dia comparado a esta "
                        "misma hora'. El response trae ``cutoff_hour_used`` "
                        "con el valor resultante para que la respuesta lo "
                        "mencione (ej. '...hasta 13h00')."
                    ),
                },
            },
        },
    },
    {
        "name": "sales_evolution_chart",
        "description": (
            "Genera un GRAFICO FOCALIZADO de EVOLUCION temporal (1 solo "
            "panel, no el dashboard 2x2) y lo sube inline al chat. USAR "
            "cuando el usuario pregunta como evolucionan las ventas en el "
            "TIEMPO — 'como va el dia', 'ventas por hora', 'compara los "
            "ultimos 3 dias', 'evolucion del mes', 'tendencia diaria', "
            "'ayer vs anteayer'.\n\n"
            "El tool detecta automaticamente la mejor forma del grafico "
            "segun la cantidad de dias del rango (modo 'auto', default):\n"
            "  - 1 dia                 -> barras por hora del dia\n"
            "  - 2 a 7 dias             -> lineas por hora, una linea por "
            "dia (ideal comparar patron intradia)\n"
            "  - mas de 7 dias          -> barras por dia (tendencia)\n\n"
            "Si el usuario pide explicitamente un modo (ej 'compara hora "
            "por hora los 3 dias') pasa el mode correspondiente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM"},
                "year": {"type": "string", "description": "YYYY"},
                "date_from": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "mode": {
                    "type": "string",
                    "enum": ["auto", "single_day_hourly",
                             "multi_day_hourly_compare", "daily_trend"],
                    "default": "auto",
                    "description": (
                        "auto recommended. Override solo si el usuario "
                        "pide explicitamente la forma."
                    ),
                },
                "sucursal": {"type": "string"},
                "metric": {
                    "type": "string",
                    "enum": ["pvp", "neto"],
                    "default": "pvp",
                    "description": (
                        "pvp = ventas brutas. neto = sin impuestos. "
                        "Usar 'neto' cuando el usuario pidio comparativa de "
                        "neto/sin IVA dia por dia."
                    ),
                },
                "cutoff_hour": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 23,
                    "description": (
                        "Corta TODOS los dias del rango uniformemente hasta "
                        "esta hora ECU (apples-to-apples). Solo afecta modes "
                        "daily_trend y multi_day_hourly_compare."
                    ),
                },
                "match_current_hour": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Si True equivale a cutoff_hour=hora actual ECU. "
                        "Usar cuando el gerente pregunta 'ventas dia por dia "
                        "a esta misma hora' o 'lo que va del dia comparado'."
                    ),
                },
                "deliver_to_chat": {
                    "type": "string",
                    "description": "REQUERIDO. Telegram chat_id.",
                },
            },
            "required": ["deliver_to_chat"],
        },
    },
    {
        "name": "sales_dashboard_chart",
        "description": (
            "Genera un dashboard visual PNG (2x2 paneles: familia, hora, "
            "contado vs credito, top cajas) y lo sube DIRECTO al chat de "
            "Telegram via Bot API sendPhoto. USAR cuando el usuario pida "
            "'panorama visual', 'dashboard', 'graficos', 'visualmente', "
            "'mostrame las ventas', 'imagen del resumen', 'chart' del "
            "rango. Mucho mas digerible que una pared de tablas para "
            "gerencia.\n\n"
            "Devuelve un resumen JSON corto (totales + cache_stats) sin "
            "binario — la imagen va inline al chat. Lila puede narrar 1-2 "
            "frases de contexto al lado del chart (NO repetir el detalle "
            "del JSON, que ya esta en la imagen)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM"},
                "year": {"type": "string", "description": "YYYY"},
                "date_from": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "sucursal": {"type": "string"},
                "include_credit_notes": {"type": "boolean", "default": True},
                "deliver_to_chat": {
                    "type": "string",
                    "description": (
                        "REQUERIDO. Telegram chat_id (ej '-5248384291' "
                        "para grupo Soporte Mepriga). El PNG se sube a ese "
                        "chat como imagen inline (no como archivo)."
                    ),
                },
            },
            "required": ["deliver_to_chat"],
        },
    },
    {
        "name": "generate_sales_report",
        "description": (
            "Genera el INFORME DE VENTAS DIARIAS (XLSX) que el operador "
            "de Mepriga produce manualmente desde el vClient. Dos hojas: "
            "``INFORME`` (pivot Familia x Bodega por fecha con totales) "
            "y ``VENTAS_DETALLE`` (29 columnas raw a nivel de línea). "
            "Por defecto trae HOY (Ecuador timezone) si no se pasa "
            "rango. Acepta ``date_from`` / ``date_to`` en ISO "
            "YYYY-MM-DD para cualquier ventana (día, semana, mes, "
            "rango libre). Cap de líneas: 5000 (suficiente para 1 día; "
            "si pidieras un mes muy ocupado y truncated=true, narrow "
            "el rango).\n\n"
            "**Entrega del archivo** — DOS modos:\n"
            "1. RECOMENDADO para el agente: pasá "
            "``deliver_to_chat=\"<telegram_chat_id>\"``. El tool sube el "
            "XLSX DIRECTO al chat via Bot API sendDocument y devuelve "
            "``{success, delivered: true, delivered_to_chat, totals, "
            "n_lines, xlsx_filename, xlsx_size_kb}`` sin base64. El "
            "usuario ve el archivo como adjunto real. Para Mepriga el "
            "chat_id del grupo Soporte Mepriga es ``-5248384291``.\n"
            "2. Sin ``deliver_to_chat`` (modo legacy / cron): devuelve "
            "``xlsx_base64`` + ``xlsx_filename`` para que el caller "
            "haga el upload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": (
                        "USAR ESTE PARAMETRO cuando el usuario pida un mes "
                        "completo ('ventas de mayo', 'informe del mes de "
                        "mayo 2026', 'mes completo'). Formato YYYY-MM (ej. "
                        "'2026-05'). El tool resuelve a date_from=YYYY-MM-01 "
                        "y date_to=ultimo dia del mes (28/29/30/31). USAR "
                        "ESTO en vez de date_from/date_to manuales — "
                        "garantiza que se cubre el mes ENTERO sin importar "
                        "que dia es hoy."
                    ),
                },
                "year": {
                    "type": "string",
                    "description": (
                        "USAR ESTE PARAMETRO cuando el usuario pida un anio "
                        "entero ('ventas de 2026'). Formato YYYY. Resuelve "
                        "a date_from=YYYY-01-01 y date_to=YYYY-12-31."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": (
                        "ISO YYYY-MM-DD. Default = today (ECU). Usar solo "
                        "cuando se necesita un rango libre que NO sea un "
                        "mes o anio completo. Para meses usar `month`, "
                        "para anios usar `year`."
                    ),
                },
                "date_to": {
                    "type": "string",
                    "description": "ISO YYYY-MM-DD. Default = date_from",
                },
                "sucursal": {"type": "string"},
                "max_rows": {"type": "integer", "default": 5000, "minimum": 100, "maximum": 200000},
                "deliver_to_chat": {
                    "type": "string",
                    "description": (
                        "Telegram chat_id donde subir el XLSX directamente "
                        "(ej. '-5248384291' para el grupo Soporte Mepriga). "
                        "Cuando se pasa, el tool entrega el archivo via Bot "
                        "API y devuelve un resumen corto sin base64. Cuando "
                        "se omite, el tool devuelve base64 (modo legacy / cron)."
                    ),
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
    {
        "name": "signature_queue_status",
        "description": (
            "Cuenta TODAS las filas de COLA_DOCS_FIRMAR agrupadas por "
            "ESTADO_FEAP, con label humano (Autorizado, Enviado al "
            "servidor, Puesto en cola, Error, etc.) y conteo. Opcional: "
            "incluye hasta N ejemplos por estado (los más recientes por "
            "FECHA_DOC). USAR cuando el usuario pregunta 'estado de la "
            "cola de documentos electrónicos', 'cómo está la cola de "
            "firma', 'qué hay en la cola', 'cuántos documentos están "
            "autorizados/enviados/pendientes/en error'. Devuelve "
            "breakdown completo, no solo errores."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_examples": {"type": "boolean", "default": True},
                "max_examples_per_state": {
                    "type": "integer", "default": 3,
                    "minimum": 0, "maximum": 10,
                },
            },
        },
    },
    {
        "name": "list_signature_queue_errors",
        "description": (
            "Lista los registros de COLA_DOCS_FIRMAR con "
            "ESTADO_FEAP='I' (Error). Por defecto filtra solo los que "
            "tienen mensajes ya identificados como SEGUROS de resetear "
            "('No es posible modificar un comprobante autorizado', "
            "'INVALID_RECEIPT'). El response separa 'safe_to_reset' de "
            "'other_errors' — los other_errors son problemas reales de "
            "datos que un reset NO arreglaría. Pasa "
            "``include_all_errors=True`` para ver también los other. "
            "USAR cuando el usuario pregunta 'qué hay en error en la "
            "cola', 'qué se debe destrabar', 'comprobantes atorados', "
            "'cuántos por arreglar'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Substrings (case-insensitive) a buscar en "
                        "OBSER_DOC_SRI. Default: los 2 patrones seguros."
                    ),
                },
                "include_all_errors": {
                    "type": "boolean", "default": False,
                    "description": "Si True trae también los errores que NO son seguros de resetear (solo lectura).",
                },
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "generate_negative_stock_report",
        "description": (
            "Genera el XLSX 'PRODUCTOS CON SALDO NEGATIVO' (reporte al "
            "momento, sin parametros de fecha) y lo sube DIRECTO al chat "
            "de Telegram con paleta corporativa + AutoFilter + negativos "
            "en rojo. Una fila por producto con saldo total <0, con "
            "desglose por bodega (columnas dinamicas). Usa el proceso "
            "Velneo BUSCAR_PRODUCTOS_SIN_EXISTENCIAS que filtra del lado "
            "del servidor (no descarga millones de productos). "
            "USAR cuando el usuario pide: 'productos con saldo negativo', "
            "'detalle de saldos negativos', 'existencias en rojo', "
            "'productos en negativo', 'productos sobrevendidos'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deliver_to_chat": {
                    "type": "string",
                    "description": "REQUERIDO. Telegram chat_id. Para Mepriga: '-5248384291'.",
                },
                "include_zero_total_with_negative_bodega": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Si True, ademas de los productos con total <0, "
                        "incluye productos cuyo total es 0 pero al menos "
                        "una bodega tiene saldo negativo (sobreventa en "
                        "una ubicacion compensada en otra)."
                    ),
                },
            },
            "required": ["deliver_to_chat"],
        },
    },
    {
        "name": "inventory_movements_window",
        "description": (
            "Devuelve movimientos de inventario (INV_MOVIMIENTOS) por "
            "rango de fechas + tipo de documento. Wrapper del proceso "
            "Velneo INV_DOC_MOV_BUSQ_JS. tipo_doc: 'V'=ventas, 'W'=NCs "
            "ventas, 'C'=compras, 'D'=NCs compras, ''=todos. Filtros "
            "opcionales: producto, bodega, sucursal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "ISO YYYY-MM-DD"},
                "tipo_doc": {
                    "type": "string",
                    "enum": ["", "V", "W", "C", "D"],
                    "default": "",
                    "description": "V=ventas, W=NCs vtas, C=compras, D=NCs compras, ''=todos",
                },
                "sucursal": {"type": "string"},
                "producto": {"type": "integer", "default": 0, "description": "0=no filtrar"},
                "bodega": {"type": "integer", "default": 0, "description": "0=no filtrar"},
                "off": {"type": "integer", "default": 0, "description": "1=incluir desactivados"},
                "limit": {"type": "integer", "default": 1000, "minimum": 1, "maximum": 5000},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "reset_signature_queue_record",
        "description": (
            "Cambia ESTADO_FEAP='1' en UNA fila de COLA_DOCS_FIRMAR "
            "por ID. Solo aplica si la fila está actualmente en estado "
            "'I' (Error) — si está en otro estado el tool rechaza con "
            "``error_code='not_in_error_state'`` para no romper un "
            "flujo normal. Re-lee la fila tras el write para "
            "confirmar. USAR después de que el usuario AUTORIZA "
            "explícitamente el reset de un ID específico (o de una "
            "lista — llamá el tool por cada ID en paralelo). NUNCA "
            "ejecutar sin confirmación humana previa: mostrar primero "
            "list_signature_queue_errors, pedir OK, después resetear. "
            "El efecto es seguro: el próximo ciclo de verificación "
            "consulta al SRI, recibe AUTORIZADO y cierra la fila. No "
            "hay re-envío ni duplicación."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "integer", "minimum": 1},
                "reason": {
                    "type": "string",
                    "description": "Texto libre para audit log (quién autorizó, contexto).",
                },
            },
            "required": ["record_id"],
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
    "render_quotation_pdf": ("sales.render_quotation_pdf", sales.render_quotation_pdf),
    "add_quotation_line": ("sales.add_quotation_line", sales.add_quotation_line),
    "update_quotation_line": ("sales.update_quotation_line", sales.update_quotation_line),
    "remove_quotation_line": ("sales.remove_quotation_line", sales.remove_quotation_line),
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
    "identify_supplier": ("admin_ops.identify_supplier", admin_ops.identify_supplier),
    "list_supplier_debts": ("admin_ops.list_supplier_debts", admin_ops.list_supplier_debts),
    "get_open_debts": ("admin_ops.get_open_debts", admin_ops.get_open_debts),
    "velneo_query": ("admin_ops.velneo_query", admin_ops.velneo_query),
    "partner_360": ("admin_ops.partner_360", admin_ops.partner_360),
    "customer_full_view": ("admin_ops.customer_full_view", admin_ops.customer_full_view),
    "list_recent_stock_movements": ("admin_ops.list_recent_stock_movements", admin_ops.list_recent_stock_movements),
    "check_stock": ("products.check_stock", products.check_stock),
    "search_velneo": ("admin_search.search_velneo", admin_search.search_velneo),
    "sales_quick_summary": ("admin_ops.sales_quick_summary", admin_ops.sales_quick_summary),
    "sales_dashboard_chart": ("admin_ops.sales_dashboard_chart", admin_ops.sales_dashboard_chart),
    "sales_evolution_chart": ("admin_ops.sales_evolution_chart", admin_ops.sales_evolution_chart),
    "generate_sales_report": ("admin_ops.generate_sales_report", admin_ops.generate_sales_report),
    "signature_queue_status": ("signature_queue.signature_queue_status", signature_queue.signature_queue_status),
    "list_signature_queue_errors": ("signature_queue.list_signature_queue_errors", signature_queue.list_signature_queue_errors),
    "reset_signature_queue_record": ("signature_queue.reset_signature_queue_record", signature_queue.reset_signature_queue_record),
    "generate_negative_stock_report": ("inventory.generate_negative_stock_report", inventory.generate_negative_stock_report),
    "inventory_movements_window": ("inventory.inventory_movements_window", inventory.inventory_movements_window),
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

    import time
    t0 = time.monotonic()
    log_ctx = {
        "tool": name,
        "tenant_id": getattr(cfg, "tenant_id", "") or "",
        "tenant_slug": getattr(cfg, "slug", "") or "",
        "channel": channel,
        "channel_user_id": channel_user_id,
    }
    async with VelneoClient(cfg) as client:
        try:
            result = await fn(client, **args)
        except TypeError as exc:
            logger.info(
                "tool_call",
                extra={**log_ctx, "ok": False, "error": f"bad_args:{exc}",
                       "latency_ms": int((time.monotonic() - t0) * 1000)},
            )
            return json.dumps(
                {"success": False, "error": f"bad arguments: {exc}"},
                ensure_ascii=False,
            )
        except VelneoError as exc:
            logger.info(
                "tool_call",
                extra={**log_ctx, "ok": False,
                       "error": f"velneo_{exc.status}:{exc.message}",
                       "latency_ms": int((time.monotonic() - t0) * 1000)},
            )
            return json.dumps(
                {
                    "success": False,
                    "error": f"velneo {exc.status}: {exc.message}",
                    "velneo_status": exc.status,
                },
                ensure_ascii=False,
            )
    ok = bool(result.get("success", True)) if isinstance(result, dict) else True
    err_code = result.get("error_code") or result.get("error") if isinstance(result, dict) and not ok else None
    logger.info(
        "tool_call",
        extra={**log_ctx, "ok": ok,
               "error": (str(err_code)[:120] if err_code else None),
               "latency_ms": int((time.monotonic() - t0) * 1000)},
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
