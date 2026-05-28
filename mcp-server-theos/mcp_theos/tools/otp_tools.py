"""``request_otp`` / ``verify_otp`` MCP tools for Theos tenants.

The financial tools (``check_balance``, ``get_customer_invoices``,
``get_customer_payments``) refuse to return data until the caller has
verified the customer's identity through this pair of tools. The gate
itself lives in :mod:`mcp_theos.transports.mcp_transport`.

Multi-tenancy: all branding (subject, body, footer) and SMTP creds
are resolved per-tenant inside :mod:`mcp_theos.otp` — there is no
``"Tecnosmart"`` fallback string anywhere on this path.
"""

from __future__ import annotations

from typing import Any

from mcp_theos.otp import (
    _generate_token,
    check_session,
    send_otp_email,
    verify_token,
)
from mcp_theos.velneo_http import VelneoClient, VelneoError


_EMAIL_SPLIT_CHARS = ",;|"


def _pick_first_email(value: str) -> str:
    """Velneo's ``MAIL_PRINCIPAL`` can hold multiple addresses separated
    by ``,`` (commonly), ``;`` or ``|``. Return the first one that looks
    like an email (has ``@`` + a dot in the domain), trimmed.
    """
    if not value:
        return ""
    raw = str(value).strip()
    # split on the first matching separator, fallback to single value
    candidates = [raw]
    for sep in _EMAIL_SPLIT_CHARS:
        if sep in raw:
            candidates = [p.strip() for p in raw.split(sep)]
            break
    for c in candidates:
        c = c.strip().strip("<>").strip()
        if "@" in c and "." in c.split("@", 1)[1]:
            return c
    return ""


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    head, at_rest = email.split("@", 1)
    if len(head) <= 3:
        return f"{head[:1]}***@{at_rest}"
    return f"{head[:3]}***@{at_rest}"


async def _read_partner_email(
    client: VelneoClient, partner_id: int,
) -> str:
    """Look up ``ENT.MAIL_PRINCIPAL`` (or MAIL_ALTERNO) for a customer.

    ``partner_id`` is the shared ``ENT.ID`` / ``ENT_ERP_CLI.ID``.
    Returns the empty string if Velneo has no email on file. The
    column can hold multiple addresses joined with ``,``/``;``/``|``;
    we pick the first one that parses as an email so smtplib doesn't
    try to deliver to a comma-mangled "address".
    """
    try:
        resp = await client.get(
            "ENT",
            record_id=partner_id,
            fields=["ID", "MAIL_PRINCIPAL", "MAIL_ALTERNO"],
        )
    except VelneoError:
        return ""
    if not resp.rows:
        return ""
    row = resp.rows[0]
    primary = _pick_first_email(row.get("MAIL_PRINCIPAL") or "")
    if primary:
        return primary
    return _pick_first_email(row.get("MAIL_ALTERNO") or "")


async def request_otp(
    client: VelneoClient,
    *,
    partner_id: int,
    channel: str,
    channel_user_id: str,
    email: str | None = None,
) -> dict[str, Any]:
    """Send a 6-digit code to the customer's email.

    If the customer already has a valid 24h verified session for this
    channel, we short-circuit with ``already_verified=true`` so the LLM
    doesn't trigger a spam loop of OTP emails.
    """
    if not partner_id:
        return {"success": False, "error_code": "missing_partner_id"}
    if not channel or not channel_user_id:
        return {
            "success": False,
            "error_code": "missing_channel_context",
            "error": (
                "No pude resolver el canal del chat. Asegurate de que el "
                "orchestrator envie los headers X-Channel y X-Channel-User-Id."
            ),
        }

    tenant_id = client.cfg.tenant_id
    ch = channel.strip().lower()

    if await check_session(tenant_id, int(partner_id), ch):
        return {
            "success": True,
            "already_verified": True,
            "message": (
                "El cliente ya tiene una sesion OTP valida. NO envies otro "
                "codigo — puedes consultar datos financieros directamente "
                "(check_balance, get_customer_invoices, get_customer_payments)."
            ),
        }

    addr = (email or "").strip()
    if not addr:
        addr = await _read_partner_email(client, int(partner_id))
    if not addr or "@" not in addr:
        return {
            "success": False,
            "error_code": "no_email_on_file",
            "error": (
                "El cliente no tiene correo en Velneo (ENT.MAIL_PRINCIPAL "
                "vacio). Pidele al cliente un email valido y registralo en "
                "su ficha antes de intentar de nuevo."
            ),
        }

    code, err = await _generate_token(
        tenant_id, int(partner_id), ch, channel_user_id.strip(),
    )
    if err:
        return {"success": False, "error": err}

    sent, send_msg = await send_otp_email(tenant_id, addr, code)
    masked = _mask_email(addr)

    if not sent:
        if send_msg == "SMTP_NOT_CONFIGURED":
            # Dev fallback so a tenant without SMTP can still be smoke-tested.
            # In production every tenant MUST have a smtp_settings row.
            return {
                "success": True,
                "message": (
                    f"[DEV] SMTP no configurado. Codigo: {code}. En "
                    f"produccion se enviaria a {masked}."
                ),
                "email_masked": masked,
                "dev_code": code,
            }
        return {
            "success": False,
            "error": f"No se pudo enviar el correo: {send_msg}",
        }
    return {
        "success": True,
        "message": f"Codigo de verificacion enviado a {masked}. Valido por 15 minutos.",
        "email_masked": masked,
    }


async def verify_otp(
    client: VelneoClient,
    *,
    partner_id: int,
    code: str,
    channel: str,
) -> dict[str, Any]:
    """Verify the 6-digit code. On success a 24h session is opened."""
    if not partner_id or not code:
        return {"success": False, "error_code": "missing_args"}
    if not channel:
        return {"success": False, "error_code": "missing_channel"}

    ok, msg = await verify_token(
        client.cfg.tenant_id, int(partner_id), channel.strip().lower(), code,
    )
    return {"success": ok, "message": msg}
