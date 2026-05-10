"""Payment tools — generate and inspect payment links (PayPhone, etc.).

These tools delegate the heavy lifting (API calls, signing, persistence) to
the Odoo modules already installed on the tenant's instance — the MCP layer
just orchestrates the XMLRPC handshake and translates Odoo exceptions into
structured envelopes the LLM can reason about.

PayPhone backend:
  - Module: ``payment_payphone`` v13.0.1.1.0 (Tecnosmart server).
  - Public method: ``sale.order.action_send_payphone_link()`` — generates the
    link via PayPhone REST API and persists a ``payment.link.history`` row
    with ``state='pending'`` and ``expire_date=now()+48h``.
  - Status refresh: ``payment.acquirer.payphone_check_transaction_status(client_tx_id)``.
"""

import logging
import time
from typing import Any

from mcp_odoo.tools.generic import odoo_search, odoo_call_method

logger = logging.getLogger("mcp_odoo.payments")


def _log_call(tool: str, tenant_id: str, args: dict, result: dict | None, error: str | None, elapsed_ms: int):
    """Structured log of every Odoo tool call. Goes to stdout (captured by docker logs)."""
    payload = {
        "evt": "odoo_tool_call",
        "tool": tool,
        "tenant_id": tenant_id,
        "args": args,
        "elapsed_ms": elapsed_ms,
        "ok": error is None,
    }
    if error:
        payload["error"] = error
    else:
        payload["result_keys"] = list((result or {}).keys())
    if error:
        logger.error("ODOO_CALL %s", payload)
    else:
        logger.info("ODOO_CALL %s", payload)


# States in which the Odoo module allows generating a payment link.
# Mirrors the validation inside ``sale.order.action_send_payphone_link``.
_PAYPHONE_ALLOWED_STATES = {"draft", "sent", "approved", "sale"}


def _classify_payphone_error(exc: Exception) -> tuple[str, str]:
    """Translate an Odoo XMLRPC fault/exception into (error_code, detail).

    The Odoo module raises ``UserError`` for: missing acquirer, missing
    Bearer token, PayPhone API HTTP error, missing fiscal data, etc.
    XMLRPC surfaces those as ``xmlrpc.client.Fault`` whose ``faultString``
    contains the original message. We do a best-effort substring match so
    the LLM gets a meaningful ``error_code`` instead of a generic 500.
    """
    msg = str(exc) or ""
    low = msg.lower()
    if "payphone" in low and ("acquirer" in low or "configurad" in low or "no se encontr" in low):
        return "no_payphone_acquirer", msg
    if "token" in low and "payphone" in low:
        return "no_payphone_acquirer", msg
    if "estado" in low or "state" in low:
        return "wrong_state", msg
    if "api" in low or "http" in low or "respond" in low or "tiempo de espera" in low:
        return "payphone_api_error", msg
    return "payphone_api_error", msg


