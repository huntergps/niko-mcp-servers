"""Deposit / proof-of-payment tools for salon tenants (Odoo 19).

The salon (Afrodita) holds a slot only after a 50% non-refundable
deposit. The booking flow is:

  1. ``book_appointment`` creates a ``calendar.event`` prefixed with
     ``"[POR CONFIRMAR] "`` (see ``mcp_odoo.tools.appointments``).
  2. The customer pays by bank transfer and sends a photo of the
     receipt.
  3. The staff verify the receipt and confirm the appointment.

This module implements the four tools that drive steps 2-4 plus the
expiration cron:

  * ``register_deposit_proof``          → attach the receipt to the event,
                                          create a DRAFT ``account.payment``
                                          and attach the receipt to it too.
  * ``confirm_appointment``             → strip the ``[POR CONFIRMAR] ``
                                          prefix and post the deposit
                                          payment.
  * ``release_appointment``             → cancel the appointment (unlink,
                                          fallback ``active=False``) and
                                          drop any draft payment.
  * ``list_pending_deposit_appointments`` → list ``[POR CONFIRMAR] ``
                                          events older than N minutes for
                                          the expiration cron.

Design notes
------------
* Pure functions: every helper receives the per-request Odoo connection
  tuple ``(tenant_id, url, db, user, password)`` plus typed args, exactly
  like ``mcp_odoo.tools.appointments`` / ``mcp_odoo.tools.billing``. The
  dispatch layer in ``mcp_transport.py`` resolves that tuple from the
  request headers (multi-tenant) and passes it down — we never open a
  connection here.
* All Odoo I/O goes through the shared ``odoo_search`` / ``odoo_read`` /
  ``odoo_create`` / ``odoo_call_method`` from ``mcp_odoo.tools.generic``.
* The deposit ``account.payment`` is left in **DRAFT** on
  ``register_deposit_proof`` (the staff post it on
  ``confirm_appointment``). We resolve the bank journal by
  ``type='bank'`` (never hardcoded). We do NOT set
  ``l10n_ec_sri_payment_id`` — that field does not exist on
  ``account.payment`` in this Odoo.
* The deposit payment is linked back to the event purely via its
  ``memo`` (``"Anticipo cita #<event_id>"``) so confirm/release can find
  it again without a custom relation field.
* ``calendar.event.create_date`` is stored in Odoo as a UTC-naive string;
  the expiration query compares against UTC.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp_odoo.tools.generic import (
    odoo_call_method,
    odoo_create,
    odoo_read,
    odoo_search,
)

logger = logging.getLogger("mcp_odoo.deposits")

_UTC = ZoneInfo("UTC")

# Prefix the booking flow puts on a calendar.event while the deposit is
# still pending. Must match ``appointments.book_appointment``.
PENDING_PREFIX = "[POR CONFIRMAR] "

# Memo template that links the deposit account.payment to its event. We
# search by this memo to post / cancel the payment later.
_MEMO_TMPL = "Anticipo cita #{event_id}"

_DEFAULT_MIMETYPE = "image/jpeg"
_DEFAULT_FILENAME = "comprobante.jpg"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_call(
    tool: str, tenant_id: str, args: dict, result_summary: Any,
    error: str | None, duration_ms: int,
) -> None:
    """Lightweight structured log (mirrors billing._log_call style)."""
    if error:
        logger.warning(
            "deposit_tool=%s tenant=%s args=%s error=%s ms=%d",
            tool, tenant_id, args, error, duration_ms,
        )
    else:
        logger.info(
            "deposit_tool=%s tenant=%s args=%s result=%s ms=%d",
            tool, tenant_id, args, result_summary, duration_ms,
        )


def _flatten_m2o(value: Any) -> dict | None:
    """``[id, name]`` -> ``{"id": id, "name": name}``."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {"id": value[0], "name": value[1]}
    return None


