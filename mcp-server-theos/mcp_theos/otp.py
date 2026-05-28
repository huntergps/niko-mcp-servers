"""OTP verification for customer financial data access.

Mirrors the OTP pattern proven in mcp-server-odoo. The model:

1. ``request_otp(partner_id, channel, channel_user_id)`` generates a
   6-digit code, hashes it, writes a row to
   ``public.verification_tokens`` (tenant-scoped) and emails the code
   to the partner using the tenant's ``public.smtp_settings`` row.
2. The customer types the code back into the chat.
3. ``verify_otp(partner_id, code)`` checks hash + expiry + attempt
   count. On success, it inserts a ``verified_session`` row with 24h
   TTL so the customer can browse balance/invoices/payments without
   re-typing the OTP.
4. Every financial tool (``check_balance``, ``get_customer_invoices``,
   ``get_customer_payments``, ``get_customer_statement``) calls
   :func:`check_session` BEFORE returning anything; if no session is
   live, the tool refuses with :data:`OTP_REQUIRED_MSG`.

Multi-tenancy is enforced at every layer:

* The Supabase ``verification_tokens`` table partitions by
  ``tenant_id`` so two tenants can never see each other's codes.
* The email subject / body / brand line come from
  ``public.tenants.commercial_name`` resolved at call time — no
  hardcoded tenant identity. This was the bug in the older
  mcp-odoo code path (``"TecnoSmart"`` as default ``company_name``).
* SMTP credentials per tenant via ``public.smtp_settings``; a single
  row maps a tenant to its host / user / app-password.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

from mcp_theos.config import settings

logger = logging.getLogger(__name__)


OTP_REQUIRED_MSG = (
    "Esta informacion es confidencial y requiere verificacion de identidad. "
    "Usa la herramienta request_otp para enviar un codigo al correo del "
    "cliente, luego verify_otp con el codigo de 6 digitos que el cliente "
    "te proporcione."
)


def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _supa_headers() -> dict[str, str]:
    key = settings.supabase_service_key or settings.supabase_jwt_secret
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept-Profile": "public",
        "Content-Profile": "public",
        "Prefer": "return=representation",
    }


# ---------------------------------------------------------------------------
# Token lifecycle (Supabase verification_tokens table)
# ---------------------------------------------------------------------------


async def _generate_token(
    tenant_id: str,
    partner_id: int,
    channel: str,
    channel_user_id: str,
    purpose: str = "account_statement",
) -> tuple[str, str | None]:
    """Generate a 6-digit code, store its hash. Returns (code, error)."""
    code = f"{random.randint(0, 999999):06d}"
    token_hash = _hash_otp(code)
    async with httpx.AsyncClient(timeout=10) as client:
        # Invalidate previous unused tokens for the same scope so the
        # newest code is always the only valid one.
        await client.patch(
            f"{settings.supabase_url}/rest/v1/verification_tokens"
            f"?tenant_id=eq.{tenant_id}&partner_id=eq.{partner_id}"
            f"&channel=eq.{channel}&purpose=eq.{purpose}&used=eq.false",
            headers=_supa_headers(),
            json={"used": True},
        )
        resp = await client.post(
            f"{settings.supabase_url}/rest/v1/verification_tokens",
            headers=_supa_headers(),
            json={
                "tenant_id": tenant_id,
                "partner_id": partner_id,
                "channel": channel,
                "channel_user_id": channel_user_id,
                "token_hash": token_hash,
                "purpose": purpose,
            },
        )
        if resp.status_code not in (200, 201):
            return "", f"verification_tokens insert failed: {resp.text[:200]}"
    return code, None


async def verify_token(
    tenant_id: str,
    partner_id: int,
    channel: str,
    code: str,
    purpose: str = "account_statement",
) -> tuple[bool, str]:
    """Verify a code. On success, opens a 24h ``verified_session`` row."""
    token_hash = _hash_otp(code.strip())
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{settings.supabase_url}/rest/v1/verification_tokens"
            f"?tenant_id=eq.{tenant_id}&partner_id=eq.{partner_id}"
            f"&channel=eq.{channel}&purpose=eq.{purpose}&used=eq.false"
            f"&expires_at=gt.now()"
            f"&order=created_at.desc&limit=1",
            headers=_supa_headers(),
        )
        if resp.status_code != 200 or not resp.json():
            return False, "Codigo expirado o no encontrado. Solicita uno nuevo con request_otp."
        token = resp.json()[0]
        token_id = token["id"]
        attempts = token["attempts"]
        max_attempts = token["max_attempts"]

        if attempts >= max_attempts:
            await client.patch(
                f"{settings.supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
                headers=_supa_headers(), json={"used": True},
            )
            return False, "Demasiados intentos. Solicita un nuevo codigo con request_otp."

        await client.patch(
            f"{settings.supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
            headers=_supa_headers(), json={"attempts": attempts + 1},
        )

        if token["token_hash"] == token_hash:
            await client.patch(
                f"{settings.supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
                headers=_supa_headers(), json={"used": True},
            )
            # Open a 24h verified-session row. The mcp-odoo path
            # documented the importance of NOT relying on the column
            # default (which is 15 min). Always pass expires_at.
            expires_24h = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            await client.post(
                f"{settings.supabase_url}/rest/v1/verification_tokens",
                headers=_supa_headers(),
                json={
                    "tenant_id": tenant_id,
                    "partner_id": partner_id,
                    "channel": channel,
                    "channel_user_id": token["channel_user_id"],
                    "token_hash": _hash_otp(f"session-{partner_id}-{channel}"),
                    "purpose": "verified_session",
                    "used": False,
                    "expires_at": expires_24h,
                },
            )
            return True, "Codigo verificado correctamente. Acceso a datos financieros valido por 24 horas."

        remaining = max_attempts - attempts - 1
        if remaining <= 0:
            await client.patch(
                f"{settings.supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
                headers=_supa_headers(), json={"used": True},
            )
            return False, "Codigo incorrecto. No quedan intentos. Solicita uno nuevo con request_otp."
        return False, f"Codigo incorrecto. Quedan {remaining} intento(s)."


async def check_session(
    tenant_id: str,
    partner_id: int,
    channel: str,
) -> bool:
    """True iff there is a non-expired ``verified_session`` row."""
    if not channel:
        return False
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{settings.supabase_url}/rest/v1/verification_tokens"
            f"?tenant_id=eq.{tenant_id}&partner_id=eq.{partner_id}"
            f"&channel=eq.{channel}&purpose=eq.verified_session&used=eq.false"
            f"&expires_at=gt.now()&limit=1",
            headers=_supa_headers(),
        )
        return resp.status_code == 200 and len(resp.json()) > 0


# ---------------------------------------------------------------------------
# Tenant-scoped lookups
# ---------------------------------------------------------------------------


async def _get_tenant_smtp_config(tenant_id: str) -> dict[str, Any] | None:
    """Load ``public.smtp_settings`` for the tenant. None if absent."""
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.get(
            f"{settings.supabase_url}/rest/v1/smtp_settings"
            f"?tenant_id=eq.{tenant_id}&limit=1",
            headers=_supa_headers(),
        )
    if resp.status_code != 200 or not resp.json():
        return None
    return resp.json()[0]


async def _get_tenant_commercial_name(tenant_id: str) -> str:
    """Read ``public.tenants.commercial_name`` for branding in the email.

    Falls back to ``name`` if ``commercial_name`` is empty. Empty string
    on any failure — never crash the OTP flow over branding.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/tenants"
                f"?id=eq.{tenant_id}&select=name,commercial_name&limit=1",
                headers=_supa_headers(),
            )
        if resp.status_code == 200 and resp.json():
            row = resp.json()[0]
            return (row.get("commercial_name") or row.get("name") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not resolve commercial_name for tenant %s: %s", tenant_id, exc)
    return ""


# ---------------------------------------------------------------------------
# SMTP delivery
# ---------------------------------------------------------------------------


def _build_otp_email_html(code: str, company_name: str) -> tuple[str, str]:
    """Return (plain_text, html_body) for the OTP email."""
    digits = " ".join(code)
    safe_company = company_name or "Asistencia virtual"
    plain = (
        f"Hola,\n\n"
        f"Tu codigo de verificacion es: {code}\n\n"
        f"Este codigo es valido por 15 minutos. "
        f"Si no solicitaste este codigo, ignora este mensaje.\n\n"
        f"— {safe_company}\n"
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:40px 0;">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:linear-gradient(135deg,#0ea5e9,#6366f1);padding:32px 40px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;">{safe_company}</h1>
          <p style="margin:8px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">Verificacion de identidad</p>
        </td></tr>
        <tr><td style="padding:40px;">
          <p style="color:#374151;font-size:16px;line-height:1.6;margin:0 0 24px;">
            Has solicitado un codigo para acceder a tu cuenta. Ingresalo en el chat:
          </p>
          <div style="background:#f0f9ff;border:2px dashed #0ea5e9;border-radius:12px;padding:24px;text-align:center;margin:0 0 24px;">
            <span style="font-size:32px;font-weight:800;letter-spacing:6px;color:#0ea5e9;font-family:monospace;white-space:nowrap;">{digits}</span>
          </div>
          <p style="color:#6b7280;font-size:13px;line-height:1.5;margin:0 0 8px;">
            ⏱ Este codigo es valido por <strong>15 minutos</strong>.
          </p>
          <p style="color:#6b7280;font-size:13px;line-height:1.5;margin:0;">
            🔒 Si no solicitaste este codigo, ignora este mensaje.
          </p>
        </td></tr>
        <tr><td style="background:#f9fafb;padding:20px 40px;border-top:1px solid #e5e7eb;">
          <p style="color:#9ca3af;font-size:11px;text-align:center;margin:0;">
            Correo automatico enviado por {safe_company}. No respondas a este mensaje.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    return plain, html


async def send_otp_email(
    tenant_id: str,
    to_email: str,
    code: str,
) -> tuple[bool, str]:
    """Send OTP via the tenant's SMTP. Returns (success, message).

    Resolves ``commercial_name`` from ``public.tenants`` and SMTP creds
    from ``public.smtp_settings`` — both scoped by tenant_id. Falls
    back to the legacy env-var SMTP only if neither row exists, which
    keeps single-tenant dev setups working.
    """
    smtp = await _get_tenant_smtp_config(tenant_id)
    company_name = await _get_tenant_commercial_name(tenant_id)

    if smtp:
        host = smtp.get("smtp_host", "")
        port = int(smtp.get("smtp_port", 587))
        user = smtp.get("smtp_user", "")
        password = smtp.get("smtp_password", "")
        from_addr = smtp.get("smtp_from") or user
        tls = bool(smtp.get("smtp_tls", True))
    else:
        host = os.environ.get("SMTP_HOST", "")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        from_addr = os.environ.get("SMTP_FROM", user)
        tls = os.environ.get("SMTP_TLS", "true").lower() in ("true", "1", "yes")

    if not host or not user:
        return False, "SMTP_NOT_CONFIGURED"

    plain, html = _build_otp_email_html(code, company_name)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Codigo de verificacion {company_name}".strip()
    msg["From"] = f"{company_name} <{from_addr}>" if company_name else from_addr
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                if tls:
                    server.starttls()
                server.login(user, password)
                server.send_message(msg)
        return True, "OK"
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP send failed: {exc}"