def odoo_create_payphone_link(
    tenant_id: str, url: str, db: str, user: str, password: str,
    order_id: int,
) -> dict:
    """Generate a PayPhone payment link for an existing sale.order.

    The Odoo module already handles:
      * acquirer lookup (``payment.acquirer`` with ``provider='payphone'``),
      * SRI amount breakdown (``_payphone_get_sri_amounts``),
      * POST to ``{payphone_base_url}/api/Links`` with Bearer token,
      * persistence in ``payment.link.history`` (state='pending', expire+48h).

    We just wrap the call, normalize errors, and read back the freshest
    history row so the LLM gets the link + tx ref in a single shape.

    Args:
        order_id: numeric sale.order id in Odoo.

    Returns on success:
        {success: true, order_id, order_name, link_url, client_tx_id,
         amount, expire_at, state, currency}
    Returns on failure:
        {success: false, error_code, error_detail}
    """
    started = time.time()
    log_args = {"order_id": order_id}

    if not order_id or not isinstance(order_id, int):
        err = "order_id requerido y debe ser entero"
        _log_call("create_payphone_link", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_order_id",
            "error_detail": err,
        }

    # ── Read order and validate state ────────────────────────────────────
    try:
        orders = odoo_search(
            tenant_id, url, db, user, password,
            "sale.order", [("id", "=", order_id)],
            fields=["id", "name", "state", "partner_id", "amount_total"],
            limit=1,
        )
    except Exception as e:
        err = f"Error leyendo sale.order: {e}"
        _log_call("create_payphone_link", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "order_read_failed",
            "error_detail": err,
        }
    if not orders:
        err = f"sale.order id={order_id} no existe"
        _log_call("create_payphone_link", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "order_not_found",
            "error_detail": err,
        }
    order = orders[0]
    state = order.get("state")
    if state not in _PAYPHONE_ALLOWED_STATES:
        err = (
            f"La cotizacion {order.get('name')} esta en estado '{state}'. "
            f"Solo se puede generar link en estados {sorted(_PAYPHONE_ALLOWED_STATES)}."
        )
        _log_call("create_payphone_link", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "wrong_state",
            "error_detail": err,
        }

    # ── Trigger the link generation in Odoo ─────────────────────────────
    # The method opens a wizard (returns an action dict). The actual link
    # is persisted in payment.link.history; we read it back below.
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "sale.order", "action_send_payphone_link", [order_id],
        )
    except Exception as e:
        code, detail = _classify_payphone_error(e)
        _log_call("create_payphone_link", tenant_id, log_args, None, detail, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": code,
            "error_detail": detail,
            "order_id": order_id,
            "order_name": order.get("name"),
        }

    # ── Read back the latest history row ────────────────────────────────
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "payment.link.history",
            [("sale_id", "=", order_id), ("provider", "=", "payphone")],
            fields=["id", "link_url", "client_tx_id", "amount", "expire_date", "state"],
            limit=1,
            order="create_date desc",
        )
    except Exception as e:
        err = f"Error leyendo payment.link.history: {e}"
        _log_call("create_payphone_link", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "history_read_failed",
            "error_detail": err,
        }
    if not rows:
        err = (
            f"Odoo no creo una fila en payment.link.history para sale_id={order_id}. "
            "Probablemente la API de PayPhone no respondio con un link valido."
        )
        _log_call("create_payphone_link", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "no_history_row",
            "error_detail": err,
        }
    row = rows[0]

    result = {
        "success": True,
        "order_id": order_id,
        "order_name": order.get("name"),
        "link_url": row.get("link_url"),
        "client_tx_id": row.get("client_tx_id"),
        "amount": float(row.get("amount") or order.get("amount_total") or 0),
        "expire_at": str(row.get("expire_date") or ""),
        "state": row.get("state") or "pending",
        "currency": "USD",
    }
    _log_call("create_payphone_link", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
    return result


def odoo_check_payphone_status(
    tenant_id: str, url: str, db: str, user: str, password: str,
    client_tx_id: str,
    refresh: bool = False,
) -> dict:
    """Look up the state of a PayPhone link by ``client_tx_id``.

    Args:
        client_tx_id: PayPhone reference returned by ``odoo_create_payphone_link``.
        refresh: when True, force a poll to PayPhone first (this updates the
            ``payment.link.history`` row); when False (default), just read
            the cached state.
    """
    started = time.time()
    log_args = {"client_tx_id": client_tx_id, "refresh": bool(refresh)}

    if not client_tx_id or not isinstance(client_tx_id, str):
        err = "client_tx_id requerido (string)"
        _log_call("check_payphone_status", tenant_id, log_args, None, err, 0)
        return {
            "success": False,
            "error_code": "invalid_client_tx_id",
            "error_detail": err,
        }

    # ── Optional refresh against PayPhone API ───────────────────────────
    if refresh:
        try:
            acquirers = odoo_search(
                tenant_id, url, db, user, password,
                "payment.acquirer",
                [("provider", "=", "payphone"), ("state", "in", ("enabled", "test"))],
                fields=["id", "state"],
                limit=1,
            )
            if acquirers:
                try:
                    odoo_call_method(
                        tenant_id, url, db, user, password,
                        "payment.acquirer",
                        "payphone_check_transaction_status",
                        [acquirers[0]["id"]],
                        args=[client_tx_id],
                    )
                except Exception as e:
                    # Refresh is best-effort: if PayPhone is down we still
                    # return whatever the history row currently says.
                    logger.warning(
                        "payphone refresh failed tenant=%s tx=%s: %s",
                        tenant_id, client_tx_id, e,
                    )
            else:
                logger.warning(
                    "payphone refresh skipped: no enabled acquirer (tenant=%s)",
                    tenant_id,
                )
        except Exception as e:
            logger.warning(
                "payphone acquirer lookup failed tenant=%s: %s", tenant_id, e,
            )

    # ── Read the persisted row ──────────────────────────────────────────
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "payment.link.history",
            [("client_tx_id", "=", client_tx_id)],
            fields=[
                "id", "link_url", "client_tx_id", "sale_id", "invoice_id",
                "amount", "expire_date", "state",
            ],
            limit=1,
        )
    except Exception as e:
        err = f"Error leyendo payment.link.history: {e}"
        _log_call("check_payphone_status", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "history_read_failed",
            "error_detail": err,
        }
    if not rows:
        err = f"No existe payment.link.history con client_tx_id='{client_tx_id}'"
        _log_call("check_payphone_status", tenant_id, log_args, None, err, int((time.time() - started) * 1000))
        return {
            "success": False,
            "error_code": "tx_not_found",
            "error_detail": err,
        }
    row = rows[0]
    sale_raw = row.get("sale_id")
    invoice_raw = row.get("invoice_id")
    sale_id: int | None = sale_raw[0] if isinstance(sale_raw, list) and sale_raw else None
    invoice_id: int | None = invoice_raw[0] if isinstance(invoice_raw, list) and invoice_raw else None

    result = {
        "success": True,
        "client_tx_id": row.get("client_tx_id"),
        "sale_id": sale_id,
        "invoice_id": invoice_id,
        "amount": float(row.get("amount") or 0),
        "state": row.get("state") or "pending",
        "expire_at": str(row.get("expire_date") or ""),
        "link_url": row.get("link_url"),
    }
    _log_call("check_payphone_status", tenant_id, log_args, result, None, int((time.time() - started) * 1000))
    return result