def _resolve_bank_journal_id(creds: tuple) -> int | None:
    """Resolve the default bank journal (``account.journal`` type='bank').

    Returns the first bank journal id (ordered by sequence/id) or ``None``
    when the tenant has no bank journal configured. Never hardcoded — the
    deposit is recorded against whatever bank journal Odoo exposes.
    """
    tenant_id, url, db, user, password = creds
    rows = odoo_search(
        tenant_id, url, db, user, password,
        "account.journal", [["type", "=", "bank"]],
        fields=["id", "name", "code"], limit=1, order="sequence, id",
    )
    if not rows:
        return None
    return int(rows[0]["id"])


def _find_deposit_payment(
    creds: tuple, event_id: int, states: list[str] | None = None,
) -> dict | None:
    """Find the deposit ``account.payment`` linked to ``event_id`` by memo.

    Returns the first matching payment dict (``id, state, amount, memo``)
    or ``None``. Optionally restricts to the given ``states`` (e.g.
    ``["draft"]``). Best-effort — swallows lookup errors and returns
    ``None`` so callers can treat the payment as absent.
    """
    tenant_id, url, db, user, password = creds
    memo = _MEMO_TMPL.format(event_id=event_id)
    domain: list = [["memo", "=", memo]]
    if states:
        domain.append(["state", "in", list(states)])
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "account.payment", domain,
            fields=["id", "state", "amount", "memo"], limit=1, order="id desc",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deposits: payment lookup failed for event %s: %s",
            event_id, exc,
        )
        return None
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def register_deposit_proof(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    event_id: int,
    partner_id: int,
    image_base64: str,
    filename: str,
    amount: float,
    mimetype: str | None = None,
) -> dict:
    """Register a customer's deposit receipt against an appointment.

    Attaches the receipt image to the ``calendar.event``, creates a
    **DRAFT** ``account.payment`` (inbound / customer, bank journal) for
    the deposit, and attaches the same receipt to that payment for
    accounting. The payment is left in draft so the staff post it when
    they confirm the appointment (``confirm_appointment``).

    Args
    ----
    event_id : int
        ``calendar.event.id`` of the pending appointment.
    partner_id : int
        ``res.partner.id`` of the customer who paid.
    image_base64 : str
        Base64-encoded receipt image (raw, not a data: URI).
    filename : str
        Original file name (e.g. 'comprobante.jpg').
    amount : float
        Deposit amount paid.
    mimetype : str, optional
        MIME type of the image (defaults to 'image/jpeg').

    Returns
    -------
    dict
        Envelope ``{success, attachment_id, payment_id, event_id,
        amount}`` on success, or ``{success: False, error_code,
        error_detail}`` in Spanish.
    """
    started = time.time()
    creds = (tenant_id, url, db, user, password)
    log_args = {"event_id": event_id, "partner_id": partner_id,
                "filename": filename, "amount": amount}

    # ----- 1. Validate inputs ----------------------------------------------
    if not isinstance(event_id, int) or event_id <= 0:
        return {"success": False, "error_code": "invalid_event_id",
                "error_detail": "El id de la cita no es válido."}
    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id",
                "error_detail": "El id del cliente no es válido."}
    if not image_base64 or not isinstance(image_base64, str):
        return {"success": False, "error_code": "no_image",
                "error_detail": "No recibí el comprobante de pago."}
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        amount_f = 0.0
    if amount_f <= 0:
        return {"success": False, "error_code": "invalid_amount",
                "error_detail": "El monto del anticipo no es válido."}

    # ----- 2. Validate event + partner exist -------------------------------
    try:
        erows = odoo_read(
            tenant_id, url, db, user, password,
            "calendar.event", [event_id], ["name"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo la cita: {exc}"
        _log_call("register_deposit_proof", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "event_read_failed",
                "error_detail": msg}
    if not erows:
        return {"success": False, "error_code": "event_not_found",
                "error_detail": f"No existe la cita id={event_id}."}

    try:
        prows = odoo_read(
            tenant_id, url, db, user, password,
            "res.partner", [partner_id], ["name"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo el cliente: {exc}"
        _log_call("register_deposit_proof", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "partner_read_failed",
                "error_detail": msg}
    if not prows:
        return {"success": False, "error_code": "partner_not_found",
                "error_detail": f"No existe un cliente con id={partner_id}."}

    safe_name = (filename or "").strip() or _DEFAULT_FILENAME
    safe_mimetype = (mimetype or "").strip() or _DEFAULT_MIMETYPE

    # ----- 3. Attach the receipt to the calendar.event ---------------------
    try:
        attachment_id = odoo_create(
            tenant_id, url, db, user, password,
            "ir.attachment",
            {
                "name": safe_name,
                "datas": image_base64,
                "res_model": "calendar.event",
                "res_id": event_id,
                "mimetype": safe_mimetype,
            },
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error guardando el comprobante: {exc}"
        _log_call("register_deposit_proof", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "attachment_failed",
                "error_detail": msg}
    attachment_id = int(attachment_id)

    # ----- 4. Resolve the bank journal -------------------------------------
    try:
        bank_journal_id = _resolve_bank_journal_id(creds)
    except Exception as exc:  # noqa: BLE001
        msg = f"Error buscando el diario bancario: {exc}"
        _log_call("register_deposit_proof", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "journal_read_failed",
                "error_detail": msg}
    if not bank_journal_id:
        return {"success": False, "error_code": "no_bank_journal",
                "error_detail": (
                    "No encontré un diario bancario configurado en Odoo."
                )}

    # ----- 5. Create the deposit account.payment in DRAFT ------------------
    payment_values: dict[str, Any] = {
        "payment_type": "inbound",
        "partner_type": "customer",
        "partner_id": partner_id,
        "amount": amount_f,
        "date": date.today().isoformat(),
        "journal_id": bank_journal_id,
        "memo": _MEMO_TMPL.format(event_id=event_id),
    }
    try:
        payment_id = odoo_create(
            tenant_id, url, db, user, password,
            "account.payment", payment_values,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error registrando el anticipo: {exc}"
        _log_call("register_deposit_proof", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "payment_create_failed",
                "error_detail": msg}
    payment_id = int(payment_id)

    # ----- 6. Attach the same receipt to the payment (best-effort) ---------
    try:
        odoo_create(
            tenant_id, url, db, user, password,
            "ir.attachment",
            {
                "name": safe_name,
                "datas": image_base64,
                "res_model": "account.payment",
                "res_id": payment_id,
                "mimetype": safe_mimetype,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "register_deposit_proof: payment attachment failed for %s: %s",
            payment_id, exc,
        )

    result = {
        "success": True,
        "attachment_id": attachment_id,
        "payment_id": payment_id,
        "event_id": event_id,
        "amount": round(amount_f, 2),
    }
    _log_call("register_deposit_proof", tenant_id, log_args,
              {"attachment_id": attachment_id, "payment_id": payment_id},
              None, int((time.time() - started) * 1000))
    return result


def confirm_appointment(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    event_id: int,
) -> dict:
    """Confirm a pending appointment after the staff verify the deposit.

    Strips the ``"[POR CONFIRMAR] "`` prefix from the event name and posts
    the linked draft deposit ``account.payment`` (best-effort — if the
    payment cannot be found or posted, the confirmation still succeeds).

    Args
    ----
    event_id : int
        ``calendar.event.id`` to confirm.

    Returns
    -------
    dict
        Envelope ``{success, event_id, new_name, payment_posted}`` on
        success, or ``{success: False, error_code, error_detail}``.
    """
    started = time.time()
    creds = (tenant_id, url, db, user, password)
    log_args = {"event_id": event_id}

    if not isinstance(event_id, int) or event_id <= 0:
        return {"success": False, "error_code": "invalid_event_id",
                "error_detail": "El id de la cita no es válido."}

    # ----- 1. Read the event + strip the prefix ----------------------------
    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "calendar.event", [event_id], ["name"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo la cita: {exc}"
        _log_call("confirm_appointment", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "event_read_failed",
                "error_detail": msg}
    if not rows:
        return {"success": False, "error_code": "event_not_found",
                "error_detail": f"No existe la cita id={event_id}."}

    current_name = rows[0].get("name") or ""
    if current_name.startswith(PENDING_PREFIX):
        new_name = current_name[len(PENDING_PREFIX):]
    else:
        new_name = current_name

    if new_name != current_name:
        try:
            odoo_call_method(
                tenant_id, url, db, user, password,
                "calendar.event", "write",
                [event_id], args=[{"name": new_name}],
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"No pude confirmar la cita: {exc}"
            _log_call("confirm_appointment", tenant_id, log_args, None, msg,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "confirm_failed",
                    "error_detail": msg}

    # ----- 2. Post the linked draft deposit payment (best-effort) ----------
    # NOTE: Odoo's XML-RPC endpoint serializes responses with
    # allow_none=False, so ``action_post`` (which can return None / a dict
    # holding None) may raise a marshalling TypeError on the WAY BACK even
    # though the post itself succeeded server-side. We therefore swallow the
    # call error and confirm the outcome by RE-READING the payment state
    # instead of trusting the return value.
    payment_posted = False
    payment = _find_deposit_payment(creds, event_id, states=["draft"])
    if payment:
        pay_id = int(payment["id"])
        try:
            odoo_call_method(
                tenant_id, url, db, user, password,
                "account.payment", "action_post", [pay_id],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "confirm_appointment: action_post raised for payment %s "
                "(may be an XML-RPC marshalling artefact, verifying state): "
                "%s", pay_id, exc,
            )
        # Verify the real state — a posted payment moves to 'posted' (or
        # 'paid' once reconciled). Either counts as posted for our purposes.
        try:
            prows = odoo_read(
                tenant_id, url, db, user, password,
                "account.payment", [pay_id], ["state"],
            )
            if prows and (prows[0].get("state") in ("posted", "paid")):
                payment_posted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "confirm_appointment: payment state re-read failed for %s: %s",
                pay_id, exc,
            )

    result = {
        "success": True,
        "event_id": event_id,
        "new_name": new_name,
        "payment_posted": payment_posted,
    }
    _log_call("confirm_appointment", tenant_id, log_args,
              {"new_name": new_name, "payment_posted": payment_posted},
              None, int((time.time() - started) * 1000))
    return result


def release_appointment(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    event_id: int,
    reason: str | None = None,
) -> dict:
    """Release (cancel) a pending appointment — expiration or rejection.

    Deletes the ``calendar.event`` (unlink; falls back to
    ``write active=False`` if the unlink is denied by ACL). If a DRAFT
    deposit ``account.payment`` is linked, it is cancelled/deleted
    best-effort (a posted payment is left untouched — accounting keeps it).

    Args
    ----
    event_id : int
        ``calendar.event.id`` to release.
    reason : str, optional
        Free-text reason (logged; not persisted on Odoo).

    Returns
    -------
    dict
        Envelope ``{success, event_id, method}`` on success, or
        ``{success: False, error_code, error_detail}``.
    """
    started = time.time()
    creds = (tenant_id, url, db, user, password)
    log_args = {"event_id": event_id, "reason": reason}

    if not isinstance(event_id, int) or event_id <= 0:
        return {"success": False, "error_code": "invalid_event_id",
                "error_detail": "El id de la cita no es válido."}

    # ----- 1. Verify the event exists --------------------------------------
    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "calendar.event", [event_id], ["name"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo la cita: {exc}"
        _log_call("release_appointment", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "event_read_failed",
                "error_detail": msg}
    if not rows:
        return {"success": False, "error_code": "event_not_found",
                "error_detail": f"No existe la cita id={event_id}."}

    # ----- 2. Drop any DRAFT deposit payment (best-effort) -----------------
    payment = _find_deposit_payment(creds, event_id, states=["draft"])
    if payment:
        pay_id = int(payment["id"])
        try:
            odoo_call_method(
                tenant_id, url, db, user, password,
                "account.payment", "unlink", [pay_id],
            )
        except Exception as unlink_exc:  # noqa: BLE001
            logger.warning(
                "release_appointment: payment unlink failed (%s), trying "
                "action_cancel", unlink_exc,
            )
            try:
                odoo_call_method(
                    tenant_id, url, db, user, password,
                    "account.payment", "action_cancel", [pay_id],
                )
            except Exception as cancel_exc:  # noqa: BLE001
                logger.warning(
                    "release_appointment: payment action_cancel failed for "
                    "%s: %s", pay_id, cancel_exc,
                )

    # ----- 3. Delete the event (unlink, fallback active=False) -------------
    method_used = "unlink"
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "calendar.event", "unlink", [event_id],
        )
    except Exception as unlink_exc:  # noqa: BLE001
        logger.warning(
            "release_appointment: unlink failed (%s), trying active=False",
            unlink_exc,
        )
        try:
            odoo_call_method(
                tenant_id, url, db, user, password,
                "calendar.event", "write", [event_id],
                args=[{"active": False}],
            )
            method_used = "deactivate"
        except Exception as exc:  # noqa: BLE001
            msg = f"No pude liberar la cita: {exc}"
            _log_call("release_appointment", tenant_id, log_args, None, msg,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "release_failed",
                    "error_detail": msg}

    result = {
        "success": True,
        "event_id": event_id,
        "method": method_used,
    }
    _log_call("release_appointment", tenant_id, log_args,
              {"method": method_used}, None,
              int((time.time() - started) * 1000))
    return result


def list_pending_deposit_appointments(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    older_than_minutes: int = 120,
) -> dict:
    """List ``[POR CONFIRMAR]`` appointments older than ``older_than_minutes``.

    Drives the expiration cron: niko warns the customer and releases the
    slot when the deposit was never paid. Matches events whose ``name``
    starts with ``"[POR CONFIRMAR] "`` and whose ``create_date`` is older
    than the cutoff (computed in UTC — Odoo stores ``create_date`` as a
    UTC-naive string).

    Args
    ----
    older_than_minutes : int, default 120
        Minimum age in minutes since creation. Clamped to >= 0.

    Returns
    -------
    dict
        Envelope ``{success, count, older_than_minutes, appointments[]}``.
        Each item: ``{event_id, name, start, create_date, partner_ids}``.
    """
    started = time.time()
    log_args = {"older_than_minutes": older_than_minutes}

    try:
        cutoff_minutes = max(0, int(older_than_minutes))
    except (TypeError, ValueError):
        cutoff_minutes = 120

    cutoff_utc = datetime.now(_UTC) - timedelta(minutes=cutoff_minutes)
    cutoff_iso = cutoff_utc.strftime("%Y-%m-%d %H:%M:%S")

    try:
        events = odoo_search(
            tenant_id, url, db, user, password,
            "calendar.event",
            [
                ["name", "=like", f"{PENDING_PREFIX}%"],
                ["create_date", "<", cutoff_iso],
            ],
            fields=["name", "start", "create_date", "partner_ids"],
            limit=200,
            order="create_date asc",
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error listando citas por confirmar: {exc}"
        _log_call("list_pending_deposit_appointments", tenant_id, log_args,
                  None, msg, int((time.time() - started) * 1000))
        return {"success": False, "error_code": "search_failed",
                "error_detail": msg}

    appointments: list[dict] = []
    for e in events:
        appointments.append({
            "event_id": int(e["id"]),
            "name": e.get("name") or "",
            "start": e.get("start") or "",
            "create_date": e.get("create_date") or "",
            "partner_ids": [int(x) for x in (e.get("partner_ids") or [])
                            if isinstance(x, int)],
        })

    result = {
        "success": True,
        "count": len(appointments),
        "older_than_minutes": cutoff_minutes,
        "appointments": appointments,
        "display_type": "list_data",
    }
    _log_call("list_pending_deposit_appointments", tenant_id, log_args,
              {"count": len(appointments)}, None,
              int((time.time() - started) * 1000))
    return result


__all__ = [
    "register_deposit_proof",
    "confirm_appointment",
    "release_appointment",
    "list_pending_deposit_appointments",
]
