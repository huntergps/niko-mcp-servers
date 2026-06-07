"""MCP Standard Transport — StreamableHTTP endpoint for Hermes/Claude integration.

Exposes all Odoo tools + RAG search as standard MCP tools.
Hermes connects with: url: "http://mcp-odoo:8080/mcp"

Multi-tenant: each MCP session carries a tenant JWT in headers.
"""

import json
import logging
import socket
import xmlrpc.client

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# PostgREST helper — ALWAYS include schema profile headers
# ---------------------------------------------------------------------------

def _postgrest_headers(supabase_key: str, *, schema: str = "public",
                       content_type: str = "application/json",
                       prefer: str = "") -> dict[str, str]:
    """Build PostgREST headers with correct schema profile.

    PostgREST defaults to the first schema in PGRST_DB_SCHEMAS which may
    be 'storage' instead of 'public'. Always specify Accept-Profile and
    Content-Profile to avoid 'table not found in schema cache' errors.
    """
    h: dict[str, str] = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept-Profile": schema,
        "Content-Profile": schema,
    }
    if content_type:
        h["Content-Type"] = content_type
    if prefer:
        h["Prefer"] = prefer
    return h


# ---------------------------------------------------------------------------
# Ecuador cedula / RUC validation
# ---------------------------------------------------------------------------
# Implementation lives in ``mcp_odoo.tools.ec_id`` so it can be imported and
# unit-tested in isolation. The thin wrappers below preserve the legacy names
# used elsewhere in this module.

from mcp_odoo.tools import ec_id as _ec_id

_validate_cedula_ecuador = _ec_id.validate_cedula_ecuador
_validate_ruc_ecuador = _ec_id.validate_ruc_ecuador
_validate_cedula_or_ruc = _ec_id.validate_cedula_or_ruc


# ---------------------------------------------------------------------------
# Error taxonomy — user-friendly Spanish messages
# ---------------------------------------------------------------------------

_ERROR_MAP = {
    "connection": "El sistema de inventario no esta disponible temporalmente. Intente en unos minutos.",
    "timeout": "La consulta esta tardando mas de lo esperado. Intente de nuevo.",
    "product_not_found": "No encontre productos con esa descripcion. Puede intentar con otros terminos?",
    "partner_not_found": "No encontre un cliente con esa cedula/RUC.",
    "sri_error": "Hubo un problema al procesar la clave SRI. Verifique que los 49 digitos sean correctos.",
    "auth_error": "Error de autenticacion con el sistema. Contacte al administrador.",
    "unknown": "Ocurrio un error inesperado. Intente de nuevo o contacte al administrador.",
}


def _classify_error(e: Exception, tool_name: str = "") -> str:
    """Classify an exception into a user-friendly Spanish error message."""
    error_str = str(e).lower()
    error_type = type(e).__name__

    # Connection errors
    if isinstance(e, (ConnectionError, ConnectionRefusedError, socket.error)):
        return _ERROR_MAP["connection"]
    if "connection" in error_str or "refused" in error_str or "unreachable" in error_str:
        return _ERROR_MAP["connection"]
    if isinstance(e, xmlrpc.client.ProtocolError):
        return _ERROR_MAP["connection"]

    # Timeout errors
    if isinstance(e, (TimeoutError, socket.timeout)):
        return _ERROR_MAP["timeout"]
    if "timeout" in error_str or "timed out" in error_str:
        return _ERROR_MAP["timeout"]

    # Authentication
    if "authentication failed" in error_str or "access denied" in error_str:
        return _ERROR_MAP["auth_error"]

    # SRI-specific
    if tool_name == "sri_import":
        if "clave" in error_str or "digito" in error_str:
            return _ERROR_MAP["sri_error"]

    return f"{_ERROR_MAP['unknown']} ({error_type})"


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# OTP verification for financial data access
# ---------------------------------------------------------------------------

import hashlib
import random
import smtplib
from email.mime.text import MIMEText

BLOCKED_MODELS = {
    "account.move", "account.invoice", "account.payment",
    "account.move.line", "account.bank.statement",
    "account.bank.statement.line", "account.analytic.line",
}

BLOCKED_PARTNER_FIELDS = {
    "credit", "debit", "total_due", "total_overdue",
    "amount_residual", "balance", "credit_limit",
}

OTP_REQUIRED_MSG = (
    "Esta informacion requiere verificacion de identidad. "
    "Use la herramienta request_otp para enviar un codigo al correo del cliente, "
    "luego verify_otp con el codigo que el cliente proporcione."
)


def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def _otp_generate(supabase_url: str, supabase_key: str, tenant_id: str,
                         partner_id: int, channel: str, channel_user_id: str,
                         purpose: str = "account_statement") -> tuple[str, str | None]:
    """Generate 6-digit OTP, store hash in Supabase. Returns (code, error)."""
    import httpx
    code = f"{random.randint(0, 999999):06d}"
    token_hash = _hash_otp(code)
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
        "Accept-Profile": "public",
        "Content-Profile": "public",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        # Invalidate previous unused tokens
        await client.patch(
            f"{supabase_url}/rest/v1/verification_tokens"
            f"?tenant_id=eq.{tenant_id}&partner_id=eq.{partner_id}"
            f"&channel=eq.{channel}&purpose=eq.{purpose}&used=eq.false",
            headers=headers, json={"used": True},
        )
        # Create new token
        resp = await client.post(
            f"{supabase_url}/rest/v1/verification_tokens",
            headers=headers,
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
            return "", f"Error creando codigo: {resp.text[:200]}"
    return code, None


async def _otp_verify(supabase_url: str, supabase_key: str, tenant_id: str,
                       partner_id: int, channel: str, code: str,
                       purpose: str = "account_statement") -> tuple[bool, str]:
    """Verify OTP code. Returns (success, message)."""
    import httpx
    token_hash = _hash_otp(code.strip())
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept-Profile": "public",
        "Content-Profile": "public",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{supabase_url}/rest/v1/verification_tokens"
            f"?tenant_id=eq.{tenant_id}&partner_id=eq.{partner_id}"
            f"&channel=eq.{channel}&purpose=eq.{purpose}&used=eq.false"
            f"&expires_at=gt.now()"
            f"&order=created_at.desc&limit=1",
            headers=headers,
        )
        if resp.status_code != 200 or not resp.json():
            return False, "Codigo expirado o no encontrado. Solicite uno nuevo con request_otp."

        token = resp.json()[0]
        token_id = token["id"]
        attempts = token["attempts"]
        max_attempts = token["max_attempts"]

        if attempts >= max_attempts:
            await client.patch(
                f"{supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
                headers=headers, json={"used": True},
            )
            return False, "Demasiados intentos. Solicite un nuevo codigo con request_otp."

        # Increment attempts
        await client.patch(
            f"{supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
            headers=headers, json={"attempts": attempts + 1},
        )

        if token["token_hash"] == token_hash:
            # Mark as used, return success
            await client.patch(
                f"{supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
                headers=headers, json={"used": True},
            )
            # Iter 80b: verified_session debe durar 24h (era 15 min por
            # bug — el comentario decia 24h pero la insercion no setea
            # expires_at y la columna tiene DEFAULT now()+15min). Owner-
            # evidencia (WhatsApp 2026-05-23): cliente verifico OTP a las
            # 01:15, a las 01:17 le pidio estado de cuenta y el bot le
            # volvio a pedir OTP — porque expires_at=01:30 era de 15min.
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            _expires_24h = (_dt.now(_tz.utc) + _td(hours=24)).isoformat()
            session_resp = await client.post(
                f"{supabase_url}/rest/v1/verification_tokens",
                headers=headers,
                json={
                    "tenant_id": tenant_id,
                    "partner_id": partner_id,
                    "channel": channel,
                    "channel_user_id": token["channel_user_id"],
                    "token_hash": _hash_otp(f"session-{partner_id}-{channel}"),
                    "purpose": "verified_session",
                    "used": False,
                    "expires_at": _expires_24h,
                },
            )
            return True, "Codigo verificado correctamente. Acceso a datos financieros valido por 24 horas."

        remaining = max_attempts - attempts - 1
        if remaining <= 0:
            await client.patch(
                f"{supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
                headers=headers, json={"used": True},
            )
            return False, "Codigo incorrecto. No quedan intentos. Solicite uno nuevo."
        return False, f"Codigo incorrecto. Quedan {remaining} intento(s)."


def _assert_quotation_editable_by_order(
    creds: tuple, order_id: int,
) -> dict | None:
    """Return None when the target sale.order is editable (draft/sent),
    else a dict-shaped error envelope the dispatch can return verbatim.

    Iter 89 (owner audit 2026-05-25 trace bb582877): the orchestrator
    persisted approved/sale/done/cancel quotes as "active", and the LLM
    happily called add_quotation_line on a 3-day-old VENTA123456 that
    was already approved in Odoo. add/update/remove now refuses to
    touch a locked sale.order BEFORE we hit Odoo's xmlrpc write so the
    customer sees a useful error instead of an opaque server failure.
    """
    try:
        from mcp_odoo.tools.generic import odoo_read
        rows = odoo_read(*creds, "sale.order", [int(order_id)], ["name", "state"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("editable check: odoo_read failed id=%s: %s", order_id, exc)
        return None  # let the underlying tool report the real failure
    if not rows:
        return {
            "success": False,
            "error_code": "quote_not_found",
            "error_detail": f"sale.order id={order_id} no existe.",
        }
    state = (rows[0].get("state") or "").strip().lower()
    name = rows[0].get("name") or f"VENTA{order_id}"
    if state in {"draft", "sent"}:
        return None
    return {
        "success": False,
        "error_code": "quote_locked",
        "error_detail": (
            f"La cotización {name} (id={order_id}) está en estado "
            f"'{state}' y NO se puede modificar. Si el cliente quiere "
            f"agregar/cambiar productos, ofrece duplicarla con "
            f"duplicate_quotation o crear una nueva con create_quotation."
        ),
        "order_id": int(order_id),
        "name": name,
        "state": state,
    }


def _assert_quotation_editable_by_line(
    creds: tuple, line_id: int,
) -> dict | None:
    """Same as _by_order, but resolves the order from a line_id first."""
    try:
        from mcp_odoo.tools.generic import odoo_read
        line_rows = odoo_read(
            *creds, "sale.order.line", [int(line_id)], ["order_id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("editable check by_line: odoo_read failed: %s", exc)
        return None
    if not line_rows:
        return {
            "success": False,
            "error_code": "line_not_found",
            "error_detail": f"sale.order.line id={line_id} no existe.",
        }
    order_link = line_rows[0].get("order_id")
    if isinstance(order_link, (list, tuple)) and order_link:
        return _assert_quotation_editable_by_order(creds, int(order_link[0]))
    return None


async def _otp_check_session(supabase_url: str, supabase_key: str, tenant_id: str,
                              partner_id: int, channel: str) -> bool:
    """Check if there is a valid verified session (24h window)."""
    import httpx
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept-Profile": "public",
        "Content-Profile": "public",
    }
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{supabase_url}/rest/v1/verification_tokens"
            f"?tenant_id=eq.{tenant_id}&partner_id=eq.{partner_id}"
            f"&channel=eq.{channel}&purpose=eq.verified_session&used=eq.false"
            f"&expires_at=gt.now()"
            f"&limit=1",
            headers=headers,
        )
        return resp.status_code == 200 and len(resp.json()) > 0


def _get_tenant_smtp_config(tenant_id: str | None, supa_url: str | None = None, supa_key: str | None = None) -> dict | None:
    """Try to load SMTP config from public.smtp_settings for the given tenant.

    Returns dict with smtp_host, smtp_port, smtp_user, smtp_password, smtp_from, smtp_tls
    or None if not found.
    """
    if not tenant_id:
        return None
    try:
        import os
        _url = supa_url or os.environ.get("SUPABASE_URL", "http://localhost:8000")
        _key = supa_key or os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not _key:
            return None
        from supabase import create_client
        sb = create_client(_url, _key)
        result = sb.table("smtp_settings").select("*").eq("tenant_id", tenant_id).limit(1).execute()
        if result.data:
            return result.data[0]
    except Exception as exc:
        logger.warning("Failed to load tenant SMTP config for %s: %s", tenant_id, exc)
    return None


def _resolve_tenant_brand(tenant_id: str | None, supa_url: str | None = None, supa_key: str | None = None) -> str:
    """Read public.tenants.commercial_name (fallback name).

    Never hardcode a brand for OTP emails — this function runs once
    per send and stays cheap because it only hits public.tenants
    which is exposed via PostgREST.
    """
    if not tenant_id:
        return ""
    try:
        import os as _os
        _url = supa_url or _os.environ.get("SUPABASE_URL", "http://localhost:8000")
        _key = supa_key or _os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not _key:
            return ""
        from supabase import create_client
        sb = create_client(_url, _key)
        result = (
            sb.table("tenants")
              .select("name,commercial_name")
              .eq("id", tenant_id)
              .limit(1)
              .execute()
        )
        if result.data:
            row = result.data[0]
            return (row.get("commercial_name") or row.get("name") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load tenant brand for %s: %s", tenant_id, exc)
    return ""


def _send_otp_email(email: str, code: str, company_name: str | None = None, tenant_id: str | None = None, supa_url: str | None = None, supa_key: str | None = None) -> tuple[bool, str]:
    """Send OTP via SMTP with HTML template. Returns (success, message).

    First tries tenant-specific SMTP config from Supabase, falls back to env vars.
    The ``company_name`` defaults to ``public.tenants.commercial_name`` — never
    hardcoded to a particular brand. Pass a value explicitly only when an
    override is genuinely needed.
    """
    import os
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText as _MIMEText

    if not company_name:
        company_name = _resolve_tenant_brand(tenant_id, supa_url, supa_key) or "Asistencia"

    # Try tenant-specific SMTP config first
    tenant_smtp = _get_tenant_smtp_config(tenant_id, supa_url, supa_key)
    if tenant_smtp:
        smtp_host = tenant_smtp.get("smtp_host", "")
        smtp_port = int(tenant_smtp.get("smtp_port", 587))
        smtp_user = tenant_smtp.get("smtp_user", "")
        smtp_password = tenant_smtp.get("smtp_password", "")
        smtp_from = tenant_smtp.get("smtp_from", smtp_user)
        smtp_tls = tenant_smtp.get("smtp_tls", True)
    else:
        # Fallback to environment variables
        smtp_host = os.environ.get("SMTP_HOST", "")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_password = os.environ.get("SMTP_PASSWORD", "")
        smtp_from = os.environ.get("SMTP_FROM", smtp_user)
        smtp_tls = os.environ.get("SMTP_TLS", "true").lower() in ("true", "1", "yes")

    if not smtp_host or not smtp_user:
        return False, "SMTP_NOT_CONFIGURED"

    # Digits with spaces for readability but compact enough for one line
    digits = " ".join(code)

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:40px 0;">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#0ea5e9,#6366f1);padding:32px 40px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;">{company_name}</h1>
          <p style="margin:8px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">Verificacion de identidad</p>
        </td></tr>
        <!-- Body -->
        <tr><td style="padding:40px;">
          <p style="color:#374151;font-size:16px;line-height:1.6;margin:0 0 24px;">
            Hola, has solicitado un codigo de verificacion para acceder a tu cuenta.
            Ingresa el siguiente codigo en el chat:
          </p>
          <!-- Code box -->
          <div style="background:#f0f9ff;border:2px dashed #0ea5e9;border-radius:12px;padding:24px;text-align:center;margin:0 0 24px;">
            <span style="font-size:32px;font-weight:800;letter-spacing:6px;color:#0ea5e9;font-family:monospace;white-space:nowrap;">{digits}</span>
          </div>
          <p style="color:#6b7280;font-size:13px;line-height:1.5;margin:0 0 8px;">
            ⏱ Este codigo es valido por <strong>5 minutos</strong>.
          </p>
          <p style="color:#6b7280;font-size:13px;line-height:1.5;margin:0;">
            🔒 Si no solicitaste este codigo, puedes ignorar este mensaje.
          </p>
        </td></tr>
        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:20px 40px;border-top:1px solid #e5e7eb;">
          <p style="color:#9ca3af;font-size:11px;text-align:center;margin:0;">
            Este correo fue enviado automaticamente por {company_name} a traves de Niko AI.
            No responda a este mensaje.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    plain = (
        f"Tu codigo de verificacion es: {code}\n\n"
        f"Este codigo es valido por 5 minutos.\n"
        f"Si no solicitaste este codigo, ignora este mensaje.\n\n"
        f"— {company_name}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Codigo de verificacion {company_name}"
    msg["From"] = f"{company_name} <{smtp_from}>"
    msg["To"] = email
    msg.attach(_MIMEText(plain, "plain", "utf-8"))
    msg.attach(_MIMEText(html, "html", "utf-8"))

    try:
        if smtp_port == 465:
            # SSL/TLS on connect (Zoho, etc.)
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            # STARTTLS (Gmail port 587, etc.)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                if smtp_tls:
                    server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        return True, "OK"
    except Exception as e:
        return False, str(e)


# Tool definitions in MCP format
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "search_products",
        "description": (
            "TOOL PRINCIPAL para preguntas de catalogo. USALA SIEMPRE que "
            "el cliente mencione un producto, marca, modelo, categoria, "
            "precio o disponibilidad. Ejemplos OBLIGATORIOS: "
            "'busco un martillo', 'tienen taladros?', 'cuanto cuesta X', "
            "'que laptops tienen', 'hay impresoras Epson', 'manejan SSD?', "
            "'precio de la silla gamer'. NO uses get_business_info ni "
            "get_company_settings para estas preguntas — esas son para "
            "datos publicos de la empresa (direccion, horarios), no "
            "para productos. "
            "Busqueda hibrida (semantica + literal). Devuelve PAGINADO: "
            "la primera llamada trae los primeros top_k productos "
            "(default 10) PRIORIZANDO los que tienen stock. Si el cliente "
            "pide ver mas, llama de nuevo con el MISMO query y offset=10 "
            "para los siguientes 10. Resultados con precio y stock LIVE "
            "de Odoo. Cuando el cliente menciona un presupuesto numerico "
            "('maximo 500', 'hasta 800', 'entre 200 y 500', 'por 1000'), "
            "pasa price_max y/o price_min para filtrar — NO presentes "
            "productos sobre el limite del cliente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto de busqueda natural (ej: 'laptop para oficina', 'tinta epson', 'procesador')"},
                "top_k": {"type": "integer", "description": "Resultados por pagina (default 10). NO hay limite maximo — pide todos los que necesites; el sistema devuelve hasta lo que encuentre el catalogo.", "default": 10},
                "offset": {"type": "integer", "description": "Offset para paginacion. Usa 0 en la primera llamada, top_k en la siguiente, etc.", "default": 0},
                "price_min": {"type": "number", "description": "Precio minimo en USD (inclusivo). Usar cuando el cliente pide 'desde X' o 'entre X y Y'. Opcional."},
                "price_max": {"type": "number", "description": "Precio maximo en USD (inclusivo). Usar cuando el cliente menciona presupuesto: 'maximo 500', 'hasta 800', 'por 1000', 'entre 200 y 500'. Opcional pero CRITICO si hay presupuesto explicito — items sobre este monto NO se presentan al cliente."},
                "category_path": {"type": "string", "description": "Filtro por categoria Odoo (categ_id.complete_name, busqueda parcial ilike). Usar cuando el cliente nombra una categoria especifica para EVITAR ruido: 'Laptops', 'Monitores', 'Computadoras', 'Impresoras'. Cuando se proporciona, solo se devuelven productos cuya categoria contiene este texto. Opcional."},
                "partner_id": {"type": "integer", "description": "Cliente identificado (res.partner.id). El orchestrator lo inyecta automáticamente cuando el cliente ya está identificado en la sesión — no necesitas pasarlo a mano. Cuando viene, los precios se calculan con la lista de precios (pricelist) configurada para ese cliente en Odoo; sin él, se devuelve el precio público (list_price). Esto evita que el catálogo muestre $206 y la cotización cobre $229 al mismo cliente B2B con tarifa especial."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product_details",
        "description": (
            "Obtener detalles completos de un producto por su codigo interno (default_code). "
            "Retorna nombre, precio, stock, descripcion, categoria e image_url (URL de la imagen "
            "del producto en Odoo, si tiene imagen). "
            "Usar cuando necesites informacion exacta de un producto antes de armar una cotizacion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_code": {"type": "string", "description": "Codigo interno del producto (default_code)"},
                "partner_id": {"type": "integer", "description": "Cliente identificado (res.partner.id). El orchestrator lo inyecta automáticamente — no necesitas pasarlo. Cuando viene, ``price`` refleja la pricelist del cliente y se agrega ``list_price`` si difiere del precio público."},
            },
            "required": ["product_code"],
        },
    },
    {
        "name": "odoo_search",
        "description": "Buscar registros en Odoo por nombre exacto o parcial. Usar para busquedas por codigo, nombre exacto, o cuando RAG no encuentra resultados.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Modelo Odoo (ej: product.product, res.partner, sale.order)"},
                "domain": {"type": "array", "description": "Dominio de busqueda Odoo (ej: [['name','ilike','laptop']])"},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Campos a retornar"},
                "limit": {"type": "integer", "description": "Maximo de resultados", "default": 10},
            },
            "required": ["model", "domain"],
        },
    },
    {
        "name": "check_stock",
        "description": "Consultar stock disponible de productos por sus IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_ids": {"type": "array", "items": {"type": "integer"}, "description": "IDs de productos"},
            },
            "required": ["product_ids"],
        },
    },
    {
        "name": "sri_status_report",
        "description": (
            "Informe de ESTADO de documentos electronicos SRI (clasificado). "
            "USAR SIEMPRE para: 'estado de (los) documentos electronicos', "
            "'informe SRI', 'cola SRI', 'como va el SRI', 'documentos pendientes "
            "de autorizar / no enviados / devueltos', 'saneamiento de documentos'. "
            "Devuelve dos secciones listas para mostrar TAL CUAL al usuario: "
            "PENDIENTES de enviar al SRI (ventas accionables, compras SOLO con "
            "liquidacion 03 o retencion real, guias por fechaemision; NC compra "
            "en silencio) + INFORMATIVO (borradores/cancelados para contabilidad). "
            "NO improvisar con odoo_search/aggregate_records/search_count — ESTE "
            "tool ya clasifica bien y evita los falsos positivos. Ventana 7 dias."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_partner",
        "description": (
            "Buscar un cliente, proveedor o contacto por nombre, empresa, ciudad o cualquier dato. "
            "Usa busqueda semantica (RAG). Usa esto cuando alguien mencione un nombre de persona o empresa "
            "para encontrar su ficha en el sistema. Ejemplo: 'Megapriga', 'Juan Perez', 'TecnoSmart'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Nombre, empresa, ciudad o cualquier dato del contacto"},
                "top_k": {"type": "integer", "description": "Numero de resultados", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_partner_profile",
        "description": (
            "Leer perfil completo del cliente cuando ya tienes su `partner_id` "
            "en sesion. Devuelve nombre, email, telefono/movil, RUC/cedula, "
            "direccion. Si pasas `include_activity=true` tambien devuelve sus "
            "ultimas cotizaciones, facturas recientes, productos mas comprados "
            "y ticket promedio — todo en un solo call. El campo `display_text` "
            "incluye una version pre-formateada WhatsApp-friendly que puedes "
            "copiar TAL CUAL al cliente. "
            "Usala cuando el cliente pregunte cosas como '¿que sabes de mi?', "
            "'¿que datos tienes mios?', '¿cual es mi correo registrado?', "
            "'¿que he comprado?', '¿cuales son mis ultimas cotizaciones?'. "
            "Para esos casos pasa SIEMPRE include_activity=true para una "
            "respuesta completa. "
            "Diferente de search_partner: esta lee por ID exacto."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID numerico del cliente (res.partner.id) en Odoo"},
                "include_activity": {
                    "type": "boolean",
                    "description": (
                        "Si true (default), anade ultimas cotizaciones, "
                        "facturas, top productos y ticket promedio. Pasa "
                        "false EXPLICITAMENTE si solo necesitas datos de "
                        "contacto y quieres ahorrar latencia/tokens (caso "
                        "raro). Para preguntas '¿que sabes de mi?' deja el "
                        "default true."
                    ),
                    "default": True,
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "(Opcional) Campos especificos a leer del partner. "
                        "Default incluye un set canonico (name, email, phone, "
                        "mobile, vat, address). Pide solo lo que necesites."
                    ),
                    "default": [],
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "identify_customer",
        "description": (
            "Identificar un cliente por su cedula (10 digitos) o RUC (13 digitos). "
            "Valida el formato ecuatoriano (modulo-10 para cedula), busca en Odoo, "
            "y retorna datos del cliente incluyendo limite de credito y saldo. "
            "Si no existe, sugiere crear el cliente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cedula_ruc": {"type": "string", "description": "Cedula (10 digitos) o RUC (13 digitos) del cliente"},
            },
            "required": ["cedula_ruc"],
        },
    },
    {
        "name": "create_quotation",
        "description": (
            "Crear una cotizacion/proforma en Odoo para un cliente con lineas de productos. "
            "Cada linea acepta `product_id` (entero, template_id de Odoo) O `code` (string, "
            "default_code visible como 'MON0026', 'LAP0176'). Si pasas `code` y NO `product_id`, "
            "el backend resuelve internamente el template_id por default_code exacto — esta es la "
            "forma PREFERIDA porque elimina la posibilidad de inventar template_ids. Si pasas "
            "ambos, el backend valida que coincidan (error `product_code_mismatch` si no). "
            "IMPORTANTE: Antes de llamar esta herramienta, confirma con el cliente el resumen "
            "de productos, cantidades y precios. "
            "Para ventas a CONSUMIDOR FINAL (RUC 9999999999999): si la empresa requiere datos "
            "del consumidor final, pasa end_customer_name, end_customer_phone y end_customer_email. "
            "En flujos B2B (agente con vendedor autenticado), pasa `salesperson_user_id` con el "
            "odoo_user_id del vendedor para que Odoo le atribuya la comisión correspondiente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer", "description": "template_id de Odoo (product.template.id). Alternativa a `code`."},
                            "code": {"type": "string", "description": "Código visible del producto (default_code en Odoo, ej. 'MON0026'). Alternativa a `product_id`. Si pasas code y no product_id, el backend resuelve internamente. Si pasas ambos, el backend valida consistencia."},
                            "quantity": {"type": "number", "default": 1},
                            "price_unit": {"type": "number", "description": "Precio unitario manual. Si se omite, usa el precio de lista."},
                            "discount": {"type": "number", "description": "Descuento en porcentaje (0-100)."},
                        },
                        # product_id OR code (al menos uno) — la validación es en el backend.
                        "required": [],
                    },
                },
                "notes": {"type": "string", "description": "Notas adicionales", "default": ""},
                "end_customer_name": {"type": "string", "description": "Nombre del consumidor final (solo para ventas a consumidor final)"},
                "end_customer_phone": {"type": "string", "description": "Telefono del consumidor final"},
                "end_customer_email": {"type": "string", "description": "Email del consumidor final"},
                "salesperson_user_id": {
                    "type": "integer",
                    "description": (
                        "ID del vendedor asignado (Odoo res.users). Opcional. Cuando viene, "
                        "Odoo lo usa para calcular comisiones (sale.order.user_id). En B2B, "
                        "el agente lo pasa con el odoo_user_id del vendedor autenticado."
                    ),
                },
            },
            "required": ["partner_id", "lines"],
        },
    },
    {
        "name": "add_to_quotation",
        "description": (
            "Agregar productos a una cotización EXISTENTE en estado borrador. REQUIERE order_id válido. "
            "Cada linea acepta `product_id` (entero, template_id) O `code` (string, default_code "
            "visible como 'MON0026'). Si pasas `code` solo, el backend resuelve el template_id "
            "internamente — PREFERIDO para evitar inventar IDs. Si pasas ambos, el backend valida "
            "consistencia. "
            "Si NO tienes order_id: (1) revisa el SystemMessage de cotización activa; (2) si el usuario dijo "
            "'última/penúltima/etc', llama get_latest_quotation primero; (3) si no es claro, PREGUNTA al "
            "usuario. NUNCA inventes order_id. NUNCA llames list_quotations para 'encontrar' un order_id "
            "cuando ya tienes uno en el contexto. "
            "FLUJO OBLIGATORIO: llama primero con confirmed=false para obtener un preview. "
            "Muéstraselo al usuario. Solo llama con confirmed=true tras recibir confirmación explícita "
            "('sí', 'confirmo', 'dale'). "
            "En flujos B2B puedes pasar `salesperson_user_id`; el backend SOLO lo escribirá si la "
            "cotización todavía no tiene vendedor asignado (nunca sobreescribe al vendedor existente). "
            "Si recibes 'VENTAxxxxxx' del cliente o de otra tool, NUNCA extraigas los dígitos como "
            "order_id — usa find_quotation_by_name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": (
                        "ID INTERNO de sale.order (ej. 113604). REGLA CRÍTICA: el `name` "
                        "humano (VENTA122173) tiene un sufijo numérico (122173) que NO es el "
                        "order_id. NUNCA pases el sufijo del name como order_id. Si solo "
                        "conoces el name (formato 'VENTA' + 6 dígitos), llama "
                        "find_quotation_by_name(name='VENTA122173') primero para obtener el "
                        "order_id real."
                    ),
                },
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer", "description": "template_id del producto. Alternativa a `code`."},
                            "code": {"type": "string", "description": "Código visible del producto (default_code en Odoo, ej. 'MON0026'). Alternativa a product_id. Si pasas code y no product_id, el backend resuelve internamente."},
                            "quantity": {"type": "number", "default": 1},
                        },
                        # product_id OR code (al menos uno) — la validación es en el backend.
                        "required": [],
                    },
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Pon true SOLO después de mostrar el preview al usuario y recibir confirmación explícita. Default: false (retorna preview solamente).",
                    "default": False,
                },
                "salesperson_user_id": {
                    "type": "integer",
                    "description": (
                        "ID del vendedor (Odoo res.users). Opcional. Solo se aplica si la cotización "
                        "todavía no tiene vendedor asignado — nunca sobreescribe al existente. En B2B, "
                        "el agente lo pasa con el odoo_user_id del vendedor autenticado."
                    ),
                },
            },
            "required": ["order_id", "lines"],
        },
    },
    {
        "name": "get_active_quotation",
        "description": (
            "DEPRECATED en flujos nuevos. Solo úsala como fallback cuando no tienes order_id ni "
            "el usuario te dio una pista ordinal. Para 'mi última proforma' usa get_latest_quotation. "
            "Si el SystemMessage te dice 'NO HAY COTIZACIÓN ACTIVA EN ESTA SESIÓN', NO uses esta — "
            "PREGUNTA al usuario qué quiere hacer. Devuelve {success, order_id, name, total, lines} o "
            "{success:false, error_code:'no_active_quote'}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "list_quotations",
        "description": (
            "Listar VARIAS cotizaciones recientes (PLURAL) — formato COMPACTO (cabecera + totales, "
            "SIN líneas). USA cuando el usuario pida VER múltiples ('muéstrame mis cotizaciones', "
            "'qué cotizaciones tengo', 'los últimos productos que cotizé/proformé', 'mis últimas N "
            "cotizaciones', 'qué he proformado'). Si el usuario quiere ver LOS PRODUCTOS de varias "
            "cotizaciones, usa list_quotations con limit=N y luego get_quotation por cada order_id "
            "para traer las líneas. NO USES cuando el usuario pida UNA SOLA (singular: 'mi última', "
            "'la más reciente') — para ese caso usa get_latest_quotation. NO USES cuando el usuario "
            "quiere AGREGAR/MODIFICAR algo: para 'agregar a la última' usa get_latest_quotation; "
            "para 'agregar a la activa' usa el order_id del SystemMessage. NO USES si ya tienes "
            "order_id en el contexto. Devuelve {orders:[{order_id, name, state, state_label, total, "
            "subtotal, date_order, lines_count}]}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "limit": {"type": "integer", "description": "Maximo de cotizaciones a retornar (default 10)", "default": 10},
                "states": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filtrar por estado: draft=proforma/borrador, sent=enviada, sale=confirmada, done=hecha. Por default todas excepto canceladas. Cuando el cliente pide 'ultima proforma' usa ['draft','sent'].",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "get_quotation",
        "description": (
            "Leer el detalle COMPLETO (cabecera + todas las líneas) de UNA cotización por order_id. "
            "Usa SOLO cuando el cliente pida ver el detalle de una cotización específica "
            "('muéstrame VENTA120704', 'qué tiene esa cotización'). Para listar las cotizaciones "
            "recientes usa list_quotations. Para obtener 'la última proforma' usa "
            "get_latest_quotation, NO get_quotation con order_id adivinado. "
            "Si recibes 'VENTAxxxxxx' del cliente o de otra tool, NUNCA extraigas los dígitos "
            "como order_id — usa find_quotation_by_name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": (
                        "ID INTERNO de sale.order (ej. 113604). REGLA CRÍTICA: el `name` "
                        "humano (VENTA122173) tiene un sufijo numérico (122173) que NO es el "
                        "order_id. NUNCA pases el sufijo del name como order_id. Si solo "
                        "conoces el name (formato 'VENTA' + 6 dígitos), llama "
                        "find_quotation_by_name(name='VENTA122173') primero para obtener el "
                        "order_id real."
                    ),
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "odoo_lookup_user_by_email",
        "description": (
            "Verificar si un usuario existe en Odoo buscando por login (username) "
            "O por correo del partner. Uso EXCLUSIVO del flujo /login — NO usar "
            "para buscar clientes. Devuelve {success, user:{user_id,name,login,email}} "
            "o {success:false, error_code:'user_not_found'}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Username de Odoo (ej. elmer.salazar) o correo (ej. user@empresa.com)",
                },
            },
            "required": ["email"],
        },
    },
    {
        "name": "find_quotation_by_name",
        "description": (
            "Resolver una cotizacion por su name humano (ej. 'VENTA122172', "
            "'S0001234') al order_id numerico real (ej. 113603). USAR ESTA "
            "TOOL siempre que tengas el name pero NO el order_id, antes de "
            "llamar tools de mutacion (update_quotation_line, "
            "remove_quotation_line, transition_quotation, etc). Acepta el "
            "name con o sin mayusculas. Devuelve {order_id, name, state, "
            "partner, amount_total} o error_code='not_found'/'ambiguous'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name del sale.order (ej. 'VENTA122172').",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_latest_quotation",
        "description": (
            "Devuelve UNA SOLA cotización (la más reciente) con detalle completo. "
            "PREFIÉRELA sobre get_active_quotation y list_quotations cuando el usuario use cualquier "
            "ordinal SINGULAR: 'mi última proforma' (singular), 'la más reciente', 'la nueva', "
            "'la última cotización', 'agrégalo a la última', 'mi pedido más reciente'. "
            "**NO LA USES** cuando el usuario pida MÚLTIPLES (plural): 'mis últimas N cotizaciones', "
            "'los últimos productos que cotizé/proformé', 'qué he proformado en los últimos días', "
            "'mis cotizaciones recientes' — para esos casos usa **list_quotations** con limit=N y "
            "agrega las líneas de cada orden con get_quotation si necesitas el detalle. "
            "Internamente lista (limit=1) y devuelve detalle completo. "
            "Combinable con add_to_quotation: get_latest_quotation → tomas el order_id "
            "→ add_to_quotation(order_id, lines)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente (odoo_id de res.partner)"
                },
                "states": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Estados a filtrar. Default: ['draft','sent']. Para ventas confirmadas: ['sale','done']",
                    "default": ["draft", "sent"]
                }
            },
            "required": ["partner_id"]
        },
    },
    {
        "name": "render_quotation_pdf",
        "description": (
            "Descargar la cotizacion como PDF oficial de Odoo (template completo: logo, "
            "encabezado, lineas, totales, impuestos, condiciones de pago) y guardarlo en "
            "disco para enviarlo como adjunto en el chat. Usa esto cuando el cliente pida "
            "'el PDF', 'envíame la cotizacion en PDF', 'mandame el documento', etc. "
            "Devuelve file_path (path absoluto al PDF, ej: /files/quotations/S00123.pdf). "
            "Para enviarlo al cliente, INCLUYE ese file_path EXACTO en tu mensaje en una "
            "linea aparte, sin code fence ni formato. El gateway de Telegram/WhatsApp "
            "detecta el path automaticamente y lo envia como adjunto nativo. NUNCA inventes paths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID de la cotizacion (sale.order)"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "send_quotation",
        "description": (
            "Enviar cotizacion por correo electronico al cliente. "
            "Usa despues de crear la cotizacion, si el cliente solicita recibirla por email. "
            "ACCION IRREVERSIBLE. "
            "FLUJO OBLIGATORIO: llama primero con confirmed=false para obtener un preview. "
            "Muéstraselo al usuario. Solo llama con confirmed=true tras recibir confirmación explícita."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID de la orden/cotizacion en Odoo"},
                "confirmed": {
                    "type": "boolean",
                    "description": "Pon true SOLO después de mostrar el preview al usuario y recibir confirmación explícita. Default: false (retorna preview solamente).",
                    "default": False,
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "confirm_quotation",
        "description": (
            "Confirmar una cotizacion y convertirla en orden de venta. ACCION IRREVERSIBLE. "
            "Solo usar cuando el cliente pida confirmar/aprobar la cotizacion. "
            "FLUJO OBLIGATORIO: llama primero con confirmed=false para obtener un preview. "
            "Muéstraselo al usuario. Solo llama con confirmed=true tras recibir confirmación explícita "
            "('sí', 'confirmo', 'dale')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID de la orden/cotizacion en Odoo"},
                "confirmed": {
                    "type": "boolean",
                    "description": "Pon true SOLO después de mostrar el preview al usuario y recibir confirmación explícita. Default: false (retorna preview solamente).",
                    "default": False,
                },
            },
            "required": ["order_id"],
        },
    },
    # ----- Sprint C: edition tools -------------------------------------
    {
        "name": "update_quotation_line",
        "description": (
            "Modificar UNA linea existente de la cotizacion: cambiar cantidad, precio unitario, "
            "descuento, descripcion o producto. Usa esto cuando el cliente diga 'cambia la cantidad', "
            "'pon X unidades', 'cambia el precio', 'aplica X% descuento a la linea'. "
            "Para 'agrega 2 mas' (delta) usa add_to_quotation/add_quotation_line; esta tool REEMPLAZA. "
            "Para cambiar el producto puedes pasar `product_id` (template_id) o `code` (default_code, "
            "ej. 'MON0026'); si pasas code el backend resuelve internamente. "
            "FLUJO OBLIGATORIO: confirmed=false primero (preview), confirmed=true tras confirmacion. "
            "IMPORTANTE: esta tool toma `line_id` (sale.order.line.id), NO order_id, NO el sufijo "
            "numérico del name. Llama get_quotation_state_summary(order_id=...) o "
            "get_quotation(order_id=...) primero para obtener el `line_id` real."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "line_id": {
                    "type": "integer",
                    "description": (
                        "ID INTERNO de sale.order.line (entero positivo). Se obtiene de "
                        "get_quotation/get_quotation_state_summary — NUNCA inventes este ID "
                        "ni uses el order_id o el sufijo del name (VENTA122173 → 122173) "
                        "en su lugar."
                    ),
                },
                "quantity": {"type": "number", "description": "Nueva cantidad TOTAL (no delta). Si quieres eliminar la linea, usa remove_quotation_line."},
                "price_unit": {"type": "number", "description": "Nuevo precio unitario (sobreescribe el del producto)."},
                "discount": {"type": "number", "description": "Descuento porcentual de la linea (0-100). Limitado por partner_max_sale_discount."},
                "name": {"type": "string", "description": "Nueva descripcion de la linea."},
                "product_id": {"type": "integer", "description": "Cambiar el producto (template_id). Operacion intrusiva. Alternativa a `code`."},
                "code": {"type": "string", "description": "Código visible del producto (default_code en Odoo, ej. 'MON0026'). Alternativa a product_id. Si pasas code y no product_id, el backend resuelve internamente."},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["line_id"],
        },
    },
    {
        "name": "remove_quotation_line",
        "description": (
            "Eliminar una linea de la cotizacion. En estado draft/sent hace unlink real; en sale "
            "cae automaticamente a qty=0 (porque l10n_ec_sri bloquea unlink si la orden tiene "
            "factura). Para 'cambiar a 0 unidades' que conserve la linea, usa update_quotation_line "
            "con quantity=0. FLUJO OBLIGATORIO: confirmed=false (preview), confirmed=true (ejecuta). "
            "IMPORTANTE: esta tool toma `line_id` (sale.order.line.id), NO order_id, NO el sufijo "
            "numérico del name. Llama get_quotation_state_summary(order_id=...) primero para "
            "obtener el `line_id` real."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "line_id": {
                    "type": "integer",
                    "description": (
                        "ID INTERNO de la sale.order.line a eliminar. Se obtiene de "
                        "get_quotation/get_quotation_state_summary — NUNCA inventes este ID "
                        "ni uses el order_id o el sufijo del name (VENTA122173 → 122173) "
                        "en su lugar."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "unlink", "qty_zero"],
                    "default": "auto",
                    "description": "auto: unlink en draft/sent, qty=0 en sale. unlink: forzar unlink (puede fallar). qty_zero: solo setear qty=0 sin borrar.",
                },
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["line_id"],
        },
    },
    {
        "name": "change_quotation_customer",
        "description": (
            "Reasignar el cliente (partner_id) de una cotizacion en borrador. Propaga "
            "automaticamente pricelist_id, payment_term_id y direcciones del nuevo partner. "
            "Solo en estados draft/sent (NO en sale/done — ahi rompe la contabilidad). "
            "FLUJO OBLIGATORIO: confirmed=false primero, confirmed=true despues."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "partner_id": {"type": "integer", "description": "ID del nuevo cliente"},
                "propagate_pricelist": {"type": "boolean", "default": True, "description": "Aplicar la tarifa default del nuevo partner."},
                "propagate_payment_term": {"type": "boolean", "default": True},
                "propagate_addresses": {"type": "boolean", "default": True, "description": "Recalcular partner_invoice_id y partner_shipping_id."},
                "reprice_lines": {"type": "boolean", "default": False, "description": "Recalcular price_unit de cada linea con la nueva tarifa."},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["order_id", "partner_id"],
        },
    },
    {
        "name": "apply_global_discount",
        "description": (
            "Aplicar un descuento a TODA la cotizacion (no a una linea individual). El descuento "
            "se propaga a cada linea via sale.order.calculate_discount(). Tipos: 'percent' (porcentaje "
            "parejo), 'amount' (monto fijo distribuido), 'cost' (margen sobre costo). Limitado por "
            "partner_max_sale_discount. Estados validos: draft/sent/waiting_approval/approved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "discount_type": {"type": "string", "enum": ["percent", "amount", "cost"]},
                "discount_rate": {"type": "number", "description": "Para percent: 0-100. Para amount: USD a descontar. Para cost: % de margen sobre costo."},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["order_id", "discount_type", "discount_rate"],
        },
    },
    {
        "name": "set_quotation_header",
        "description": (
            "Actualizar campos de cabecera de la cotizacion: fecha, validez, vendedor, tarifa, "
            "termino de pago, nota, referencia del cliente. NO toca lineas; cambiar pricelist aqui "
            "no recalcula precios existentes (usa change_quotation_customer con reprice_lines=true)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "date_order": {"type": "string", "description": "Formato 'YYYY-MM-DD HH:MM:SS'"},
                "validity_date": {"type": "string", "description": "Formato 'YYYY-MM-DD'"},
                "payment_term_id": {"type": "integer"},
                "pricelist_id": {"type": "integer"},
                "user_id": {"type": "integer", "description": "ID del vendedor"},
                "note": {"type": "string"},
                "client_order_ref": {"type": "string", "description": "Referencia del cliente"},
                "invoice_date": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "add_quotation_line",
        "description": (
            "Agregar UNA sola linea a la cotizacion. Mas simple que add_to_quotation cuando es 1 "
            "producto — no hace merge automatico con lineas existentes. Si quieres SUMAR a una linea "
            "existente, usa update_quotation_line con la nueva qty TOTAL. "
            "Acepta `product_id` (template_id) o `code` (default_code, ej. 'MON0026'). Pasa code "
            "para evitar inventar template_ids; si pasas ambos, product_id gana."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "product_id": {"type": "integer", "description": "template_id del producto. Alternativa a `code`."},
                "code": {"type": "string", "description": "Código visible del producto (default_code en Odoo, ej. 'MON0026'). Alternativa a product_id. Si pasas code y no product_id, el backend resuelve internamente."},
                "quantity": {"type": "number", "default": 1},
                "price_unit": {"type": "number", "description": "Override del precio default"},
                "discount": {"type": "number", "description": "Descuento porcentual de la linea"},
                "name": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            # product_id OR code — validado en el backend.
            "required": ["order_id"],
        },
    },
    {
        "name": "recalculate_quotation",
        "description": (
            "Forzar el recalculo de totales (action_recalculate). Util cuando se sospecha que el "
            "amount_total quedo desincronizado tras cambios masivos."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "get_quotation_state_summary",
        "description": (
            "Devolver un resumen completo del estado de la cotizacion para razonar antes de "
            "modificarla: state, totales, lineas con line_id + qty + qty_invoiced + qty_delivered + "
            "product_updatable, facturas vinculadas, pickings. Usalo SIEMPRE antes de "
            "update_quotation_line / remove_quotation_line — necesitas los line_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "transition_quotation",
        "description": (
            "Disparar una transicion de estado en la cotizacion. Acciones validas: confirm, cancel, "
            "draft, approve, reject, done, unlock, generar_despacho, generar_factura, "
            "procesar_venta, aprobar. ACCIONES POTENCIALMENTE IRREVERSIBLES — confirma con el "
            "usuario antes. "
            "Si recibes 'VENTAxxxxxx' del cliente o de otra tool, NUNCA extraigas los dígitos "
            "como order_id — usa find_quotation_by_name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": (
                        "ID INTERNO de sale.order (ej. 113604). REGLA CRÍTICA: el `name` "
                        "humano (VENTA122173) tiene un sufijo numérico (122173) que NO es el "
                        "order_id. NUNCA pases el sufijo del name como order_id. Si solo "
                        "conoces el name (formato 'VENTA' + 6 dígitos), llama "
                        "find_quotation_by_name(name='VENTA122173') primero para obtener el "
                        "order_id real."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["confirm", "cancel", "draft", "approve", "reject",
                             "done", "unlock", "generar_despacho",
                             "generar_factura", "procesar_venta", "aprobar"],
                },
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["order_id", "action"],
        },
    },
    {
        "name": "sign_quotation",
        "description": (
            "Firmar digitalmente una cotizacion (sale.order) escribiendo la firma "
            "(PNG base64), el nombre del firmante y la fecha. Replica /my/orders/<id>/accept "
            "del portal Odoo. Solo aplica a cotizaciones en estado 'draft' o 'sent' que no "
            "tengan firma previa. Por defecto (auto_confirm=true) tambien llama a "
            "action_confirm() para mover la orden a estado 'sale'. Tool MCP odoo_sign_quotation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID numerico de la sale.order a firmar.",
                },
                "signature": {
                    "type": "string",
                    "description": (
                        "PNG de la firma codificado en base64 SIN el prefijo "
                        "'data:image/png;base64,' — solo el base64 puro."
                    ),
                },
                "signed_by_name": {
                    "type": "string",
                    "description": "Nombre completo del firmante (minimo 3 caracteres).",
                },
                "auto_confirm": {
                    "type": "boolean",
                    "description": (
                        "Si true (default), confirma la cotizacion despues de firmarla "
                        "(action_confirm -> estado 'sale')."
                    ),
                    "default": True,
                },
            },
            "required": ["order_id", "signature", "signed_by_name"],
        },
    },
    {
        "name": "create_payphone_link",
        "description": (
            "Genera un link de pago PayPhone para una cotizacion (sale.order) "
            "ya existente. El link permite al cliente pagar con tarjeta de "
            "credito o debito ecuatoriana. Usa esta tool cuando el cliente "
            "pida 'link de pago', 'pagar con tarjeta', 'pago en linea' o "
            "similar. Solo aplica a ordenes en estado draft, sent, approved "
            "o sale. La tool devuelve link_url, client_tx_id, monto y la "
            "fecha de expiracion (48h)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID numerico de la sale.order en Odoo.",
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "check_payphone_status",
        "description": (
            "Consulta el estado de un link PayPhone por su client_tx_id. "
            "Usala cuando el cliente pregunte '¿ya pague?', '¿se acredito?' "
            "o '¿en que esta mi pago?'. Si refresh=true, fuerza una "
            "consulta a la API de PayPhone para actualizar el estado antes "
            "de leerlo (recomendado cuando el cliente acaba de pagar)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "client_tx_id": {
                    "type": "string",
                    "description": "Referencia PayPhone devuelta por create_payphone_link.",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "true = poll a PayPhone antes de leer; false = lee solo el estado cacheado.",
                    "default": False,
                },
            },
            "required": ["client_tx_id"],
        },
    },
    {
        "name": "niko_send_sign_request",
        "description": (
            "Envia al cliente del chat ACTUAL un mini-app para firmar la "
            "cotizacion. Usala cuando el cliente diga 'firmar', 'firma', 'sign', "
            "'enviar para firmar' sobre una cotizacion (estado draft o sent). "
            "Solo necesitas pasar `order_id` — el backend resuelve el canal y "
            "destinatario automaticamente desde el contexto del chat. NO "
            "preguntes al usuario por su channel_user_id, NO uses search_memories "
            "para buscarlo: solo llama esta tool con el order_id y listo. El "
            "cliente firma → Odoo confirma la orden automaticamente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID numerico de la sale.order a firmar.",
                },
                "message_prefix": {
                    "type": "string",
                    "description": (
                        "(Opcional) Linea introductoria que se prepende al "
                        "cuerpo del mensaje. Ej: 'Aqui esta tu cotizacion del "
                        "kit gamer:'."
                    ),
                    "default": "",
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "sri_import",
        "description": "Importar factura de compra del SRI usando la clave de acceso de 49 digitos. Usa esto cuando alguien envie un numero de 49 digitos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "access_key": {"type": "string", "description": "Clave de acceso SRI (49 digitos)"},
            },
            "required": ["access_key"],
        },
    },
    {
        "name": "request_otp",
        "description": (
            "Enviar codigo de verificacion OTP al correo electronico del cliente. "
            "OBLIGATORIO antes de mostrar datos financieros (saldos, deudas, facturas). "
            "Primero identifica al cliente con identify_customer, luego llama request_otp. "
            "Solo necesitas pasar `partner_id` — el backend resuelve email desde Odoo "
            "(res.partner.email) y canal/destinatario desde el contexto del chat "
            "automaticamente. NO preguntes al cliente por su email, NO pidas channel ni "
            "channel_user_id. El cliente recibira un codigo de 6 digitos en su correo "
            "registrado. Pidele que te lo escriba en el chat y luego usa verify_otp."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "email": {"type": "string", "description": "(Opcional) Email del cliente. Si se omite, el backend lo lee desde res.partner.email."},
                "channel": {"type": "string", "description": "(Opcional) Canal — el backend lo resuelve desde el contexto del chat."},
                "channel_user_id": {"type": "string", "description": "(Opcional) ID del usuario en el canal — el backend lo resuelve."},
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "verify_otp",
        "description": (
            "Verificar el codigo OTP de 6 digitos que el cliente proporciona. "
            "Si es correcto, se desbloquea el acceso a datos financieros por 24 horas. "
            "Solo necesitas pasar `partner_id` y `code` — el backend resuelve el canal "
            "desde el contexto del chat automaticamente. "
            "Despues de verificar, puedes llamar check_balance para obtener los datos."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "channel": {"type": "string", "description": "(Opcional) Canal — el backend lo resuelve desde el contexto."},
                "code": {"type": "string", "description": "Codigo de 6 digitos proporcionado por el cliente"},
            },
            "required": ["partner_id", "code"],
        },
    },
    {
        "name": "check_balance",
        "description": "Consultar saldo de un cliente. REQUIERE verificacion OTP previa. Si el cliente no ha verificado su identidad, usa request_otp primero.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "channel": {"type": "string", "description": "Canal actual para verificar sesion OTP"},
            },
            "required": ["partner_id"],
        },
    },
    {
        # ZETA iter 80 — listar facturas detalladas del cliente (gap iter
        # 79: check_balance solo devolvia el agregado).
        "name": "get_customer_invoices",
        "description": (
            "Listar las facturas (account.move type=out_invoice / "
            "out_refund) emitidas a un cliente. Devuelve detalle por "
            "factura: numero, fecha emision, fecha vencimiento, dias de "
            "vencimiento, total, saldo pendiente, estado de pago, "
            "referencia a la venta origen, y URL del PDF (RIDE) del "
            "portal. Filtra por estado con state='paid' | 'not_paid' | "
            "'overdue' | 'all'. REQUIERE verificacion OTP previa. "
            "Usa esta tool cuando el cliente pregunte 'dame mis "
            "facturas', '¿que facturas vencidas tengo?', '¿estoy al "
            "dia?', '¿que me falta pagar?', '¿facturas del 2025?'. NO "
            "uses list_quotations para esto — las cotizaciones son "
            "proformas, NO facturas."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner.id)",
                },
                "state": {
                    "type": "string",
                    "description": (
                        "Filtro: 'all' (default), 'paid' (pagadas), "
                        "'not_paid' (pendientes), 'overdue' (vencidas: "
                        "no pagadas + fecha vencimiento pasada)."
                    ),
                    "enum": ["all", "paid", "not_paid", "overdue"],
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "description": "Numero de facturas a devolver (default 10, max 50).",
                    "default": 10,
                },
                "year": {
                    "type": "integer",
                    "description": "(Opcional) restringir al anio indicado por fecha de emision.",
                },
                "channel": {
                    "type": "string",
                    "description": "Canal actual para verificar sesion OTP (opcional, lo resuelve el backend desde X-Channel).",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        # ZETA iter 80 — detalle completo de UNA factura (lineas + tax).
        "name": "get_invoice_detail",
        "description": (
            "Obtener el detalle completo de una factura: cabecera, "
            "lineas (producto, cantidad, precio unitario, descuento, "
            "subtotal), desglose de impuestos (IVA + retenciones SRI), "
            "y URL del PDF/RIDE del portal. REQUIERE verificacion OTP "
            "previa (es informacion financiera). "
            "Usala cuando el cliente pida el detalle de una factura "
            "especifica (ej: '¿que esta incluido en la FACV/2025/4897?', "
            "'mandame el detalle de la factura X', 'mandame el PDF del "
            "RIDE'). Para listar varias facturas usa "
            "get_customer_invoices."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "integer",
                    "description": "ID numerico de la factura (account.move.id).",
                },
                "include_lines": {
                    "type": "boolean",
                    "description": "Incluir las lineas de producto (default true).",
                    "default": True,
                },
                "include_taxes": {
                    "type": "boolean",
                    "description": "Incluir desglose de impuestos y retenciones (default true).",
                    "default": True,
                },
                "channel": {
                    "type": "string",
                    "description": "(Opcional) Canal actual para validar OTP. El backend lo resuelve desde X-Channel.",
                },
            },
            "required": ["invoice_id"],
        },
    },
    {
        # ZETA iter 80 — pagos hechos por el cliente.
        "name": "get_customer_payments",
        "description": (
            "Listar los pagos recibidos del cliente (account.payment "
            "inbound posted). Devuelve cada pago con numero, fecha, "
            "monto, diario contable (banco / efectivo), referencia y "
            "las facturas a las que se aplico. REQUIERE verificacion "
            "OTP previa. "
            "Usala cuando el cliente pregunte '¿que pagos he hecho?', "
            "'¿cuanto he pagado este mes?', 'mandame mis ultimos pagos', "
            "'¿en que banco pague tal factura?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner.id)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Numero de pagos a devolver (default 10, max 50).",
                    "default": 10,
                },
                "year": {
                    "type": "integer",
                    "description": "(Opcional) restringir al anio indicado por fecha de pago.",
                },
                "channel": {
                    "type": "string",
                    "description": "(Opcional) Canal para validar OTP. Default desde X-Channel.",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        # ZETA iter 80 — resumen agregado del estado de cuenta.
        "name": "get_customer_statement",
        "description": (
            "Generar un estado de cuenta resumido del cliente para los "
            "ultimos N dias: total facturado, total pagado, saldo "
            "pendiente actual, monto vencido, conteo de facturas en el "
            "periodo, conteo de facturas vencidas, dias promedio de "
            "pago, y un listado de los ultimos 10 movimientos "
            "(facturas + pagos) ordenados por fecha. REQUIERE "
            "verificacion OTP previa. "
            "Usala cuando el cliente pida '¿como estoy en cuenta?', "
            "'mandame mi estado de cuenta', 'resumen financiero', "
            "'¿cuanto debo en total?'. Para detalle factura por factura "
            "usa get_customer_invoices."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner.id)",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Dias hacia atras a considerar (default 90, max 730).",
                    "default": 90,
                },
                "channel": {
                    "type": "string",
                    "description": "(Opcional) Canal para validar OTP. Default desde X-Channel.",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        # ETA iter 81 — PDF oficial del estado de cuenta (mismo reporte
        # que el cron mensual: ``tecno_l10n_ec_payment.report_account_balance``).
        # Owner feedback (WhatsApp 2026-05-23): "obtener el email desde
        # Odoo y pasarlo a PDF asi la informacion seria la correcta y
        # oficial de Odoo". El bot debe entregar el documento descargable
        # que el cliente ya conoce, no una version reconstruida en niko.
        "name": "get_customer_statement_pdf",
        "description": (
            "Generar el PDF OFICIAL del estado de cuenta del cliente "
            "(mismo documento que recibe por correo cada 15 dias del "
            "cron de Odoo). REQUIERE verificacion OTP previa. Usa esta "
            "tool cuando el cliente pide el documento descargable: "
            "'mandame mi estado de cuenta en PDF', 'envíame el archivo', "
            "'necesito el documento oficial', 'el mismo que me mandan "
            "por correo'. Para preguntas conversacionales como '¿cuánto "
            "debo?' usa get_customer_statement (sin PDF). El resultado "
            "incluye un ``pdf_url`` publico que el cliente puede abrir "
            "directamente — incluyelo en tu respuesta verbatim."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner.id)",
                },
                "days_back": {
                    "type": "integer",
                    "description": (
                        "Periodo en dias hacia atras (informativo en el "
                        "envelope; el reporte Odoo no acepta filtro de "
                        "dias directo, devuelve todo el saldo abierto). "
                        "Default 90."
                    ),
                    "default": 90,
                },
                "channel": {
                    "type": "string",
                    "description": "(Opcional) Canal para validar OTP. Default desde X-Channel.",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        # ETA iter 81 — RIDE oficial de la factura (Tecnosmart Odoo 13
        # con l10n_ec_sri_ece). Auto-detecta NC vs factura via type.
        "name": "get_invoice_pdf",
        "description": (
            "Generar el RIDE OFICIAL en PDF de una factura electronica "
            "(el mismo documento autorizado por el SRI). REQUIERE "
            "verificacion OTP previa. Si la factura es nota de credito "
            "(type=out_refund), automaticamente usa el reporte de NC. "
            "Usala cuando el cliente pide 'mandame el RIDE', 'envia el "
            "PDF de la factura', 'el archivo oficial de la factura', "
            "'el comprobante autorizado por el SRI'. "
            "Acepta invoice_id (entero) O invoice_name (string tipo "
            "'FACV/2025/4897'): si pasas el name, el backend resuelve "
            "el id por busqueda. Si listaste facturas previamente con "
            "get_customer_invoices, prefiere usar el invoice_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "integer",
                    "description": "ID numerico de la factura (account.move.id). Si solo tienes el name, usa invoice_name.",
                },
                "invoice_name": {
                    "type": "string",
                    "description": "Nombre/numero de la factura (ej 'FACV/2025/4897'). Alternativa a invoice_id — el backend resuelve.",
                },
                "channel": {
                    "type": "string",
                    "description": "(Opcional) Canal para validar OTP. Default desde X-Channel.",
                },
            },
        },
    },
    {
        # ETA iter 81 — RIDE de nota de credito. Refuses cuando el
        # account.move no es out_refund.
        "name": "get_credit_note_pdf",
        "description": (
            "Generar el RIDE OFICIAL en PDF de una nota de credito "
            "electronica (account.move type='out_refund'). REQUIERE "
            "verificacion OTP previa. Si el id pasado no es una nota "
            "de credito, devuelve error — para facturas normales usa "
            "get_invoice_pdf. Usala cuando el cliente pida 'el PDF de "
            "la nota de credito X', 'mandame la NC', 'el RIDE de la "
            "devolucion'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refund_id": {
                    "type": "integer",
                    "description": (
                        "ID numerico de la nota de credito "
                        "(account.move.id con type='out_refund')."
                    ),
                },
                "channel": {
                    "type": "string",
                    "description": "(Opcional) Canal para validar OTP. Default desde X-Channel.",
                },
            },
            "required": ["refund_id"],
        },
    },
    {
        # ETA iter 81 — Comprobante de retencion (l10n_ec_sri_ece
        # report_retencion_electronica). Para retenciones que clientes
        # B2B le hicieron a Tecnosmart.
        "name": "get_retention_pdf",
        "description": (
            "Generar el RIDE OFICIAL en PDF de un comprobante de "
            "retencion electronica (cuando un cliente B2B retuvo "
            "impuestos a Tecnosmart). REQUIERE verificacion OTP previa. "
            "El retention_id es el id del account.move tipo retencion "
            "(NO el id de la factura asociada). Usala cuando el cliente "
            "pida 'mandame el comprobante de la retencion que te hice', "
            "'el RIDE de la retencion N'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "retention_id": {
                    "type": "integer",
                    "description": "ID numerico de la retencion (account.move.id tipo retencion).",
                },
                "channel": {
                    "type": "string",
                    "description": "(Opcional) Canal para validar OTP. Default desde X-Channel.",
                },
            },
            "required": ["retention_id"],
        },
    },
    {
        "name": "lookup_sri",
        "description": (
            "Consultar datos de una persona o empresa en el SRI (Servicio de Rentas Internas) "
            "por cedula (10 digitos) o RUC (13 digitos). Devuelve nombre completo, estado "
            "tributario, actividad economica, direccion de establecimientos. "
            "Usa esto cuando identify_customer no encuentre al cliente en Odoo — "
            "el SRI puede tener sus datos para crear el perfil."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cedula_ruc": {"type": "string", "description": "Cedula (10 digitos) o RUC (13 digitos)"},
            },
            "required": ["cedula_ruc"],
        },
    },
    {
        "name": "create_partner",
        "description": (
            "Crear un nuevo cliente/contacto en Odoo. "
            "El tool consulta el SRI automaticamente para obtener nombre, direccion y datos fiscales — "
            "NUNCA le pidas nombre ni direccion al usuario, el SRI los provee. "
            "Solo necesitas recopilar del usuario: correo electronico (obligatorio) y telefono (opcional). "
            "Flujo OBLIGATORIO: "
            "1) Llama con solo vat (sin confirmed ni name/street) → el tool consulta SRI y devuelve preview. "
            "2) Muestra el preview al cliente; pide SOLO email y telefono. "
            "3) Si confirma, llama de nuevo con confirmed=true + email + phone → crea el cliente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vat": {"type": "string", "description": "Cedula (10 digitos) o RUC (13 digitos)"},
                "email": {"type": "string", "description": "Correo electronico (pedirlo al usuario)"},
                "phone": {"type": "string", "description": "Telefono (pedirlo al usuario, opcional)"},
                "mobile": {"type": "string", "description": "Celular (opcional)"},
                "name": {"type": "string", "description": "Nombre completo — omitir, el SRI lo provee automaticamente"},
                "street": {"type": "string", "description": "Direccion — omitir, el SRI la provee automaticamente"},
                "city": {"type": "string", "description": "Ciudad — omitir, el SRI la provee automaticamente"},
                "confirmed": {"type": "boolean", "description": "true para crear, false/omitido para preview"},
            },
            "required": ["vat"],
        },
    },
    {
        "name": "update_partner",
        "description": (
            "Actualizar datos de un cliente existente en Odoo. "
            "REQUIERE confirmed=true para ejecutar. Sin confirmed, solo muestra preview. "
            "Flujo: 1) Llama SIN confirmed → devuelve preview con datos actuales vs nuevos. "
            "2) Muestra al cliente y pide confirmacion. "
            "3) Si confirma, llama DE NUEVO con confirmed=true → ejecuta el cambio."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "name": {"type": "string", "description": "Nombre completo"},
                "email": {"type": "string", "description": "Correo electronico"},
                "phone": {"type": "string", "description": "Telefono"},
                "mobile": {"type": "string", "description": "Celular"},
                "street": {"type": "string", "description": "Direccion"},
                "city": {"type": "string", "description": "Ciudad"},
                "confirmed": {"type": "boolean", "description": "true para ejecutar, false/omitido para preview"},
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "get_company_settings",
        "description": (
            "Settings INTERNOS del flow consumidor final SOLAMENTE — NO es "
            "informacion publica de la empresa. Devuelve: partner_id del "
            "consumidor final generico (RUC 9999999999999), si se requieren "
            "datos del consumidor final (pedir_end_customer_data), monto "
            "maximo SRI para facturas de consumidor final "
            "(sri_invoice_limit). Llamar ANTES de create_quotation cuando el "
            "cliente NO se identifica con cedula/RUC. "
            "NO usar cuando el cliente pregunta direccion, telefono, "
            "horarios, email, RUC fiscal, formas de pago, IVA o "
            "actividad de la empresa — para esos datos usa get_business_info."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ----- B2B priority tools ------------------------------------------
    {
        "name": "get_customer_credit_status",
        "description": (
            "Consultar el estado financiero de un cliente: saldo pendiente, "
            "facturas vencidas y credito disponible. "
            "Devuelve credit_used (deuda total), overdue_amount (vencida), "
            "credit_limit y credit_available (si el modulo de credito esta activo), "
            "y la lista completa de facturas pendientes con su estado de pago. "
            "Usar cuando el vendedor pregunte por el estado de cuenta, cuanto debe "
            "el cliente, si tiene facturas vencidas, o si puede comprar mas."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner)",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "get_order_delivery_status",
        "description": (
            "Consultar el estado de entrega de un pedido confirmado (sale.order): "
            "que se ha enviado, que falta por enviar, y el estado de cada "
            "transferencia (picking) vinculada. "
            "Acepta order_id (entero) O order_name (ej: 'VENTA122196'). "
            "Usar cuando el cliente o vendedor pregunte por el despacho, "
            "'cuando llega mi pedido', 'ya salio el pedido', 'estado de entrega'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID numerico del sale.order",
                },
                "order_name": {
                    "type": "string",
                    "description": "Nombre del pedido (ej: 'VENTA122196'). Usar si no se tiene order_id.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_my_sales_summary",
        "description": (
            "Resumen de ventas del vendedor logueado para el periodo indicado: "
            "total vendido, numero de pedidos, clientes unicos y listado de ordenes. "
            "Periodos: 'month' (mes en curso, default), 'week' (semana en curso), "
            "'today' (solo hoy). "
            "Usar cuando el vendedor pregunte por sus ventas, 'cuanto llevo este mes', "
            "'mis ventas de hoy', 'resumen de mi semana', 'cuantos pedidos tengo'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Periodo: 'month' (default), 'week' o 'today'",
                    "enum": ["month", "week", "today"],
                    "default": "month",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_pricelist_price",
        "description": (
            "Obtener el precio efectivo de venta de un producto para un cliente "
            "especifico segun la lista de precios (pricelist) configurada en su "
            "ficha. Devuelve list_price (precio base del producto), "
            "pricelist_price (precio resuelto por la tarifa del cliente), el "
            "nombre de la tarifa aplicada y el descuento porcentual implicito. "
            "Si el cliente no tiene tarifa configurada, devuelve el list_price. "
            "Usar cuando el vendedor pregunte 'a que precio se lo vendo a este "
            "cliente', 'que precio tiene MEGA PRIMAVERA para este producto', o "
            "antes de armar una cotizacion para verificar el precio efectivo. "
            "El template_id es el devuelto por search_products."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner)",
                },
                "template_id": {
                    "type": "integer",
                    "description": (
                        "ID del producto (product.template), tal como lo "
                        "devuelve search_products"
                    ),
                },
                "quantity": {
                    "type": "number",
                    "description": "Cantidad a cotizar (default 1)",
                    "default": 1,
                },
            },
            "required": ["partner_id", "template_id"],
        },
    },
    {
        "name": "get_quotation_margin",
        "description": (
            "Calcular el margen de ganancia de una cotizacion o pedido (sale.order). "
            "Usa los campos del modulo nativo `sale_margin` de Odoo 13: "
            "purchase_price (costo unitario), margin (ganancia por linea) y el "
            "margin del header. Devuelve total_margin, margin_pct y un detalle "
            "por linea con qty, precio, costo, subtotal, margen y descuento. "
            "Acepta order_id (entero) O order_name (ej: 'VENTA122196'). "
            "Usar cuando el vendedor pregunte 'cuanto gano con esta cotizacion', "
            "'que margen tiene', 'puedo bajar el precio sin perder', o antes de "
            "aplicar un descuento global."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID numerico del sale.order",
                },
                "order_name": {
                    "type": "string",
                    "description": (
                        "Nombre de la cotizacion/pedido (ej: 'VENTA122196'). "
                        "Usar si no se tiene order_id."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_customer_purchase_history",
        "description": (
            "Historial de compras de un cliente. Distingue dos categorias:\n"
            "  - top_products (FACTURADOS): productos que el cliente sí compró "
            "    realmente (qty_invoiced > 0). Estos son los que aparecieron en "
            "    facturas posted no canceladas (Odoo ya neta refunds). Usa este "
            "    campo cuando digas al cliente 'compraste X'.\n"
            "  - top_products_quoted_only: productos que estan en cotizaciones "
            "    pero NO se han facturado. Cada item incluye `quotations` con "
            "    los nombres VENTAxxx donde aparece. NUNCA digas 'compraste' "
            "    sobre estos — di 'cotizaste' o 'tienes pendiente'.\n"
            "  - total_amount (cotizado en sale.order) vs total_invoiced "
            "    (facturado real) — siempre prefiere mostrar total_invoiced "
            "    cuando hables de 'compraste'.\n"
            "Considera solo ordenes confirmadas (state in sale|done) en el "
            "anio indicado (default: actual). "
            "Usar cuando el vendedor pregunte 'que compra este cliente', "
            "'cuanto ha comprado este anio', 'cual es su ticket promedio', "
            "'que productos se lleva', o para preparar una visita comercial."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner)",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Numero de ordenes recientes a devolver en "
                        "recent_orders (default 10, max 100)"
                    ),
                    "default": 10,
                },
                "year": {
                    "type": "integer",
                    "description": "Anio del periodo a analizar (default: anio actual)",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "get_stock_by_warehouse",
        "description": (
            "Stock disponible de un producto agrupado por bodega + entradas "
            "esperadas (purchase orders pendientes de recepcion). Devuelve "
            "totales (available/reserved/free), desglose por bodega y lista "
            "de PO con qty_received < product_qty. "
            "Acepta template_id (preferido) o product_code (default_code, ej. "
            "'LAP0176'). Usar cuando el vendedor pregunte 'donde esta el "
            "stock', 'cuanto tengo en cada bodega', 'cuando llega mas', "
            "'esperan reposicion'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {
                    "type": "integer",
                    "description": "ID de product.template (preferido).",
                },
                "product_code": {
                    "type": "string",
                    "description": "default_code del producto (ej. 'LAP0176'). Alternativa si no tienes template_id.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_pending_quotations",
        "description": (
            "Cotizaciones enviadas (state=sent) sin respuesta del cliente, "
            "del vendedor logueado. Categoriza por validez: expirada / "
            "por_vencer (<3 dias) / vigente. Usar cuando el vendedor pida "
            "'cotizaciones pendientes', 'que quedo sin contestar', 'mis "
            "proformas sin respuesta', 'que tengo por hacer seguimiento', "
            "'cotizaciones que se vencen'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days_old": {
                    "type": "integer",
                    "description": "Mostrar cotizaciones enviadas hace mas de N dias (default 7).",
                    "default": 7,
                },
                "include_expired": {
                    "type": "boolean",
                    "description": "Incluir cotizaciones cuya validity_date ya paso (default true).",
                    "default": True,
                },
            },
            "required": [],
        },
    },
    {
        "name": "duplicate_quotation",
        "description": (
            "Duplicar una cotizacion existente como nuevo borrador (sale.order "
            "en estado draft). Util cuando el cliente quiere repetir un pedido "
            "similar. Acepta order_id (preferido) o order_name (ej. "
            "'VENTA122196'). La nueva cotizacion puede modificarse antes de "
            "enviarse. Usar cuando el vendedor pida 'duplicar', 'clonar', "
            "'repetir', 'copiar la cotizacion', 'haz otra igual'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID de la cotizacion a duplicar (preferido).",
                },
                "order_name": {
                    "type": "string",
                    "description": "Nombre humano (ej. 'VENTA122196'). Se resuelve a order_id si no tienes el ID.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_my_crm_opportunities",
        "description": (
            "Oportunidades CRM activas del vendedor logueado — pipeline de "
            "ventas. Devuelve lista de crm.lead con type='opportunity', "
            "ordenadas por fecha limite ascendente. Incluye revenue total "
            "(planned) y revenue ponderado por probabilidad (forecast). "
            "Usar cuando el vendedor pregunte por su 'pipeline', "
            "'oportunidades', 'que estoy trabajando', 'leads abiertos', "
            "'forecast', 'pronostico de ventas'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Filtrar por nombre de etapa (ej. 'Propuesta', 'Negociacion'). Match parcial (ilike).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximo de oportunidades a retornar (default 10).",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    # -----------------------------------------------------------------------
    # Appointment / booking tools (salon — Odoo 19 ``appointment`` module).
    # -----------------------------------------------------------------------
    {
        "name": "list_services",
        "description": (
            "Listar los servicios que ofrece el salón (appointment.type) "
            "con su nombre, duración y precio en USD. Úsala cuando el "
            "cliente pregunte '¿qué servicios tienen?', '¿cuánto cuesta "
            "una manicura?', '¿qué hacen?', 'precios', o cuando necesites "
            "resolver el nombre exacto de un servicio antes de consultar "
            "disponibilidad o agendar. Acepta un filtro opcional por "
            "nombre."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "(Opcional) Filtro por nombre del servicio "
                        "(ilike, sin distinguir mayúsculas). Ej.: "
                        "'manicura', 'pedicura', 'acrílicas'."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_availability",
        "description": (
            "Calcular los HORARIOS LIBRES para un servicio del salón en un "
            "rango de fechas. Lee el horario laboral del servicio, "
            "respeta la antelación mínima, descarta los horarios que ya "
            "tienen una cita y devuelve los espacios libres agrupados por "
            "día en hora local. Úsala SIEMPRE antes de agendar, cuando el "
            "cliente pregunte '¿cuándo tienen espacio?', '¿qué horarios "
            "hay el viernes?', '¿está libre mañana a las 3?'. El nombre "
            "del servicio se resuelve de forma aproximada; si no estás "
            "seguro del nombre exacto, usa primero list_services."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": (
                        "Nombre del servicio a consultar (ej.: 'Manicura "
                        "semipermanente'). Se resuelve de forma aproximada."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": (
                        "(Opcional) Fecha inicial 'YYYY-MM-DD' (hora local "
                        "del salón). Por defecto hoy."
                    ),
                },
                "days_ahead": {
                    "type": "integer",
                    "description": (
                        "(Opcional) Cuántos días hacia adelante escanear "
                        "desde date_from (default 7, máx 30)."
                    ),
                    "default": 7,
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Agendar una cita en el calendario del salón. La cita queda "
            "POR CONFIRMAR hasta que el staff verifique el anticipo del "
            "50% (no reembolsable, por transferencia); copia al evento los "
            "recordatorios (email/SMS) configurados para ese servicio. "
            "Re-valida que el horario siga libre y dentro del horario de "
            "atención antes de crear; si está ocupado o fuera de horario, "
            "devuelve un error claro y debes usar get_availability para "
            "ofrecer otra hora. Úsala cuando el cliente confirme un horario "
            "específico (ej.: 'agéndame el viernes a las 10'). NO requiere "
            "cédula: si el cliente ya está identificado pasa su partner_id; "
            "si no, pasa customer_name + customer_phone (el celular es "
            "OBLIGATORIO cuando no hay partner_id) y se crea un contacto "
            "mínimo. Tras agendar, usa get_payment_info para enviarle los "
            "datos del anticipo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Nombre del servicio a agendar.",
                },
                "start_local": {
                    "type": "string",
                    "description": (
                        "Fecha y hora de inicio en hora local del salón, "
                        "formato 'YYYY-MM-DD HH:MM' (ej.: "
                        "'2026-06-12 10:00')."
                    ),
                },
                "partner_id": {
                    "type": "integer",
                    "description": (
                        "(Opcional) ID del cliente en Odoo (res.partner.id) "
                        "si ya está identificado. Si no lo pasas, usa "
                        "customer_name + customer_phone."
                    ),
                },
                "customer_name": {
                    "type": "string",
                    "description": (
                        "Nombre del cliente para crear un contacto mínimo "
                        "cuando no hay partner_id (sin cédula)."
                    ),
                },
                "customer_phone": {
                    "type": "string",
                    "description": (
                        "Celular del cliente. OBLIGATORIO cuando no hay "
                        "partner_id: se usa para reconocer a clientas que "
                        "regresan y, si no existe, crear el contacto."
                    ),
                },
            },
            "required": ["service", "start_local"],
        },
    },
    {
        "name": "list_my_appointments",
        "description": (
            "Listar las citas FUTURAS de un cliente (calendar.event con "
            "start >= ahora), ordenadas por fecha. Devuelve cada cita con "
            "servicio, fecha/hora local y un número de referencia "
            "(event_id) que sirve para cancelar. Úsala cuando el cliente "
            "pregunte '¿qué citas tengo?', '¿cuándo es mi próxima cita?', "
            "o antes de cancelar para que el cliente elija cuál."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "ID del cliente en Odoo (res.partner.id).",
                },
            },
            "required": ["partner_id"],
        },
    },
    {
        "name": "cancel_appointment",
        "description": (
            "Cancelar una cita del cliente. Verifica que la cita esté a "
            "nombre de ese cliente (partner_id entre los asistentes) antes "
            "de cancelar; si no le pertenece, la rechaza. Úsala cuando el "
            "cliente pida cancelar una cita y te dé el número de "
            "referencia (event_id). Si no lo conoces, usa primero "
            "list_my_appointments."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "integer",
                    "description": (
                        "ID de la cita a cancelar (calendar.event.id / "
                        "número de referencia mostrado al cliente)."
                    ),
                },
                "partner_id": {
                    "type": "integer",
                    "description": (
                        "ID del cliente en Odoo (res.partner.id), para "
                        "autorizar la cancelación."
                    ),
                },
            },
            "required": ["event_id", "partner_id"],
        },
    },
    {
        "name": "create_invoice",
        "description": (
            "Crear una FACTURA en BORRADOR para un cliente YA identificado "
            "(salón Afrodita, facturación electrónica Ecuador). Crea el "
            "documento en estado borrador con los servicios/productos que "
            "indique el cliente y deja un aviso para que el staff la revise "
            "y la autorice en el SRI. NO la emite ni la autoriza por su "
            "cuenta. Cada ítem se resuelve por nombre contra el catálogo; "
            "si un ítem no existe, devuelve un error claro y NO inventes "
            "productos. Úsala cuando el cliente pida que le factures un "
            "servicio o compra. NECESITAS el partner_id del cliente "
            "identificado y la lista de ítems."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": (
                        "ID del cliente en Odoo (res.partner.id). El "
                        "cliente debe estar identificado primero."
                    ),
                },
                "lines": {
                    "type": "array",
                    "description": (
                        "Lista de ítems a facturar. Cada uno es un objeto "
                        "con 'item' (nombre del servicio/producto), "
                        "'quantity' (cantidad, default 1) y opcionalmente "
                        "'price_unit' (precio unitario; si no lo pasas, "
                        "Odoo usa el precio de lista del producto)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "item": {
                                "type": "string",
                                "description": (
                                    "Nombre del servicio o producto a "
                                    "facturar (ej.: 'Manicura "
                                    "semipermanente'). Se resuelve de forma "
                                    "aproximada contra el catálogo."
                                ),
                            },
                            "quantity": {
                                "type": "number",
                                "description": "Cantidad (default 1).",
                                "default": 1,
                            },
                            "price_unit": {
                                "type": "number",
                                "description": (
                                    "(Opcional) Precio unitario. Si se "
                                    "omite, Odoo toma el precio de lista del "
                                    "producto."
                                ),
                            },
                        },
                        "required": ["item"],
                    },
                },
                "note": {
                    "type": "string",
                    "description": (
                        "(Opcional) Nota libre que se agrega al aviso para "
                        "el staff sobre la factura."
                    ),
                },
            },
            "required": ["partner_id", "lines"],
        },
    },
    {
        "name": "get_payment_info",
        "description": (
            "Devolver los datos bancarios del salón para que el cliente "
            "haga el anticipo del 50% (no reembolsable, por transferencia) "
            "que confirma su cita. Úsala justo después de agendar una cita "
            "(book_appointment) o cuando el cliente pregunte cómo o dónde "
            "pagar el anticipo. Devuelve también la imagen con los datos de "
            "pago para enviársela. No inventes números de cuenta: usa "
            "exactamente lo que entrega esta tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_location_info",
        "description": (
            "Devolver la ubicación del salón (dirección, horario de "
            "atención, teléfono e Instagram) y la imagen con el mapa/"
            "dirección para enviársela al cliente. Úsala cuando el cliente "
            "pregunte dónde están, cómo llegar, la dirección, el horario o "
            "los datos de contacto del salón. No inventes la dirección ni "
            "el horario: usa exactamente lo que entrega esta tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _parse_allowed_tools(request) -> list[str] | None:
    """Extract allowed tools list from X-Allowed-Tools header.

    Returns None if header is absent or empty (meaning all tools allowed).
    Returns a list of tool name strings if header is present and non-empty.
    """
    header = request.headers.get("x-allowed-tools", "").strip().strip('"')
    if not header:
        return None
    return [t.strip() for t in header.split(",") if t.strip()]


def _make_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


import asyncio
import uuid
from starlette.responses import StreamingResponse

# SSE session store: session_id -> asyncio.Queue
_sse_sessions: dict[str, asyncio.Queue] = {}


@router.get("/sse")
async def sse_endpoint(request: Request):
    """MCP SSE transport: client opens GET /sse, receives events, sends POST /messages."""
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue

    async def event_stream():
        # First event: tell client where to POST messages
        yield f"event: endpoint\ndata: /messages?sessionId={session_id}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"event: message\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/messages")
async def sse_messages_endpoint(request: Request):
    """MCP SSE transport: receives JSON-RPC requests, sends responses via SSE."""
    session_id = request.query_params.get("sessionId", "")
    queue = _sse_sessions.get(session_id)
    if not queue:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    body = await request.json()
    response = await _handle_mcp_request(request, body)
    if response is not None:
        await queue.put(response)
    return JSONResponse({"ok": True})


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    """StreamableHTTP MCP endpoint. Handles JSON-RPC over HTTP POST."""
    body = await request.json()
    return JSONResponse(await _handle_mcp_request(request, body) or {"jsonrpc": "2.0"})


async def _handle_mcp_request(request: Request, body: dict) -> dict | None:
    """Process a single MCP JSON-RPC request."""
    req_id = body.get("id")
    method = body.get("method", "")

    if method == "initialize":
        return _make_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mcp-server-odoo", "version": "1.0.0"},
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
        tool_name = body["params"]["name"]
        args = body["params"].get("arguments", {})

        # Enforce tool isolation: reject calls to tools not in the allowed list
        allowed = _parse_allowed_tools(request)
        if allowed is not None and tool_name not in allowed:
            logger.warning(
                f"Tool '{tool_name}' blocked for agent "
                f"{request.headers.get('x-agent-slug', '?')} "
                f"(allowed: {allowed})"
            )
            return _make_response(req_id, {
                "content": [{"type": "text", "text":
                    f"Herramienta '{tool_name}' no esta habilitada para este agente. "
                    f"Herramientas disponibles: {', '.join(allowed)}."
                }],
                "isError": True,
            })

        # Pre-execution: validate that ``args["order_id"]`` is a real
        # Odoo order_id. If the LLM passed the name ("VENTA122172") or
        # the digit suffix ("122172"), we REJECT the call with a hint
        # to use ``find_quotation_by_name`` first. We never silently
        # mutate using a guessed id (e.g. via ilike) because that could
        # touch the wrong sale.order on collisions.
        reject_envelope = await _resolve_order_id_alias(request, tool_name, args)
        if reject_envelope is not None:
            return _make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    reject_envelope, indent=2, ensure_ascii=False,
                )}],
                "isError": True,
            })

        # Pre-execution arg validation against the tool's inputSchema.
        # Without this, malformed LLM args (e.g. fields placed at the
        # top level instead of inside a required nested array) reach the
        # tool function which then fails with a generic message that
        # gives the LLM no signal on what to fix. We catch the missing/
        # mistyped fields here and return a structured error containing
        # the expected schema so the LLM can self-correct on the next
        # AIMessage.
        validation_error = _validate_args_against_schema(tool_name, args)
        if validation_error is not None:
            return _make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    validation_error, indent=2, ensure_ascii=False,
                )}],
                "isError": True,
            })

        try:
            text = await _execute_tool(request, tool_name, args)
            # Auto-attach _card to quotation tool results that returned
            # order_id without a _card envelope, so the orchestrator can
            # render an OrderCard (Editar/PDF/Confirmar buttons) on
            # Telegram/WhatsApp. Without this, mutation tools fall back
            # to the LLM rendering markdown tables that Telegram does NOT
            # display — the customer sees raw `|---|---|` pipes.
            if tool_name in _QUOTATION_TOOLS_NEEDING_CARD:
                try:
                    tc_for_card = await _get_tenant_config(request)
                    text = _maybe_attach_card(tc_for_card, tool_name, text)
                except Exception as exc_card:
                    logger.debug("card auto-attach failed: %s", exc_card)
            return _make_response(req_id, {
                "content": [{"type": "text", "text": text}],
            })
        except Exception as e:
            import traceback
            logger.error(f"Tool {tool_name} error: {e}\n{traceback.format_exc()}")
            friendly = _classify_error(e, tool_name)
            return _make_response(req_id, {
                "content": [{"type": "text", "text": friendly}],
                "isError": True,
            })

    if method == "ping":
        return _make_response(req_id, {})

    return _make_error(req_id, -32601, f"Unknown method: {method}")


# Channels that need plain-text rendering (no markdown tables).
_CHAT_CHANNELS = {"whatsapp", "telegram"}


def _attach_display_text(
    result: dict | None,
    channel: str,
    *,
    kind: str,
    **kwargs,
) -> dict | None:
    """Add ``display_text`` to a list-tool envelope when channel is chat.

    ``kind`` selects the formatter:
      * "quotations"           → format_quotations_list(orders=result.orders)
      * "purchase_history"     → format_purchase_history(history=result)
      * "pending_quotations"   → format_pending_quotations(quotations=result)
      * "quotation_detail"     → format_quotation_detail(quotation=result)

    No-op when ``result`` is falsy, when the call failed (success=False
    with no listable data), or when the channel is not chat. Never
    raises — formatter errors are swallowed and logged so a render glitch
    cannot break a successful tool call.
    """
    if not isinstance(result, dict):
        return result
    if (channel or "").lower() not in _CHAT_CHANNELS:
        return result
    try:
        from mcp_odoo.formatters.whatsapp import (
            format_pending_quotations,
            format_purchase_history,
            format_quotation_detail,
            format_quotations_list,
        )
        # ZETA iter 80 — invoice / payment / statement formatters.
        from mcp_odoo.formatters.whatsapp_invoices import (
            format_invoice_detail,
            format_invoices_list,
            format_payments_list,
            format_statement_summary,
        )
        if kind == "quotations":
            orders = result.get("orders") or []
            result["display_text"] = format_quotations_list(
                orders,
                partner_name=kwargs.get("partner_name"),
                state_filter=kwargs.get("state_filter"),
            )
        elif kind == "purchase_history":
            # Only attach when the call succeeded — error envelopes have
            # no recent_orders and the formatter would emit a misleading
            # "no compras" message that would override the real error.
            if result.get("success") is not False:
                result["display_text"] = format_purchase_history(result)
        elif kind == "pending_quotations":
            if result.get("success") is not False:
                result["display_text"] = format_pending_quotations(result)
        elif kind == "quotation_detail":
            if result.get("success") is not False:
                result["display_text"] = format_quotation_detail(result)
        elif kind == "invoices":
            if result.get("success") is not False:
                result["display_text"] = format_invoices_list(
                    result.get("invoices") or [],
                    total_amount=result.get("total_amount"),
                    total_residual=result.get("total_residual"),
                    state_filter=result.get("state_filter"),
                )
        elif kind == "invoice_detail":
            if result.get("success") is not False:
                result["display_text"] = format_invoice_detail(
                    result.get("invoice") or {},
                    lines=result.get("lines") or [],
                    taxes=result.get("taxes") or [],
                )
        elif kind == "payments":
            if result.get("success") is not False:
                result["display_text"] = format_payments_list(result)
        elif kind == "statement":
            if result.get("success") is not False:
                result["display_text"] = format_statement_summary(
                    result.get("summary") or {},
                    recent_movements=result.get("recent_movements") or [],
                    period=result.get("period") or {},
                )
        elif kind in (
            "services", "availability", "booking",
            "my_appointments", "cancellation", "payment_info",
            "location_info",
        ):
            # Appointment / booking formatters. booking + cancellation +
            # availability render even on success=False so the customer
            # sees the friendly error (e.g. "horario ocupado") verbatim.
            from mcp_odoo.formatters.whatsapp_appointments import (
                format_availability,
                format_booking_confirmation,
                format_cancellation,
                format_location_info,
                format_my_appointments,
                format_payment_info,
                format_services_list,
            )
            if kind == "services":
                if result.get("success") is not False:
                    result["display_text"] = format_services_list(result)
            elif kind == "availability":
                result["display_text"] = format_availability(result)
            elif kind == "booking":
                result["display_text"] = format_booking_confirmation(result)
            elif kind == "my_appointments":
                if result.get("success") is not False:
                    result["display_text"] = format_my_appointments(result)
            elif kind == "cancellation":
                result["display_text"] = format_cancellation(result)
            elif kind == "payment_info":
                if result.get("success") is not False:
                    result["display_text"] = format_payment_info(result)
            elif kind == "location_info":
                if result.get("success") is not False:
                    result["display_text"] = format_location_info(result)
        elif kind == "invoice_created":
            # Billing formatter. Renders even on success=False so the
            # customer sees the friendly error (e.g. "ítem no existe").
            from mcp_odoo.formatters.whatsapp_billing import (
                format_invoice_created,
            )
            result["display_text"] = format_invoice_created(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "display_text formatter failed kind=%s channel=%s: %s",
            kind, channel, exc,
        )
    return result


def _format_partner_profile_chat(
    partner: dict, activity: dict | None = None,
) -> str:
    """Iter 79b — render perfil 360° del cliente en formato WhatsApp.

    Combina los datos del res.partner con la actividad reciente
    (purchase_history + recent_quotations + recent_invoices) en un
    bloque legible que el LLM puede copiar tal cual al cliente.
    """
    lines: list[str] = []
    name = partner.get("display_name") or partner.get("name") or "Cliente"
    lines.append(f"👤 *Esto es lo que tengo de ti, {name}:*")
    lines.append("")
    lines.append("*Contacto:*")

    def _mask_email(email: str) -> str:
        if not email or "@" not in email:
            return email or "—"
        local, domain = email.split("@", 1)
        return f"{local[:3]}***@{domain}"

    def _mask_phone(phone: str) -> str:
        if not phone:
            return "—"
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) <= 4:
            return phone
        return f"{digits[:3]}***{digits[-2:]}"

    if partner.get("email"):
        lines.append(f"• Email: {_mask_email(partner['email'])}")
    if partner.get("phone") or partner.get("mobile"):
        ph = partner.get("phone") or partner.get("mobile")
        lines.append(f"• Teléfono: {_mask_phone(ph)}")
    if partner.get("vat"):
        lines.append(f"• RUC/Cédula: {partner['vat']}")
    addr_bits = [
        partner.get("street"),
        partner.get("city"),
        (partner.get("state_id") or {}).get("name") if isinstance(partner.get("state_id"), dict) else None,
    ]
    addr_bits = [b for b in addr_bits if b]
    if addr_bits:
        lines.append(f"• Dirección: {', '.join(addr_bits)}")
    if partner.get("lang"):
        lines.append(f"• Idioma: {partner['lang']}")

    if not activity:
        lines.append("")
        lines.append("_Aún no tengo historial de compras en memoria. Si necesitas info financiera (saldo / crédito) pídeme y te envío OTP._")
        return "\n".join(lines)

    # ── Actividad reciente ─────────────────────────────────────────
    # Iter89 owner-audit 2026-05-25: distinguir COMPRADO (facturado) vs
    # COTIZADO_NO_FACTURADO. Antes el bot decía "Top productos comprados"
    # cuando solo había sale.order confirmadas — mentira al cliente.
    ph_data = (activity or {}).get("purchase_history") or {}
    if ph_data.get("success") and ph_data.get("orders_count"):
        lines.append("")
        oc = ph_data.get("orders_count", 0)
        total_quoted = ph_data.get("total_amount", 0)
        total_invoiced = ph_data.get("total_invoiced", 0)
        avg = ph_data.get("avg_ticket", 0)
        top_purchased = ph_data.get("top_products") or []
        top_quoted_only = ph_data.get("top_products_quoted_only") or []

        # Bloque "Compras facturadas" — solo si hay algo facturado real.
        if top_purchased or total_invoiced > 0:
            lines.append("🛒 *Compras facturadas (este año):*")
            lines.append(
                f"• USD {total_invoiced:,.2f} facturado "
                f"(de USD {total_quoted:,.2f} cotizado en {oc} órdenes)"
            )
            if top_purchased:
                lines.append("• *Productos que compraste (facturados):*")
                for p in top_purchased[:3]:
                    code = p.get("code") or ""
                    pname = (p.get("name") or "").strip()
                    qty = p.get("total_qty") or 0
                    qty_q = p.get("total_qty_quoted") or qty
                    tag = f" ({code})" if code else ""
                    # Si la qty facturada < qty cotizada → mostrar parcial
                    if qty_q > qty:
                        lines.append(
                            f"   ▪ {pname}{tag} — {qty:g} u (facturadas de {qty_q:g} cotizadas)"
                        )
                    else:
                        lines.append(f"   ▪ {pname}{tag} — {qty:g} u")
        else:
            # No hay nada facturado todavía — honesto.
            lines.append("🛒 *Aún no tienes compras facturadas este año.*")
            lines.append(f"• Tienes {oc} cotizaciones por USD {total_quoted:,.2f}.")

        # Bloque "Cotizados pero no facturados" — separado del de compras.
        if top_quoted_only:
            lines.append("")
            lines.append("📋 *Productos que cotizaste pero aún no compraste:*")
            for p in top_quoted_only[:3]:
                code = p.get("code") or ""
                pname = (p.get("name") or "").strip()
                qty = p.get("total_qty") or 0
                tag = f" ({code})" if code else ""
                quots = p.get("quotations") or []
                quot_tag = f" — cotización {quots[0]}" if quots else ""
                if len(quots) > 1:
                    quot_tag = f" — cotizaciones {', '.join(quots[:3])}"
                lines.append(f"   ▪ {pname}{tag} — {qty:g} u{quot_tag}")

    rq_data = (activity or {}).get("recent_quotations") or {}
    if rq_data.get("success") and rq_data.get("orders"):
        orders = rq_data["orders"][:5]
        lines.append("")
        lines.append("📋 *Cotizaciones recientes:*")
        for o in orders:
            name_o = o.get("name", "?")
            total_o = o.get("total", 0)
            state_lbl = o.get("state_label") or o.get("state") or ""
            date_o = (o.get("date_order") or "")[:10]
            lines.append(f"• *{name_o}* — USD {total_o:,.2f} · _{state_lbl}_ · {date_o}")

    rinv = (activity or {}).get("recent_invoices") or {}
    if rinv.get("success") and rinv.get("invoices"):
        invs = rinv["invoices"][:5]
        lines.append("")
        lines.append("🧾 *Últimas facturas:*")
        for inv in invs:
            name_i = inv.get("name", "?")
            total_i = inv.get("amount_total", 0)
            residual = inv.get("amount_residual", 0)
            state_p = inv.get("invoice_payment_state", "?")
            date_i = (inv.get("invoice_date") or "")[:10]
            status_lbl = {
                "paid": "pagada",
                "in_payment": "pagada (en pago)",
                "not_paid": f"pendiente USD {residual:,.2f}",
            }.get(state_p, state_p)
            lines.append(f"• *{name_i}* — USD {total_i:,.2f} · _{status_lbl}_ · {date_i}")

    if not any([
        ph_data.get("orders_count"),
        rq_data.get("orders"),
        rinv.get("invoices"),
    ]):
        lines.append("")
        lines.append("_Aún no tienes movimientos registrados — eres cliente nuevo en el sistema._")

    lines.append("")
    lines.append("_Si quieres ver saldo / crédito disponible, dime y te envío un OTP a tu correo._")
    return "\n".join(lines)


async def _execute_tool(request: Request, tool_name: str, args: dict) -> str:
    """Execute a tool and return text result."""
    from mcp_odoo.config import settings
    import httpx

    # Load tenant config (simplified — for now uses env/default tenant)
    # In production, extract JWT from MCP session headers
    tenant_config = await _get_tenant_config(request)

    tc = tenant_config  # shorthand
    creds = (tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"])

    # Extract the session-active quotation header forwarded by the orchestrator.
    # When present, write tools must validate that the LLM is acting on the
    # quotation the user explicitly selected — not one it inferred autonomously.
    _aqid_str = request.headers.get("x-active-quotation-id", "")
    session_active_quotation_id: int | None = (
        int(_aqid_str) if _aqid_str.isdigit() else None
    )

    # Iter 79 (2026-05-22). Channel-aware display formatting for chat
    # gateways. The orchestrator injects X-Channel (whatsapp / telegram /
    # web / api / etc.) on every MCP request. List-style tools enrich
    # their JSON envelope with a ``display_text`` string pre-rendered for
    # the channel — WhatsApp/Telegram get plain-text bullets, web/api
    # see no extra field (the dashboard already renders structured rows
    # in TanStack tables). The LLM is taught (rule iter79) to copy the
    # ``display_text`` verbatim when present.
    _channel_header = (
        request.headers.get("x-channel")
        or request.headers.get("X-Channel")
        or ""
    )
    request_channel: str = str(_channel_header).strip().lower()

    # C1 — Cross-client data leak fix (2026-05-12). The orchestrator pins
    # the expected partner_id for B2C sessions; we use it to reject any
    # quotation-touching tool whose underlying sale.order belongs to a
    # DIFFERENT partner. Header is absent for B2B sellers and for sessions
    # where the customer is not yet identified — those cases skip the
    # cross-partner enforcement here (the orchestrator still runs its
    # own post-tool validation as defence in depth).
    _epid_str = request.headers.get("x-expected-partner-id", "")
    expected_partner_id: int | None = (
        int(_epid_str) if _epid_str.isdigit() else None
    )

    # Tools whose result envelope must belong to ``expected_partner_id``.
    # We resolve the underlying sale.order.partner_id via a single
    # odoo_read and short-circuit with a ``cross_partner_quotation``
    # envelope BEFORE the tool runs — never let it return real data
    # of someone else's quotation.
    _QUOTATION_TOOLS_PARTNER_CHECK = {
        "get_quotation",
        "get_quotation_state_summary",
        "find_quotation_by_name",
        "add_to_quotation",
        "update_quotation_line",
        "remove_quotation_line",
        "confirm_quotation",
        "apply_global_discount",
        "send_quotation",
        "render_quotation_pdf",
        "create_payphone_link",
        "change_quotation_customer",
        "add_quotation_line",
        "set_quotation_header",
        "recalculate_quotation",
        "transition_quotation",
        "sign_quotation",
        "get_quotation_margin",
        "niko_send_sign_request",
        "duplicate_quotation",
    }

    if expected_partner_id and tool_name in _QUOTATION_TOOLS_PARTNER_CHECK:
        _candidate_id = args.get("order_id")
        # update_quotation_line / remove_quotation_line take line_id;
        # resolve the parent order's partner_id from sale.order.line.
        _line_id = args.get("line_id") if tool_name in (
            "update_quotation_line", "remove_quotation_line",
        ) else None
        _check_partner_id: int | None = None
        try:
            from mcp_odoo.tools.generic import odoo_read, odoo_search
            if _line_id:
                try:
                    _line_rows = odoo_read(
                        *creds, "sale.order.line", [int(_line_id)],
                        ["order_id"],
                    )
                except Exception:
                    _line_rows = []
                if _line_rows:
                    _ord_ref = _line_rows[0].get("order_id")
                    if isinstance(_ord_ref, list) and _ord_ref:
                        _candidate_id = _ord_ref[0]
            if _candidate_id:
                try:
                    _ord_rows = odoo_read(
                        *creds, "sale.order", [int(_candidate_id)],
                        ["partner_id"],
                    )
                except Exception:
                    _ord_rows = []
                if _ord_rows:
                    _p_ref = _ord_rows[0].get("partner_id")
                    if isinstance(_p_ref, list) and _p_ref:
                        try:
                            _check_partner_id = int(_p_ref[0])
                        except (TypeError, ValueError):
                            _check_partner_id = None
            # find_quotation_by_name uses ``name`` instead of order_id —
            # resolve via search.
            if (
                tool_name == "find_quotation_by_name"
                and not _check_partner_id
                and args.get("name")
            ):
                try:
                    _by_name_rows = odoo_search(
                        *creds, "sale.order",
                        [["name", "=", str(args.get("name")).strip()]],
                        ["partner_id"], limit=1,
                    )
                except Exception:
                    _by_name_rows = []
                if _by_name_rows:
                    _p_ref = _by_name_rows[0].get("partner_id")
                    if isinstance(_p_ref, list) and _p_ref:
                        try:
                            _check_partner_id = int(_p_ref[0])
                        except (TypeError, ValueError):
                            _check_partner_id = None
        except Exception as _cp_exc:
            logger.warning(
                "cross_partner pre-check failed (tool=%s args=%s): %s — "
                "letting tool run, orchestrator post-validation still applies",
                tool_name, args, _cp_exc,
            )
            _check_partner_id = None
        if _check_partner_id and _check_partner_id != expected_partner_id:
            logger.error(
                "C1 cross_partner_quotation BLOCKED: tool=%s tenant=%s "
                "expected_partner_id=%s order_partner_id=%s order_id=%s",
                tool_name, tc["tenant_id"], expected_partner_id,
                _check_partner_id, _candidate_id,
            )
            return json.dumps({
                "success": False,
                "error_code": "cross_partner_quotation",
                "error_detail": (
                    "Esa cotizacion pertenece a otro cliente. No puedo "
                    "operar sobre ella desde esta sesion. Pidele al "
                    "cliente la cotizacion correcta (suya) o usa "
                    "get_latest_quotation con su partner_id."
                ),
                "expected_partner_id": expected_partner_id,
            }, ensure_ascii=False)

    # get_latest_quotation / list_quotations / get_active_quotation take
    # ``partner_id`` directly — if the LLM passed someone else's id,
    # reject up front so we don't expose the other customer's list.
    if (
        expected_partner_id
        and tool_name in {
            "get_latest_quotation", "list_quotations", "get_active_quotation",
        }
        and args.get("partner_id")
    ):
        try:
            _pid_arg = int(args["partner_id"])
        except (TypeError, ValueError):
            _pid_arg = None
        if _pid_arg and _pid_arg != expected_partner_id:
            logger.error(
                "C1 cross_partner_quotation BLOCKED: tool=%s tenant=%s "
                "expected_partner_id=%s arg_partner_id=%s",
                tool_name, tc["tenant_id"], expected_partner_id, _pid_arg,
            )
            return json.dumps({
                "success": False,
                "error_code": "cross_partner_quotation",
                "error_detail": (
                    "No puedo consultar cotizaciones de otro cliente "
                    "desde esta sesion. Usa el partner_id del cliente "
                    "actual."
                ),
                "expected_partner_id": expected_partner_id,
            }, ensure_ascii=False)

    if tool_name == "search_products":
        # Optional price filters: the LLM passes them when the customer
        # states a budget ("max 500", "hasta 800", "entre 200 y 500").
        # We accept numeric strings too so a fumbled tool call still
        # filters instead of silently ignoring the constraint.
        def _coerce_price(val):
            if val is None or val == "":
                return None
            try:
                f = float(val)
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        _cat_raw = args.get("category_path") or args.get("category_id")
        category_path = (
            str(_cat_raw).strip() if _cat_raw not in (None, "") else None
        )
        _pid_raw = args.get("partner_id")
        try:
            _partner_id_int = int(_pid_raw) if _pid_raw not in (None, "") else None
            if _partner_id_int is not None and _partner_id_int <= 0:
                _partner_id_int = None
        except (TypeError, ValueError):
            _partner_id_int = None
        return await _rag_search(
            args["query"],
            top_k=max(int(args.get("top_k", 10)), 1),
            offset=max(int(args.get("offset", 0)), 0),
            tenant_id=tc["tenant_id"],
            price_min=_coerce_price(args.get("price_min")),
            price_max=_coerce_price(args.get("price_max")),
            category_path=category_path,
            partner_id=_partner_id_int,
        )

    if tool_name == "get_product_details":
        # Read from product.template (catalog row that the Odoo UI
        # shows in its kanban list view), NOT product.product (which
        # holds variants). The embedding indexes templates and the IDs
        # do not match across the two tables.
        from mcp_odoo.tools.generic import odoo_search as _search
        code = args["product_code"].strip()
        _detail_fields = [
            "name", "default_code", "list_price", "standard_price",
            "qty_available", "virtual_available", "description_sale",
            "categ_id", "barcode", "active", "image_128",
        ]
        products = _search(
            *creds,
            "product.template",
            [["default_code", "=ilike", code]],
            fields=_detail_fields,
            limit=1,
        )
        if not products:
            # Try partial match
            products = _search(
                *creds,
                "product.template",
                [["default_code", "ilike", code]],
                fields=_detail_fields,
                limit=5,
            )
        if not products:
            return _ERROR_MAP["product_not_found"]

        # B1: si el orchestrator inyectó partner_id, reescribir el
        # precio mostrado con la pricelist del cliente para no divergir
        # con create_quotation.
        _gpd_pid_raw = args.get("partner_id")
        try:
            _gpd_partner_id = int(_gpd_pid_raw) if _gpd_pid_raw not in (None, "") else None
            if _gpd_partner_id is not None and _gpd_partner_id <= 0:
                _gpd_partner_id = None
        except (TypeError, ValueError):
            _gpd_partner_id = None
        pricelist_prices: dict[int, float] = {}
        if _gpd_partner_id:
            _gpd_live = {
                int(p["id"]): {"price": float(p.get("list_price") or 0)}
                for p in products if isinstance(p.get("id"), int)
            }
            if _gpd_live:
                await _apply_pricelist_to_live(
                    tc["tenant_id"], _gpd_partner_id, _gpd_live,
                )
                for tid, data in _gpd_live.items():
                    if isinstance(data, dict) and "price" in data:
                        pricelist_prices[tid] = float(data["price"])

        base_url = tc["url"].rstrip("/")
        results = []
        for p in products:
            # Build image URL: Odoo serves product images via /web/image
            has_image = bool(p.get("image_128"))
            image_url = (
                f"{base_url}/web/image/product.template/{p['id']}/image_256"
                if has_image else None
            )
            list_price = p.get("list_price", 0)
            effective_price = pricelist_prices.get(p["id"], list_price)
            # ``cost`` is the internal supplier price. It is intentionally
            # not surfaced to the customer-facing tool result: every time
            # we exposed it, the LLM either leaked it ("internal_cost_leak"
            # in response_guard) or responded blandly to avoid the leak
            # (prod 2026-05-16: Mario asked "cuánto cuesta el PCD0048" and
            # the bot answered "Buen día Mario, ¿en qué puedo ayudarte?"
            # to dodge cost=162.45 instead of stating price=229.99). The
            # seller-facing flow uses get_pricelist_price for discount
            # calcs — that tool surfaces ``discount_applied`` instead.
            # ``list_price`` is also withheld unless asked: when it differs
            # from the effective price, surfacing both confuses the LLM
            # (which price to quote?). If the customer asks "¿y el precio
            # público?" the agent can call get_pricelist_price.
            entry = {
                "id": p["id"],
                "code": p.get("default_code", ""),
                "name": p.get("name", ""),
                "price": effective_price,
                "stock": p.get("qty_available", 0),
                "available": p.get("virtual_available", 0),
                "description": p.get("description_sale") or "",
                "category": p.get("categ_id", [None, ""])[1] if isinstance(p.get("categ_id"), list) else "",
                "barcode": p.get("barcode") or "",
                "image_url": image_url,
            }
            results.append(entry)
        return json.dumps(results if len(results) > 1 else results[0], indent=2, ensure_ascii=False, default=str)

    if tool_name == "odoo_search":
        model = args["model"]
        if model in BLOCKED_MODELS:
            return (
                "Acceso a modelos financieros bloqueado. "
                "Use check_balance con verificacion OTP para consultar datos financieros."
            )
        req_fields = set(args.get("fields") or [])
        if model == "res.partner" and req_fields & BLOCKED_PARTNER_FIELDS:
            return (
                "Los campos financieros de res.partner requieren verificacion OTP. "
                "Use check_balance con verificacion OTP."
            )
        from mcp_odoo.tools.generic import odoo_search
        result = odoo_search(
            *creds,
            model, args["domain"], args.get("fields"), args.get("limit", 10),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "check_stock":
        from mcp_odoo.tools.inventory import odoo_check_stock
        result = odoo_check_stock(*creds, args["product_ids"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "sri_status_report":
        from mcp_odoo.tools.sri_report import sri_status_report
        # Devuelve texto monospace listo para Telegram (NO json) — Lila lo
        # muestra tal cual; ya viene clasificado (pendientes + informativo).
        return sri_status_report(*creds)

    if tool_name == "search_partner":
        return await _rag_search_partners(args["query"], args.get("top_k", 5), tc["tenant_id"])

    if tool_name == "get_partner_profile":
        from mcp_odoo.tools.generic import odoo_read
        # Iter 79: dado un partner_id, leer datos del res.partner desde Odoo.
        # Cubre el gap entre search_partner (texto → id) y los flujos que
        # ya tienen id pero no datos. Evidencia (WhatsApp 2026-05-23): el
        # bot tenia partner_id=62 en sesion pero pregunta "que sabes de mi"
        # → llamo solo honcho_peer_representation que devolvio 31 chars y
        # respondio "no tengo info previa". Esta tool da fallback directo.
        try:
            partner_id = int(args["partner_id"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({
                "success": False,
                "error_code": "invalid_partner_id",
                "error": "partner_id debe ser un entero positivo.",
            }, ensure_ascii=False)
        if partner_id <= 0:
            return json.dumps({
                "success": False,
                "error_code": "invalid_partner_id",
                "error": "partner_id debe ser > 0.",
            }, ensure_ascii=False)
        # Default fields — names + contact + identificacion fiscal +
        # location. Si el LLM pide subset, lo respetamos. Evitamos campos
        # con datos sensibles que no son utiles para conversacion (ej:
        # property_payment_term_id, credit, credit_limit — esos viven en
        # get_customer_credit_status que requiere OTP).
        default_fields = [
            "id", "name", "display_name",
            "email", "email_normalized",
            "phone", "mobile",
            "vat",
            "street", "street2", "city", "zip",
            "state_id", "country_id",
            "lang", "tz",
            "customer_rank", "supplier_rank",
            "is_company", "parent_id",
            "comment",
        ]
        req_fields = args.get("fields") or default_fields
        if not isinstance(req_fields, list) or not req_fields:
            req_fields = default_fields
        # Drop fields absent on this Odoo version (e.g. mobile in Odoo 17+).
        from mcp_odoo.tools.generic import valid_partner_fields
        req_fields = valid_partner_fields(*creds, req_fields)
        try:
            rows = odoo_read(*creds, "res.partner", [partner_id], req_fields)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_partner_profile: odoo_read failed tenant=%s id=%s: %s",
                tc["tenant_id"], partner_id, exc,
            )
            return json.dumps({
                "success": False,
                "error_code": "odoo_read_failed",
                "error": f"No pude leer el partner {partner_id}: {exc}",
            }, ensure_ascii=False)
        if not rows:
            return json.dumps({
                "success": False,
                "error_code": "partner_not_found",
                "error": f"No existe un partner con id={partner_id}.",
            }, ensure_ascii=False)
        partner = rows[0]
        # Many2one fields llegan como [id, name] — flatten a {id, name}.
        for fk in ("state_id", "country_id", "parent_id"):
            v = partner.get(fk)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                partner[fk] = {"id": v[0], "name": v[1]}
            elif v is False:
                partner[fk] = None
        # Normalizar Falses booleanos de XMLRPC a None para campos texto.
        for k, v in list(partner.items()):
            if v is False and k not in ("is_company",):
                partner[k] = None

        # Iter 79b: include_activity → enriquecer con purchase history +
        # facturas. Owner-feedback (WhatsApp 2026-05-23 trace 85e7fa65):
        # Qwen3 router llamaba get_partner_profile({partner_id: 62}) sin
        # el flag → handler devolvia solo contacto → bot decia "no tengo
        # historial de compras en memoria" pese a haber compras reales.
        # Default ahora TRUE para que el caso mas comun ("que sabes de
        # mi") devuelva todo sin que el LLM tenga que recordarlo. El LLM
        # puede pasar false explicito si solo quiere contacto.
        include_activity = bool(args.get("include_activity", True))
        activity = None
        if include_activity:
            from mcp_odoo.tools.sales import (
                odoo_get_customer_purchase_history,
                odoo_list_quotations,
            )
            from mcp_odoo.tools.generic import odoo_search as _odoo_search
            activity = {
                "purchase_history": None,
                "recent_quotations": None,
                "recent_invoices": None,
            }
            # 1. Historial de compras (incluye top_products + avg_ticket)
            try:
                activity["purchase_history"] = odoo_get_customer_purchase_history(
                    *creds, partner_id=partner_id, limit=5,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_partner_profile activity.purchase_history failed: %s", exc,
                )
                activity["purchase_history"] = {"success": False, "error": str(exc)}
            # 2. Cotizaciones recientes (todos los estados)
            try:
                activity["recent_quotations"] = odoo_list_quotations(
                    *creds, partner_id=partner_id, limit=5,
                    states=["draft", "sent", "sale", "done", "cancel"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_partner_profile activity.recent_quotations failed: %s", exc,
                )
                activity["recent_quotations"] = {"success": False, "error": str(exc)}
            # 3. Facturas recientes (account.move type=out_invoice/out_refund)
            try:
                invs = _odoo_search(
                    *creds, "account.move",
                    [
                        ["partner_id", "=", partner_id],
                        ["type", "in", ["out_invoice", "out_refund"]],
                        ["state", "=", "posted"],
                    ],
                    fields=[
                        "id", "name", "invoice_date",
                        "amount_total", "amount_residual",
                        "invoice_payment_state", "type",
                    ],
                    limit=5,
                    order="invoice_date desc, id desc",
                )
                activity["recent_invoices"] = {"success": True, "invoices": invs}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_partner_profile activity.recent_invoices failed: %s", exc,
                )
                activity["recent_invoices"] = {"success": False, "error": str(exc)}

        # Iter 79b: display_text WhatsApp-friendly cuando el canal lo amerita
        # o include_activity=true (el LLM lo usa tal cual sin re-formatear).
        _ch_for_fmt = (
            request.headers.get("x-channel")
            or request.headers.get("X-Channel")
            or ""
        ).strip().lower()
        display_text: str | None = None
        if include_activity or _ch_for_fmt in ("whatsapp", "telegram"):
            display_text = _format_partner_profile_chat(
                partner=partner, activity=activity,
            )

        result = {
            "success": True,
            "partner": partner,
            "partner_id": partner_id,
        }
        if activity is not None:
            result["activity"] = activity
        if display_text:
            result["display_text"] = display_text
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

    if tool_name == "identify_customer":
        return await _identify_customer(creds, args["cedula_ruc"])

    if tool_name == "get_company_settings":
        from mcp_odoo.tools.generic import odoo_search as _search
        # Find consumidor final partner (vat=9999999999999)
        cf_partner_id = None
        try:
            partners = _search(
                *creds, "res.partner",
                [["vat", "=", "9999999999999"], ["active", "=", True]],
                ["id", "name"], 1,
            )
            if partners:
                cf_partner_id = partners[0]["id"]
        except Exception as e:
            logger.warning("get_company_settings: error searching consumidor final partner: %s", e)

        # Try to read config parameters via ir.config_parameter
        pedir_end_customer_data = True  # safe default
        sri_invoice_limit = 50.0  # Ecuador SRI default 2025+
        try:
            params = _search(
                *creds, "ir.config_parameter",
                [["key", "in", ["sale.pedir_end_customer_data",
                                "sale.sale_customer_invoice_limit_sri",
                                "sale.end_customer_default_id"]]],
                ["key", "value"], 10,
            )
            for p in (params or []):
                if p["key"] == "sale.pedir_end_customer_data":
                    pedir_end_customer_data = str(p["value"]).lower() in ("true", "1", "yes")
                elif p["key"] == "sale.sale_customer_invoice_limit_sri":
                    try:
                        sri_invoice_limit = float(p["value"])
                    except (ValueError, TypeError):
                        pass
                elif p["key"] == "sale.end_customer_default_id":
                    try:
                        cf_partner_id = cf_partner_id or int(p["value"])
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.warning("get_company_settings: cannot read ir.config_parameter (access denied?): %s", e)

        result = {
            "success": True,
            "consumidor_final_partner_id": cf_partner_id,
            "pedir_end_customer_data": pedir_end_customer_data,
            "sri_invoice_limit": sri_invoice_limit,
        }
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "create_quotation":
        from mcp_odoo.tools.sales import odoo_create_quotation
        result = odoo_create_quotation(
            *creds, args["partner_id"], args["lines"], args.get("notes", ""),
            end_customer_name=args.get("end_customer_name"),
            end_customer_phone=args.get("end_customer_phone"),
            end_customer_email=args.get("end_customer_email"),
            salesperson_user_id=args.get("salesperson_user_id"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "add_to_quotation":
        from mcp_odoo.tools.sales import odoo_add_to_quotation
        result = odoo_add_to_quotation(
            *creds,
            args["order_id"],
            args["lines"],
            confirmed=bool(args.get("confirmed", False)),
            session_active_quotation_id=session_active_quotation_id,
            salesperson_user_id=args.get("salesperson_user_id"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_active_quotation":
        from mcp_odoo.tools.sales import odoo_get_active_quotation
        result = odoo_get_active_quotation(*creds, args["partner_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "list_quotations":
        from mcp_odoo.tools.sales import odoo_list_quotations
        result = odoo_list_quotations(
            *creds,
            args["partner_id"],
            args.get("limit", 10),
            args.get("states"),
        )
        # Iter 79: chat channels need a plain-text render — markdown
        # tables don't render on WhatsApp/Telegram. We compute a state
        # label hint from the request when the caller filtered (e.g.
        # ['draft'] → "borrador"). Web/api callers see no extra field.
        _states_arg = args.get("states") or []
        _state_hint = None
        if isinstance(_states_arg, list) and len(_states_arg) == 1:
            _state_hint = {
                "draft": "borrador",
                "sent": "enviadas",
                "sale": "confirmadas",
                "done": "completadas",
                "cancel": "canceladas",
            }.get(str(_states_arg[0]).lower())
        result = _attach_display_text(
            result, request_channel, kind="quotations",
            state_filter=_state_hint,
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_quotation":
        from mcp_odoo.tools.sales import odoo_get_quotation
        result = odoo_get_quotation(*creds, args["order_id"])
        # Iter 79: chat-friendly detail block for WhatsApp/Telegram.
        result = _attach_display_text(
            result, request_channel, kind="quotation_detail",
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "odoo_lookup_user_by_email":
        from mcp_odoo.tools.sales import odoo_lookup_user_by_email
        result = odoo_lookup_user_by_email(*creds, email=args["email"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "find_quotation_by_name":
        from mcp_odoo.tools.sales import odoo_find_quotation_by_name
        result = odoo_find_quotation_by_name(*creds, args["name"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_latest_quotation":
        from mcp_odoo.tools.sales import get_latest_quotation
        result = get_latest_quotation(
            *creds,
            partner_id=int(args["partner_id"]),
            states=args.get("states"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "render_quotation_pdf":
        from mcp_odoo.tools.sales import odoo_render_quotation_pdf
        result = odoo_render_quotation_pdf(*creds, args["order_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "send_quotation":
        from mcp_odoo.tools.sales import odoo_send_quotation
        result = odoo_send_quotation(
            *creds,
            args["order_id"],
            confirmed=bool(args.get("confirmed", False)),
            session_active_quotation_id=session_active_quotation_id,
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "confirm_quotation":
        from mcp_odoo.tools.sales import odoo_confirm_sale_order
        result = odoo_confirm_sale_order(
            *creds,
            args["order_id"],
            confirmed=bool(args.get("confirmed", False)),
            session_active_quotation_id=session_active_quotation_id,
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # --- Sprint C: edition tools (full sale.order coverage) ---

    if tool_name == "update_quotation_line":
        _guard = _assert_quotation_editable_by_line(creds, int(args["line_id"]))
        if _guard is not None:
            return json.dumps(_guard, indent=2, ensure_ascii=False)
        from mcp_odoo.tools.sales import odoo_update_quotation_line
        result = odoo_update_quotation_line(
            *creds,
            args["line_id"],
            quantity=args.get("quantity"),
            price_unit=args.get("price_unit"),
            discount=args.get("discount"),
            name=args.get("name"),
            product_id=args.get("product_id"),
            code=args.get("code"),
            confirmed=bool(args.get("confirmed", False)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "remove_quotation_line":
        _guard = _assert_quotation_editable_by_line(creds, int(args["line_id"]))
        if _guard is not None:
            return json.dumps(_guard, indent=2, ensure_ascii=False)
        from mcp_odoo.tools.sales import odoo_remove_quotation_line
        result = odoo_remove_quotation_line(
            *creds,
            args["line_id"],
            mode=args.get("mode", "auto"),
            confirmed=bool(args.get("confirmed", False)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "change_quotation_customer":
        _guard = _assert_quotation_editable_by_order(creds, int(args["order_id"]))
        if _guard is not None:
            return json.dumps(_guard, indent=2, ensure_ascii=False)
        from mcp_odoo.tools.sales import odoo_change_quotation_customer
        result = odoo_change_quotation_customer(
            *creds,
            args["order_id"],
            args["partner_id"],
            propagate_pricelist=bool(args.get("propagate_pricelist", True)),
            propagate_payment_term=bool(args.get("propagate_payment_term", True)),
            propagate_addresses=bool(args.get("propagate_addresses", True)),
            reprice_lines=bool(args.get("reprice_lines", False)),
            confirmed=bool(args.get("confirmed", False)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "apply_global_discount":
        _guard = _assert_quotation_editable_by_order(creds, int(args["order_id"]))
        if _guard is not None:
            return json.dumps(_guard, indent=2, ensure_ascii=False)
        from mcp_odoo.tools.sales import odoo_apply_global_discount
        result = odoo_apply_global_discount(
            *creds,
            args["order_id"],
            args["discount_type"],
            float(args["discount_rate"]),
            confirmed=bool(args.get("confirmed", False)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "set_quotation_header":
        _guard = _assert_quotation_editable_by_order(creds, int(args["order_id"]))
        if _guard is not None:
            return json.dumps(_guard, indent=2, ensure_ascii=False)
        from mcp_odoo.tools.sales import odoo_set_quotation_header
        # Forward only known header fields, drop everything else.
        header_fields = (
            "date_order", "validity_date", "payment_term_id",
            "pricelist_id", "user_id", "note", "client_order_ref",
            "invoice_date",
        )
        kw = {k: args[k] for k in header_fields if k in args and args[k] is not None}
        result = odoo_set_quotation_header(
            *creds,
            args["order_id"],
            confirmed=bool(args.get("confirmed", False)),
            **kw,
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "add_quotation_line":
        _guard = _assert_quotation_editable_by_order(creds, int(args["order_id"]))
        if _guard is not None:
            return json.dumps(_guard, indent=2, ensure_ascii=False)
        from mcp_odoo.tools.sales import odoo_add_quotation_line
        result = odoo_add_quotation_line(
            *creds,
            args["order_id"],
            args.get("product_id"),
            float(args.get("quantity", 1)),
            code=args.get("code"),
            price_unit=args.get("price_unit"),
            discount=args.get("discount"),
            name=args.get("name"),
            confirmed=bool(args.get("confirmed", False)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "recalculate_quotation":
        from mcp_odoo.tools.sales import odoo_recalculate_quotation
        result = odoo_recalculate_quotation(*creds, args["order_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_quotation_state_summary":
        from mcp_odoo.tools.sales import odoo_get_quotation_state_summary
        result = odoo_get_quotation_state_summary(*creds, args["order_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "transition_quotation":
        from mcp_odoo.tools.sales import odoo_transition_quotation
        result = odoo_transition_quotation(
            *creds,
            args["order_id"],
            args["action"],
            confirmed=bool(args.get("confirmed", False)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "sign_quotation":
        from mcp_odoo.tools.sales import odoo_sign_quotation
        result = odoo_sign_quotation(
            *creds,
            order_id=int(args["order_id"]),
            signature=args["signature"],
            signed_by_name=args["signed_by_name"],
            auto_confirm=args.get("auto_confirm", True),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "create_payphone_link":
        from mcp_odoo.tools.payments import odoo_create_payphone_link
        result = odoo_create_payphone_link(
            *creds,
            order_id=int(args["order_id"]),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "check_payphone_status":
        from mcp_odoo.tools.payments import odoo_check_payphone_status
        result = odoo_check_payphone_status(
            *creds,
            client_tx_id=str(args["client_tx_id"]),
            refresh=bool(args.get("refresh", False)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "niko_send_sign_request":
        # Delegate to the niko backend so the channel-specific gateway
        # handles delivery. The MCP server INTENTIONALLY does not know
        # how to talk to Telegram / WhatsApp — that's the backend's job.
        # We just forward the args along with the tenant_id resolved
        # earlier (via JWT or X-Tenant-ID) so the backend can validate
        # ownership against Odoo and mint the HMAC sign_token.
        from mcp_odoo.config import settings as _s

        niko_url = (_s.niko_api_url or "").rstrip("/")
        if not niko_url:
            return json.dumps({
                "success": False,
                "error_code": "niko_api_url_not_configured",
                "error_detail": (
                    "El backend de Niko no esta configurado en el MCP. "
                    "Pide al operador que configure NIKO_API_URL."
                ),
            }, ensure_ascii=False)

        # Service-role JWT — same key the MCP already uses to talk to
        # Supabase REST. Niko's get_current_user accepts service_role
        # tokens and reads tenant_id from the body for that path.
        bearer = _s.supabase_service_key or _s.supabase_jwt_secret
        if not bearer:
            return json.dumps({
                "success": False,
                "error_code": "service_token_missing",
                "error_detail": (
                    "Falta SUPABASE_SERVICE_KEY/SUPABASE_JWT_SECRET en "
                    "el MCP — no puedo autenticarme contra el backend."
                ),
            }, ensure_ascii=False)

        order_id = int(args["order_id"])
        # Iter 78b: channel + channel_user_id se resuelven desde headers
        # X-Channel / X-Channel-User-Id inyectados por el orchestrator de
        # niko. El inputSchema de esta tool declara que solo `order_id` es
        # requerido (descripción: "el backend resuelve el canal y
        # destinatario automáticamente desde el contexto del chat") — leer
        # args["channel"] crudo provocaba KeyError → LLM retry → recursion
        # limit 25 (trace efeb5032 WhatsApp 2026-05-22 "Quiero firmar").
        # args[] sigue siendo fallback para callers que sí los manden.
        _ch = (args.get("channel")
               or request.headers.get("x-channel")
               or request.headers.get("X-Channel")
               or "")
        _cuid = (args.get("channel_user_id")
                 or request.headers.get("x-channel-user-id")
                 or request.headers.get("X-Channel-User-Id")
                 or "")
        if not _ch or not _cuid:
            return json.dumps({
                "success": False,
                "error_code": "missing_channel_context",
                "error_detail": (
                    "No pude resolver el canal/destinatario del chat "
                    "actual desde el contexto. Verifica que la request "
                    "incluya headers X-Channel y X-Channel-User-Id."
                ),
            }, ensure_ascii=False)
        body = {
            "channel": str(_ch).strip().lower(),
            "channel_user_id": str(_cuid).strip(),
            "message_prefix": str(args.get("message_prefix", "") or ""),
            "tenant_id": tc["tenant_id"],
            # Forward agent_slug so the backend picks the right Telegram
            # bot when the tenant has multiple (Niko B2C + Yarvis B2B).
            "agent_slug": (
                args.get("agent_slug")
                or request.headers.get("x-agent-slug")
                or request.headers.get("X-Agent-Slug")
                or ""
            ).strip(),
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{niko_url}/api/quotations/{order_id}/send-sign-request",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "Content-Type": "application/json",
                        # Forward X-Tenant-ID too so the backend's
                        # downstream MCP loop can resolve credentials
                        # if it falls back to the header path.
                        "X-Tenant-ID": tc["tenant_id"],
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "niko_send_sign_request transport error tenant=%s order=%s: %s",
                tc["tenant_id"], order_id, exc,
            )
            return json.dumps({
                "success": False,
                "error_code": "niko_unreachable",
                "error_detail": "No pude contactar al backend de Niko.",
            }, ensure_ascii=False)

        if resp.status_code in (200, 201):
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {}
            return json.dumps({
                "success": bool(data.get("success", True)),
                "sign_url": data.get("sign_url"),
                "sent_message_id": data.get("sent_message_id"),
                "error": data.get("error"),
            }, ensure_ascii=False)

        # Surface Niko's structured error verbatim so the LLM sees
        # something actionable ("La cotizacion ya esta firmada") and
        # the model doesn't retry blindly.
        try:
            err_body = resp.json()
            err_detail = (
                err_body.get("detail")
                or err_body.get("error")
                or f"HTTP {resp.status_code}"
            )
        except Exception:  # noqa: BLE001
            err_detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
        return json.dumps({
            "success": False,
            "error_code": f"niko_http_{resp.status_code}",
            "error_detail": str(err_detail),
        }, ensure_ascii=False)

    if tool_name == "sri_import":
        from mcp_odoo.tools.sri import sri_import_create
        result = sri_import_create(*creds, args["access_key"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "request_otp":
        from mcp_odoo.config import settings as _s
        _supa_url = _s.supabase_url or "http://localhost:8000"
        _supa_key = _s.supabase_service_key or _s.supabase_jwt_secret
        _tenant = tc["tenant_id"]
        partner_id = args["partner_id"]
        # Iter 81b: skip OTP generation when session is already valid.
        # Bug WhatsApp 2026-05-23 trace 63360085: cliente verificó OTP a
        # las 02:13 (sesion valida hasta 24-may 02:13). A las 02:15 pidio
        # "dame mi saldo" → LLM principal llamo request_otp(partner_id=62)
        # SIN chequear si ya habia sesion → MCP envio nuevo email al
        # cliente innecesariamente. Fix: si _otp_check_session devuelve
        # true, devolver un short-circuit "ya estas autorizado" para que
        # el LLM llame check_balance directo.
        _ch_for_check = (
            args.get("channel")
            or request.headers.get("x-channel")
            or request.headers.get("X-Channel")
            or ""
        ).strip().lower()
        if _ch_for_check:
            has_session = await _otp_check_session(
                _supa_url, _supa_key, _tenant, int(partner_id), _ch_for_check,
            )
            if has_session:
                return json.dumps({
                    "success": True,
                    "already_verified": True,
                    "message": (
                        "El cliente ya tiene una sesion OTP verificada y "
                        "valida. NO envies otro codigo — puedes consultar "
                        "datos financieros directamente (check_balance, "
                        "get_customer_invoices, get_customer_payments, "
                        "get_customer_statement)."
                    ),
                }, ensure_ascii=False)
        email = (args.get("email") or "").strip()
        if not email:
            try:
                from mcp_odoo.tools.generic import odoo_read as _odoo_read_otp
                rows = _odoo_read_otp(
                    tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"],
                    "res.partner", [int(partner_id)], ["email", "email_normalized"],
                )
                if rows:
                    email = (rows[0].get("email") or rows[0].get("email_normalized") or "").strip()
            except Exception as _e_lookup:
                logger.warning(
                    "request_otp: failed to read email for partner=%s tenant=%s: %s",
                    partner_id, tc["tenant_id"], _e_lookup,
                )
        channel = (
            args.get("channel")
            or request.headers.get("x-channel")
            or request.headers.get("X-Channel")
            or ""
        ).strip().lower()
        channel_user_id = (
            args.get("channel_user_id")
            or request.headers.get("x-channel-user-id")
            or request.headers.get("X-Channel-User-Id")
            or ""
        ).strip()

        if not channel or not channel_user_id:
            return json.dumps({
                "success": False,
                "error_code": "missing_channel_context",
                "error": (
                    "No pude resolver el canal del chat actual. Asegurate "
                    "de que el orchestrator envie los headers X-Channel y "
                    "X-Channel-User-Id."
                ),
            }, ensure_ascii=False)

        if not email or "@" not in email:
            return json.dumps({
                "success": False,
                "error_code": "no_email_on_file",
                "error": (
                    "El cliente no tiene correo electronico registrado en "
                    "Odoo (res.partner.email vacio). Pidele al cliente que "
                    "te indique un email valido y luego registralo en su "
                    "ficha — o llama otra tool de OTP que use un canal "
                    "alternativo."
                ),
            }, ensure_ascii=False)

        code, error = await _otp_generate(_supa_url, _supa_key, _tenant, partner_id, channel, channel_user_id)
        if error:
            return json.dumps({"success": False, "error": error}, ensure_ascii=False)

        sent, send_msg = _send_otp_email(email, code, tenant_id=_tenant, supa_url=_supa_url, supa_key=_supa_key)
        email_masked = f"{email[:3]}***{email[email.index('@'):]}" if "@" in email else "***"

        if not sent:
            if send_msg == "SMTP_NOT_CONFIGURED":
                # Dev mode: return code directly (REMOVE IN PRODUCTION)
                return json.dumps({
                    "success": True,
                    "message": f"[DEV] SMTP no configurado. Codigo: {code}. En produccion se enviaria a {email_masked}.",
                    "email_masked": email_masked,
                    "dev_code": code,
                }, ensure_ascii=False, indent=2)
            return json.dumps({"success": False, "error": f"No se pudo enviar el correo: {send_msg}"}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "message": f"Codigo de verificacion enviado a {email_masked}. Valido por 15 minutos.",
            "email_masked": email_masked,
        }, ensure_ascii=False, indent=2)

    if tool_name == "verify_otp":
        from mcp_odoo.config import settings as _s
        _supa_url = _s.supabase_url or "http://localhost:8000"
        _supa_key = _s.supabase_service_key or _s.supabase_jwt_secret
        _tenant = tc["tenant_id"]
        # Iter 78c: channel desde args O X-Channel header (mismo patron que
        # request_otp / niko_send_sign_request).
        _channel_v = (
            args.get("channel")
            or request.headers.get("x-channel")
            or request.headers.get("X-Channel")
            or ""
        ).strip().lower()
        if not _channel_v:
            return json.dumps({
                "success": False,
                "error_code": "missing_channel_context",
                "message": "No pude resolver el canal del chat actual.",
            }, ensure_ascii=False)
        success, msg = await _otp_verify(
            _supa_url, _supa_key, _tenant,
            args["partner_id"], _channel_v, args["code"],
        )
        return json.dumps({"success": success, "message": msg}, ensure_ascii=False, indent=2)

    if tool_name == "check_balance":
        partner_id = args["partner_id"]
        channel = args.get("channel", "unknown")
        # Check for valid OTP session
        from mcp_odoo.config import settings as _settings
        _supa_url = _settings.supabase_url or "http://localhost:8000"
        _supa_key = _settings.supabase_service_key or _settings.supabase_jwt_secret
        _tenant = tc["tenant_id"]
        has_session = await _otp_check_session(_supa_url, _supa_key, _tenant, partner_id, channel)
        if not has_session:
            return (
                "VERIFICACION REQUERIDA: El cliente no ha verificado su identidad. "
                "Para mostrar datos financieros, primero usa request_otp para enviar "
                "un codigo al correo del cliente, luego verify_otp cuando te de el codigo."
            )
        from mcp_odoo.tools.sales import odoo_check_balance
        result = odoo_check_balance(*creds, partner_id)
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # ----- ZETA iter 80: invoice / payment / statement tools -------------
    # All four require an OTP session because they expose financial data
    # (invoice amounts, payment records, statement totals). The OTP guard
    # follows the same pattern as ``check_balance`` above. ``channel`` is
    # resolved with the iter78 priority chain (args → X-Channel header)
    # so the LLM never has to pass it.
    if tool_name in (
        "get_customer_invoices",
        "get_invoice_detail",
        "get_customer_payments",
        "get_customer_statement",
    ):
        from mcp_odoo.config import settings as _settings
        _supa_url = _settings.supabase_url or "http://localhost:8000"
        _supa_key = _settings.supabase_service_key or _settings.supabase_jwt_secret
        _tenant = tc["tenant_id"]
        _channel = (
            args.get("channel")
            or request.headers.get("x-channel")
            or request.headers.get("X-Channel")
            or "unknown"
        ).strip().lower() or "unknown"

        # OTP gate. For get_invoice_detail we don't have partner_id in args;
        # resolve it from the invoice itself BEFORE the OTP check so we can
        # gate against the right partner.
        _otp_partner_id: int | None = None
        if tool_name == "get_invoice_detail":
            try:
                _inv_id = int(args["invoice_id"])
            except (KeyError, TypeError, ValueError):
                return json.dumps({
                    "success": False,
                    "error_code": "invalid_invoice_id",
                    "error_detail": "invoice_id requerido (entero).",
                }, ensure_ascii=False)
            try:
                from mcp_odoo.tools.generic import odoo_read as _odoo_read
                _rows = _odoo_read(
                    *creds, "account.move", [_inv_id], ["partner_id"],
                )
                if _rows:
                    _pid = _rows[0].get("partner_id")
                    if isinstance(_pid, (list, tuple)) and _pid:
                        _otp_partner_id = int(_pid[0])
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "get_invoice_detail: partner lookup failed inv=%s: %s",
                    _inv_id, _exc,
                )
        else:
            try:
                _otp_partner_id = int(args["partner_id"])
            except (KeyError, TypeError, ValueError):
                return json.dumps({
                    "success": False,
                    "error_code": "invalid_partner_id",
                    "error_detail": "partner_id requerido (entero).",
                }, ensure_ascii=False)

        # Cross-partner enforcement (defence in depth — orchestrator also
        # checks). When the gateway pinned X-Expected-Partner-Id, the
        # invoice/partner_id requested MUST match it.
        if (
            expected_partner_id
            and _otp_partner_id
            and expected_partner_id != _otp_partner_id
        ):
            return json.dumps({
                "success": False,
                "error_code": "cross_partner_invoice",
                "error_detail": (
                    "El recurso pertenece a otro cliente. Identifica al "
                    "cliente correcto antes de consultar sus facturas."
                ),
            }, ensure_ascii=False)

        if _otp_partner_id:
            has_session = await _otp_check_session(
                _supa_url, _supa_key, _tenant, _otp_partner_id, _channel,
            )
            if not has_session:
                return (
                    "VERIFICACION REQUERIDA: El cliente no ha verificado su identidad. "
                    "Para mostrar facturas, pagos o estado de cuenta, primero usa "
                    "request_otp para enviar un codigo al correo del cliente y luego "
                    "verify_otp cuando te de el codigo."
                )

        from mcp_odoo.tools.invoices import (
            odoo_get_customer_invoices,
            odoo_get_customer_payments,
            odoo_get_customer_statement,
            odoo_get_invoice_detail,
        )
        if tool_name == "get_customer_invoices":
            result = odoo_get_customer_invoices(
                *creds,
                partner_id=int(args["partner_id"]),
                state=args.get("state", "all"),
                limit=args.get("limit", 10),
                year=args.get("year"),
            )
            result = _attach_display_text(result, request_channel, kind="invoices")
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        if tool_name == "get_invoice_detail":
            result = odoo_get_invoice_detail(
                *creds,
                invoice_id=int(args["invoice_id"]),
                include_lines=bool(args.get("include_lines", True)),
                include_taxes=bool(args.get("include_taxes", True)),
            )
            result = _attach_display_text(result, request_channel, kind="invoice_detail")
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        if tool_name == "get_customer_payments":
            result = odoo_get_customer_payments(
                *creds,
                partner_id=int(args["partner_id"]),
                limit=args.get("limit", 10),
                year=args.get("year"),
            )
            result = _attach_display_text(result, request_channel, kind="payments")
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        if tool_name == "get_customer_statement":
            result = odoo_get_customer_statement(
                *creds,
                partner_id=int(args["partner_id"]),
                days_back=args.get("days_back", 90),
            )
            result = _attach_display_text(result, request_channel, kind="statement")
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # ----- ETA iter 81: PDF report tools ---------------------------------
    # Four tools that wrap the official Odoo HTTP report endpoint so the
    # customer receives the SAME PDF Odoo generates internally (statement
    # cron mail / RIDE SRI / etc). Like the ZETA iter80 read tools they
    # require an OTP-verified session — financial documents must not be
    # served without identity check.
    if tool_name in (
        "get_customer_statement_pdf",
        "get_invoice_pdf",
        "get_credit_note_pdf",
        "get_retention_pdf",
    ):
        from mcp_odoo.config import settings as _settings_pdf
        from mcp_odoo.tools.odoo_reports import (
            OdooReportError,
            fetch_odoo_report_pdf,
        )

        _supa_url = _settings_pdf.supabase_url or "http://localhost:8000"
        _supa_key = _settings_pdf.supabase_service_key or _settings_pdf.supabase_jwt_secret
        _tenant = tc["tenant_id"]
        _channel = (
            args.get("channel")
            or request.headers.get("x-channel")
            or request.headers.get("X-Channel")
            or "unknown"
        ).strip().lower() or "unknown"

        # Resolve partner_id for the OTP gate.
        # * statement_pdf: pasa partner_id directo.
        # * invoice_pdf / credit_note_pdf / retention_pdf: resolver desde
        #   el account.move antes de gatear OTP (mismo patron que
        #   get_invoice_detail).
        _otp_partner_id: int | None = None
        # account.move row, cached for the dispatch below (saves a second
        # odoo_read when we already know type + name).
        _move_row: dict | None = None
        _id_arg_name: str
        _id_arg_value: int
        if tool_name == "get_customer_statement_pdf":
            try:
                _otp_partner_id = int(args["partner_id"])
            except (KeyError, TypeError, ValueError):
                return json.dumps({
                    "success": False,
                    "error_code": "invalid_partner_id",
                    "error_detail": "partner_id requerido (entero).",
                }, ensure_ascii=False)
        else:
            # invoice_pdf / credit_note_pdf / retention_pdf
            _id_arg_name = (
                "invoice_id" if tool_name == "get_invoice_pdf"
                else ("refund_id" if tool_name == "get_credit_note_pdf"
                      else "retention_id")
            )
            # Iter 81d: aceptar tambien `invoice_name` / `name` cuando el
            # cliente nombra el documento ("Mandame el RIDE de FACV/2025/
            # 4897"). Qwen3 router pasaba el name sin resolver el id →
            # MCP rechazaba con invalid_arguments. Estrategia: si no hay
            # entero valido, intentar resolver desde el name con
            # odoo_search.
            from mcp_odoo.tools.generic import (
                odoo_read as _odoo_read_pdf,
                odoo_search as _odoo_search_pdf,
            )
            _id_arg_value = None
            try:
                _id_arg_value = int(args.get(_id_arg_name)) if args.get(_id_arg_name) is not None else None
            except (TypeError, ValueError):
                _id_arg_value = None
            if _id_arg_value is None or _id_arg_value <= 0:
                # Try resolving by name (also support 'invoice_name' for
                # get_invoice_pdf since the schema documents it).
                name_arg = (
                    args.get("invoice_name")
                    or args.get("refund_name")
                    or args.get("retention_name")
                    or args.get("name")
                    or args.get(_id_arg_name)  # LLM puede pasar el name como invoice_id (string)
                )
                if isinstance(name_arg, str) and name_arg.strip():
                    name_clean = name_arg.strip()
                    # Build domain — scope to current partner when available
                    # to avoid cross-partner resolution.
                    _name_domain = [["name", "=", name_clean]]
                    if expected_partner_id:
                        _name_domain.append(["partner_id", "=", expected_partner_id])
                    try:
                        _hits = _odoo_search_pdf(
                            *creds, "account.move", _name_domain,
                            fields=["id"], limit=2,
                        )
                        if len(_hits) == 1:
                            _id_arg_value = int(_hits[0]["id"])
                        elif len(_hits) > 1:
                            return json.dumps({
                                "success": False,
                                "error_code": "ambiguous_name",
                                "error_detail": (
                                    f"Hay varios documentos con name='{name_clean}'. "
                                    "Pasa el ID exacto."
                                ),
                            }, ensure_ascii=False)
                    except Exception as _exc:  # noqa: BLE001
                        logger.warning(
                            "%s: name->id resolve failed name=%r: %s",
                            tool_name, name_clean, _exc,
                        )
            if _id_arg_value is None or _id_arg_value <= 0:
                return json.dumps({
                    "success": False,
                    "error_code": f"invalid_{_id_arg_name}",
                    "error_detail": (
                        f"{_id_arg_name} requerido (entero positivo) o "
                        f"name resoluble (ej 'FACV/2025/4897')."
                    ),
                }, ensure_ascii=False)
            try:
                _rows = _odoo_read_pdf(
                    *creds, "account.move", [_id_arg_value],
                    ["id", "name", "type", "partner_id"],
                )
                if _rows:
                    _move_row = _rows[0]
                    _pid = _move_row.get("partner_id")
                    if isinstance(_pid, (list, tuple)) and _pid:
                        _otp_partner_id = int(_pid[0])
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "%s: partner lookup failed id=%s: %s",
                    tool_name, _id_arg_value, _exc,
                )
            if not _move_row:
                return json.dumps({
                    "success": False,
                    "error_code": "move_not_found",
                    "error_detail": (
                        f"No existe el account.move id={_id_arg_value}."
                    ),
                }, ensure_ascii=False)

        # Cross-partner enforcement (same as iter80 read tools).
        if (
            expected_partner_id
            and _otp_partner_id
            and expected_partner_id != _otp_partner_id
        ):
            return json.dumps({
                "success": False,
                "error_code": "cross_partner_invoice",
                "error_detail": (
                    "El recurso pertenece a otro cliente. Identifica al "
                    "cliente correcto antes de generar su PDF."
                ),
            }, ensure_ascii=False)

        if _otp_partner_id:
            has_session = await _otp_check_session(
                _supa_url, _supa_key, _tenant, _otp_partner_id, _channel,
            )
            if not has_session:
                return (
                    "VERIFICACION REQUERIDA: El cliente no ha verificado "
                    "su identidad. Para enviar el PDF oficial, primero usa "
                    "request_otp para enviar un codigo al correo del "
                    "cliente y luego verify_otp cuando te de el codigo."
                )

        # ── Dispatch per-tool ───────────────────────────────────────────
        import os as _os_pdf
        import secrets as _secrets_pdf
        from datetime import date as _date_pdf

        _odoo_url = tc["url"]
        _odoo_db = tc["db"]
        _odoo_user = tc["user"]
        _odoo_password = tc["password"]
        # Public base URL for the niko-served PDF. ``niko_public_url``
        # may be overridden via env (NIKO_PUBLIC_URL); strip trailing /.
        _public_base = (
            _os_pdf.environ.get("NIKO_PUBLIC_URL")
            or getattr(_settings_pdf, "niko_public_url", "")
            or ""
        ).rstrip("/")

        if tool_name == "get_customer_statement_pdf":
            try:
                pdf_bytes, _ = fetch_odoo_report_pdf(
                    tenant_id=_tenant,
                    odoo_url=_odoo_url, odoo_db=_odoo_db,
                    odoo_user=_odoo_user, odoo_password=_odoo_password,
                    report_xmlid="tecno_l10n_ec_payment.report_account_balance",
                    res_ids=[_otp_partner_id],
                )
            except OdooReportError as exc:
                return json.dumps({
                    "success": False,
                    "error_code": exc.code,
                    "error_detail": exc.detail,
                }, ensure_ascii=False)

            out_dir = "/files/statements"
            try:
                _os_pdf.makedirs(out_dir, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({
                    "success": False,
                    "error_code": "mkdir_failed",
                    "error_detail": str(exc),
                }, ensure_ascii=False)
            token = _secrets_pdf.token_urlsafe(6)
            today_iso = _date_pdf.today().isoformat()
            fname = f"estado_cuenta_partner{_otp_partner_id}_{today_iso}_{token}.pdf"
            path = f"{out_dir}/{fname}"
            try:
                with open(path, "wb") as fh:
                    fh.write(pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({
                    "success": False,
                    "error_code": "save_failed",
                    "error_detail": str(exc),
                }, ensure_ascii=False)

            # ETA iter 81 — compute expiry timestamp (7d retention) and
            # a localised "generated_at" for the display_text.
            from datetime import datetime as _dt_pdf, timedelta as _td_pdf, timezone as _tz_pdf
            now_utc = _dt_pdf.now(_tz_pdf.utc)
            expires_at = (now_utc + _td_pdf(days=7)).isoformat()
            # America/Guayaquil = UTC-5 fixed (no DST), per memory
            # "Mostrar timestamps en zona Ecuador".
            try:
                from zoneinfo import ZoneInfo as _ZI
                local_dt = now_utc.astimezone(_ZI("America/Guayaquil"))
            except Exception:  # noqa: BLE001
                local_dt = now_utc - _td_pdf(hours=5)
            generated_at_local = local_dt.strftime("%d-%b %H:%M")

            pdf_url = (
                f"{_public_base}/files/statements/{fname}"
                if _public_base else f"/files/statements/{fname}"
            )
            result = {
                "success": True,
                "partner_id": _otp_partner_id,
                "report_xmlid": "tecno_l10n_ec_payment.report_account_balance",
                "pdf_filename": fname,
                "pdf_size_bytes": len(pdf_bytes),
                "pdf_url": pdf_url,
                "generated_at": now_utc.isoformat(),
                "generated_at_local": generated_at_local,
                "expires_at": expires_at,
                "days_back": int(args.get("days_back") or 90),
            }
            try:
                from mcp_odoo.formatters.whatsapp_invoices import format_statement_pdf
                if request_channel in _CHAT_CHANNELS:
                    result["display_text"] = format_statement_pdf(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("statement_pdf formatter failed: %s", exc)
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        # ── Invoice / NC / Retention PDF (all three share the save logic)
        # Pick the right Odoo xmlid based on tool + move type.
        move_type = (_move_row or {}).get("type") or ""
        move_name = (_move_row or {}).get("name") or ""
        if tool_name == "get_invoice_pdf":
            # Auto-detect NC: type='out_refund' uses the NC report. Any
            # other supported invoice type uses the factura report. We
            # refuse anything that is not a customer invoice / refund.
            if move_type == "out_refund":
                xmlid = "l10n_ec_sri_ece.report_nota_credito_electronica"
                kind_label = "credit_note"
            elif move_type in ("out_invoice",):
                xmlid = "l10n_ec_sri_ece.report_factura_electronica"
                kind_label = "invoice"
            else:
                return json.dumps({
                    "success": False,
                    "error_code": "not_an_invoice",
                    "error_detail": (
                        f"El account.move {_id_arg_value} es type={move_type!r}; "
                        "esta tool solo aplica a facturas (out_invoice) y "
                        "notas de credito (out_refund)."
                    ),
                }, ensure_ascii=False)
        elif tool_name == "get_credit_note_pdf":
            if move_type != "out_refund":
                return json.dumps({
                    "success": False,
                    "error_code": "not_a_credit_note",
                    "error_detail": (
                        f"El account.move {_id_arg_value} es type={move_type!r}; "
                        "get_credit_note_pdf solo aplica a notas de credito "
                        "(out_refund). Para facturas normales usa get_invoice_pdf."
                    ),
                }, ensure_ascii=False)
            xmlid = "l10n_ec_sri_ece.report_nota_credito_electronica"
            kind_label = "credit_note"
        else:  # get_retention_pdf
            # Retentions are stored as account.move entries (type='entry').
            # We don't enforce the type strictly — Odoo will refuse to
            # render the report if the move is not a retention, and
            # ``fetch_odoo_report_pdf`` surfaces that as
            # ``odoo_report_not_pdf``. Surfacing the typed error is more
            # informative than a custom guess here.
            xmlid = "l10n_ec_sri_ece.report_retencion_electronica"
            kind_label = "retention"

        # Iter 81c: el RIDE PDF ya existe como ir.attachment en Odoo
        # Tecnosmart — se genera tras la autorizacion SRI (no via el
        # template QWEB l10n_ec_sri_ece.report_factura_electronica, que
        # tiene un bug latente con `date_due` vs `invoice_date_due` y
        # devuelve HTTP 500 al renderizar on-the-fly). Owner-evidencia
        # (2026-05-23): la factura 404936 tiene attachment 487786
        # 'FACV_2025_4897.pdf' (40 KB, application/pdf) + el XML SRI
        # firmado. Estrategia: leer el attachment existente PRIMERO, si
        # no hay caer al render QWEB como fallback. Esto entrega siempre
        # el RIDE oficial firmado por el SRI.
        pdf_bytes: bytes | None = None
        attachment_source: str | None = None
        try:
            from mcp_odoo.tools.generic import (
                odoo_search as _odoo_search_att,
                odoo_read as _odoo_read_att,
            )
            atts = _odoo_search_att(
                *creds, "ir.attachment",
                [
                    ["res_model", "=", "account.move"],
                    ["res_id", "=", _id_arg_value],
                    ["mimetype", "=", "application/pdf"],
                ],
                fields=["id", "name", "file_size"],
                limit=10,
                order="create_date desc",
            )
            if atts:
                # Preferir el que matchee el `name` del move (FACV...)
                # sobre cualquier otro PDF (ej. attachments de respaldo).
                expected_stem = (move_name or "").replace("/", "_").lower()
                best = None
                for a in atts:
                    n = (a.get("name") or "").lower()
                    if expected_stem and expected_stem in n and n.endswith(".pdf"):
                        best = a
                        break
                if best is None:
                    best = atts[0]
                full = _odoo_read_att(
                    *creds, "ir.attachment", [int(best["id"])], ["datas", "name"],
                )
                if full and full[0].get("datas"):
                    import base64 as _b64_att
                    raw_b64 = full[0]["datas"]
                    if isinstance(raw_b64, bytes):
                        raw_b64 = raw_b64.decode("ascii", errors="ignore")
                    pdf_bytes = _b64_att.b64decode(raw_b64)
                    attachment_source = f"ir.attachment[{best['id']}]"
                    logger.info(
                        "%s: using ir.attachment id=%s name=%s (%d bytes) "
                        "instead of QWEB render",
                        tool_name, best["id"], full[0].get("name"),
                        len(pdf_bytes),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s: attachment lookup failed for move=%s: %s",
                tool_name, _id_arg_value, exc,
            )

        if pdf_bytes is None:
            # Fallback al QWEB render (puede fallar con date_due bug en
            # el template Tecnosmart — el error se propaga al cliente).
            try:
                pdf_bytes, _ = fetch_odoo_report_pdf(
                    tenant_id=_tenant,
                    odoo_url=_odoo_url, odoo_db=_odoo_db,
                    odoo_user=_odoo_user, odoo_password=_odoo_password,
                    report_xmlid=xmlid,
                    res_ids=[_id_arg_value],
                )
                attachment_source = f"qweb:{xmlid}"
            except OdooReportError as exc:
                return json.dumps({
                    "success": False,
                    "error_code": exc.code,
                    "error_detail": (
                        f"{exc.detail} (Tip: la factura {move_name or _id_arg_value} "
                        f"no tiene PDF guardado como ir.attachment y el "
                        f"template QWEB falla. Esto pasa cuando la factura "
                        f"aun no fue autorizada por el SRI. Verifica el "
                        f"estado SRI en Odoo.)"
                    ),
                }, ensure_ascii=False)

        out_dir = "/files/rides"
        try:
            _os_pdf.makedirs(out_dir, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({
                "success": False,
                "error_code": "mkdir_failed",
                "error_detail": str(exc),
            }, ensure_ascii=False)

        token = _secrets_pdf.token_urlsafe(6)
        # account.move.name can be "FACV/2025/4897" or "VENTA122584";
        # sanitize for filesystem.
        safe_name = move_name.replace("/", "_").replace(" ", "_") or f"move{_id_arg_value}"
        fname = f"{kind_label}_{safe_name}_{token}.pdf"
        path = f"{out_dir}/{fname}"
        try:
            with open(path, "wb") as fh:
                fh.write(pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({
                "success": False,
                "error_code": "save_failed",
                "error_detail": str(exc),
            }, ensure_ascii=False)

        from datetime import datetime as _dt_pdf, timedelta as _td_pdf, timezone as _tz_pdf
        now_utc = _dt_pdf.now(_tz_pdf.utc)
        expires_at = (now_utc + _td_pdf(days=7)).isoformat()
        pdf_url = (
            f"{_public_base}/files/rides/{fname}"
            if _public_base else f"/files/rides/{fname}"
        )
        result = {
            "success": True,
            "kind": kind_label,
            "report_xmlid": xmlid,
            "pdf_filename": fname,
            "pdf_size_bytes": len(pdf_bytes),
            "pdf_url": pdf_url,
            "expires_at": expires_at,
            # Iter 81c: source = ir.attachment[id] cuando viene del PDF
            # ya guardado tras autorizacion SRI, qweb:<xmlid> cuando se
            # tuvo que renderizar (fallback). Util para debug y para que
            # el LLM sepa si el PDF es el RIDE oficial firmado SRI o no.
            "source": attachment_source,
        }
        if kind_label == "retention":
            result["retention_id"] = _id_arg_value
            result["retention_name"] = move_name or None
        else:
            result["invoice_id"] = _id_arg_value
            result["invoice_name"] = move_name or None
            result["invoice_type"] = move_type or None

        try:
            from mcp_odoo.formatters.whatsapp_invoices import (
                format_credit_note_pdf,
                format_invoice_pdf,
                format_retention_pdf,
            )
            if request_channel in _CHAT_CHANNELS:
                if kind_label == "credit_note":
                    result["display_text"] = format_credit_note_pdf(result)
                elif kind_label == "retention":
                    result["display_text"] = format_retention_pdf(result)
                else:
                    result["display_text"] = format_invoice_pdf(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ride_pdf formatter failed: %s", exc)

        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "lookup_sri":
        return await _lookup_sri(args["cedula_ruc"])

    if tool_name == "create_partner":
        return await _create_partner(creds, args)

    if tool_name == "update_partner":
        return await _update_partner(creds, args)

    if tool_name == "get_customer_credit_status":
        from mcp_odoo.tools.sales import odoo_get_customer_credit_status
        result = odoo_get_customer_credit_status(*creds, args["partner_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_order_delivery_status":
        from mcp_odoo.tools.sales import odoo_get_order_delivery_status
        result = odoo_get_order_delivery_status(
            *creds,
            order_id=args.get("order_id"),
            order_name=args.get("order_name"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_my_sales_summary":
        from mcp_odoo.tools.sales import odoo_get_my_sales_summary
        result = odoo_get_my_sales_summary(
            *creds,
            period=args.get("period", "month"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_pricelist_price":
        from mcp_odoo.tools.sales import odoo_get_pricelist_price
        result = odoo_get_pricelist_price(
            *creds,
            partner_id=args["partner_id"],
            template_id=args["template_id"],
            quantity=args.get("quantity", 1),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_quotation_margin":
        from mcp_odoo.tools.sales import odoo_get_quotation_margin
        result = odoo_get_quotation_margin(
            *creds,
            order_id=args.get("order_id"),
            order_name=args.get("order_name"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_customer_purchase_history":
        from mcp_odoo.tools.sales import odoo_get_customer_purchase_history
        result = odoo_get_customer_purchase_history(
            *creds,
            partner_id=args["partner_id"],
            limit=args.get("limit", 10),
            year=args.get("year"),
        )
        # Iter 79: chat-friendly history summary for WhatsApp/Telegram.
        result = _attach_display_text(
            result, request_channel, kind="purchase_history",
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_stock_by_warehouse":
        from mcp_odoo.tools.sales import odoo_get_stock_by_warehouse
        result = odoo_get_stock_by_warehouse(
            *creds,
            template_id=args.get("template_id"),
            product_code=args.get("product_code"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_pending_quotations":
        from mcp_odoo.tools.sales import odoo_get_pending_quotations
        result = odoo_get_pending_quotations(
            *creds,
            days_old=args.get("days_old", 7),
            include_expired=args.get("include_expired", True),
        )
        # Iter 79: chat-friendly seller follow-up list for WhatsApp/Telegram.
        result = _attach_display_text(
            result, request_channel, kind="pending_quotations",
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "duplicate_quotation":
        from mcp_odoo.tools.sales import odoo_duplicate_quotation
        result = odoo_duplicate_quotation(
            *creds,
            order_id=args.get("order_id"),
            order_name=args.get("order_name"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_my_crm_opportunities":
        from mcp_odoo.tools.sales import odoo_get_my_crm_opportunities
        result = odoo_get_my_crm_opportunities(
            *creds,
            stage=args.get("stage"),
            limit=args.get("limit", 10),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # ----- Appointment / booking tools (salon — Odoo 19 appointment) -----
    if tool_name in (
        "list_services",
        "get_availability",
        "book_appointment",
        "list_my_appointments",
        "cancel_appointment",
    ):
        from mcp_odoo.tools.appointments import (
            book_appointment,
            cancel_appointment,
            get_availability,
            list_my_appointments,
            list_services,
        )
        if tool_name == "list_services":
            result = list_services(*creds, query=args.get("query"))
            result = _attach_display_text(
                result, request_channel, kind="services",
            )
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        if tool_name == "get_availability":
            result = get_availability(
                *creds,
                service=args["service"],
                date_from=args.get("date_from"),
                days_ahead=args.get("days_ahead", 7),
            )
            result = _attach_display_text(
                result, request_channel, kind="availability",
            )
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        if tool_name == "book_appointment":
            _pid = args.get("partner_id")
            result = book_appointment(
                *creds,
                service=args["service"],
                start_local=args["start_local"],
                partner_id=int(_pid) if _pid not in (None, "", 0) else None,
                customer_name=args.get("customer_name"),
                customer_phone=args.get("customer_phone"),
            )
            result = _attach_display_text(
                result, request_channel, kind="booking",
            )
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        if tool_name == "list_my_appointments":
            result = list_my_appointments(
                *creds, partner_id=int(args["partner_id"]),
            )
            result = _attach_display_text(
                result, request_channel, kind="my_appointments",
            )
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

        if tool_name == "cancel_appointment":
            result = cancel_appointment(
                *creds,
                event_id=int(args["event_id"]),
                partner_id=int(args["partner_id"]),
            )
            result = _attach_display_text(
                result, request_channel, kind="cancellation",
            )
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # ----- Billing tools (salon — Odoo 19 account.move + l10n_ec_edi) ----
    if tool_name == "create_invoice":
        from mcp_odoo.tools.billing import create_invoice
        result = create_invoice(
            *creds,
            partner_id=int(args["partner_id"]),
            lines=args.get("lines") or [],
            note=args.get("note"),
        )
        result = _attach_display_text(
            result, request_channel, kind="invoice_created",
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_payment_info":
        from mcp_odoo.tools.billing import get_payment_info
        result = get_payment_info(*creds)
        result = _attach_display_text(
            result, request_channel, kind="payment_info",
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_location_info":
        from mcp_odoo.tools.billing import get_location_info
        result = get_location_info(*creds)
        result = _attach_display_text(
            result, request_channel, kind="location_info",
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    raise ValueError(f"Unknown tool: {tool_name}")


async def _lookup_sri(cedula_ruc: str) -> str:
    """Look up a person/company in Ecuador's SRI by cedula or RUC."""
    import re
    import httpx
    clean = cedula_ruc.strip().replace("-", "").replace(" ", "")

    SRI_BASE = "https://srienlinea.sri.gob.ec/sri-catastro-sujeto-servicio-internet/rest"
    SRI_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://srienlinea.sri.gob.ec/",
    }

    # Detect type
    if re.match(r"^\d{13}$", clean):
        tipo, ruc = "R", clean
    elif re.match(r"^\d{10}$", clean):
        tipo, ruc = "C", clean + "001"
    else:
        return json.dumps({"error": f"'{clean}' no es cedula (10) ni RUC (13) valido"})

    result: dict = {}

    async with httpx.AsyncClient(headers=SRI_HEADERS, timeout=20) as client:
        # 1. Basic person data
        try:
            resp = await client.get(
                f"{SRI_BASE}/Persona/obtenerPorTipoIdentificacion",
                params={"numeroIdentificacion": clean, "tipoIdentificacion": tipo},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("nombreCompleto"):
                    result["nombre"] = data["nombreCompleto"]
                    result["tipo_persona"] = data.get("tipoPersona", "")
        except Exception:
            pass

        # 2. Tax data (all activities)
        try:
            resp = await client.get(
                f"{SRI_BASE}/ConsolidadoContribuyente/obtenerPorNumerosRuc",
                params={"ruc": ruc},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    contrib = data[0]
                    result["ruc"] = ruc
                    result["razon_social"] = contrib.get("razonSocial", "")
                    result["estado"] = contrib.get("estadoContribuyenteRuc", "")
                    result["tipo_contribuyente"] = contrib.get("tipoContribuyente", "")
                    result["regimen"] = contrib.get("regimen", "")
                    result["obligado_contabilidad"] = contrib.get("obligadoLlevarContabilidad", "")
                    result["agente_retencion"] = contrib.get("agenteRetencion", "")
                    result["contribuyente_especial"] = contrib.get("contribuyenteEspecial", "")
                    # All activities from all records
                    actividades = []
                    for c in data:
                        act = c.get("actividadEconomicaPrincipal", "")
                        if act and act not in actividades:
                            actividades.append(act)
                    result["actividades"] = actividades
                    result["actividad_principal"] = actividades[0] if actividades else ""
                    # Dates
                    fechas = contrib.get("informacionFechasContribuyente") or {}
                    result["fecha_inicio"] = fechas.get("fechaInicioActividades", "")
        except Exception:
            pass

        # 3. Establishments (all locations with human-readable addresses)
        try:
            resp = await client.get(
                f"{SRI_BASE}/Establecimiento/consultarPorNumeroRuc",
                params={"numeroRuc": ruc},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    establecimientos = []
                    for e in data:
                        dir_raw = e.get("direccionCompleta", "")
                        # Make address human-readable: "GALAPAGOS / ISABELA / ..." → keep as-is, it's already clear
                        est = {
                            "tipo": {"MAT": "Matriz", "SUC": "Sucursal", "OFI": "Oficina"}.get(
                                e.get("tipoEstablecimiento", ""), e.get("tipoEstablecimiento", "")
                            ),
                            "nombre_comercial": e.get("nombreFantasiaComercial") or "",
                            "direccion": dir_raw,
                            "estado": e.get("estado", ""),
                            "numero": e.get("numeroEstablecimiento", ""),
                        }
                        establecimientos.append(est)
                    result["establecimientos"] = establecimientos
                    # Primary address: first open matriz
                    matriz = next(
                        (e for e in establecimientos if "Matriz" in e["tipo"] and e["estado"] == "ABIERTO"),
                        establecimientos[0],
                    )
                    result["direccion"] = matriz["direccion"]
                    result["nombre_comercial"] = matriz["nombre_comercial"]
        except Exception:
            pass

    if not result:
        return json.dumps({
            "found": False,
            "cedula_ruc": clean,
            "message": f"No se encontro informacion en el SRI para {clean}. "
                       "Puede que no tenga RUC activo.",
        }, ensure_ascii=False)

    result["found"] = True
    result["cedula"] = clean if len(clean) == 10 else clean[:10]
    return json.dumps(result, ensure_ascii=False, indent=2)


async def _create_partner(creds: tuple, args: dict) -> str:
    """Create a new res.partner in Odoo with validation. Two-phase: preview then confirm."""
    from mcp_odoo.tools.generic import odoo_create, odoo_read, odoo_search

    confirmed = args.get("confirmed", False)
    vat = args["vat"].strip().replace("-", "").replace(" ", "")

    # Validate cedula/RUC format
    valid, msg, doc_type = _validate_cedula_or_ruc(vat)
    if not valid:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)

    # Store as-is: cedula stays as cedula, RUC stays as RUC.
    # The agent decides whether to use cedula or RUC based on customer preference.
    vat_store = vat

    # Check if already exists in Odoo (check both cedula and RUC variants)
    ruc_variant = vat + "001" if doc_type == "cedula" else vat
    cedula_variant = vat[:10] if doc_type == "ruc" and len(vat) == 13 else vat
    existing = odoo_search(*creds, "res.partner",
                           ["|", "|", ["vat", "=", vat], ["vat", "=", ruc_variant], ["vat", "=", cedula_variant]],
                           ["id", "name", "vat"], limit=1)
    if existing:
        return json.dumps({
            "success": False,
            "error": "already_exists",
            "partner_id": existing[0]["id"],
            "name": existing[0]["name"],
            "message": f"El cliente ya existe en Odoo: {existing[0]['name']} (ID {existing[0]['id']}). "
                       "Use update_partner para modificar sus datos.",
        }, ensure_ascii=False, indent=2)

    # Verify in SRI that the person exists
    sri_data = None
    try:
        sri_response = await _lookup_sri(vat)
        sri_data = json.loads(sri_response)
    except Exception:
        pass

    if sri_data and sri_data.get("found"):
        # Use SRI name if not provided
        name = args.get("name") or sri_data.get("nombre") or sri_data.get("razon_social") or ""
        street = args.get("street") or sri_data.get("direccion") or ""
    else:
        name = args.get("name", "")
        street = args.get("street", "")

    if not name:
        return json.dumps({
            "success": False,
            "error": "name_required",
            "message": "No se pudo obtener el nombre del SRI. Proporcione el nombre completo.",
        }, ensure_ascii=False)

    # Phase 1: Preview
    if not confirmed:
        preview_data = {
            "nombre": name,
            "cedula_ruc": vat_store,
            "email": args.get("email", ""),
            "telefono": args.get("phone", ""),
            "direccion": street,
            "sri_data": sri_data if sri_data and sri_data.get("found") else None,
        }
        return json.dumps({
            "success": True,
            "preview": True,
            "confirmed": False,
            "datos": preview_data,
            "message": (
                "PREVIEW — muestra estos datos al cliente y pregunta si son correctos. "
                "Si confirma, llama create_partner de nuevo con confirmed=true."
            ),
        }, ensure_ascii=False, indent=2)

    vals = {
        "name": name,
        "vat": vat_store,
        "customer_rank": 1,
    }
    if street:
        vals["street"] = street
    for field in ("email", "phone", "mobile", "city"):
        if args.get(field):
            vals[field] = args[field]

    # Drop fields absent on this Odoo version before writing (e.g. mobile in
    # Odoo 17+ would raise ValueError: Invalid field 'mobile').
    from mcp_odoo.tools.generic import valid_partner_fields
    _valid = set(valid_partner_fields(*creds, list(vals.keys())))
    vals = {k: v for k, v in vals.items() if k in _valid}

    try:
        partner_id = odoo_create(*creds, "res.partner", vals)
        partner = odoo_read(*creds, "res.partner", [partner_id],
                           ["id", "name", "vat", "email", "phone", "street", "city"])[0]

        # Sync new partner to RAG (partner_embeddings)
        try:
            from mcp_odoo.config import settings as _cfg
            tenant_id = creds[0]
            tenant_slug = await _get_tenant_slug(tenant_id)
            supa_url = _cfg.supabase_url or "http://supabase-kong:8000"
            supa_key = _cfg.supabase_service_key or _cfg.supabase_jwt_secret

            p_name = partner.get("name", "")
            p_vat = partner.get("vat", "")
            p_email = partner.get("email", "")
            p_phone = partner.get("phone", "")
            content = f"{p_name} | RUC/Cedula: {p_vat} | Email: {p_email} | Telefono: {p_phone}"
            metadata = {"name": p_name, "vat": p_vat, "email": p_email, "phone": p_phone}

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{supa_url}/rest/v1/partner_embeddings",
                    headers=_postgrest_headers(supa_key,
                                               schema=f"tenant_{tenant_slug}",
                                               prefer="return=minimal"),
                    json={"odoo_id": partner_id, "name": p_name, "vat": p_vat,
                          "content": content, "metadata": metadata},
                )
            logger.info("Synced new partner %s to RAG", partner_id)
        except Exception as sync_err:
            logger.warning("Failed to sync new partner to RAG: %s", sync_err)

        return json.dumps({
            "success": True,
            "partner_id": partner_id,
            "name": partner["name"],
            "vat": partner["vat"],
            "email": partner.get("email") or "",
            "phone": partner.get("phone") or "",
            "sri_data": sri_data if sri_data and sri_data.get("found") else None,
            "message": f"Cliente {partner['name']} creado exitosamente (ID {partner_id}).",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "message": "Error al crear el cliente en Odoo.",
        }, ensure_ascii=False)


async def _update_partner(creds: tuple, args: dict) -> str:
    """Update an existing res.partner in Odoo and sync RAG embeddings.

    Two-phase: without confirmed=true, returns preview only.
    With confirmed=true, executes the change.
    """
    from mcp_odoo.tools.generic import odoo_write, odoo_read, valid_partner_fields
    from mcp_odoo.config import settings
    import httpx

    partner_id = args["partner_id"]
    confirmed = args.get("confirmed", False)
    vals = {}
    for field in ("name", "email", "phone", "mobile", "street", "city"):
        if args.get(field):
            vals[field] = args[field]

    if not vals:
        return json.dumps({"success": False, "message": "No hay campos para actualizar."})

    # Drop fields absent on this Odoo version (e.g. mobile in Odoo 17+) so
    # neither the read nor the write raises Invalid field.
    _valid_vals = set(valid_partner_fields(*creds, list(vals.keys())))
    vals = {k: v for k, v in vals.items() if k in _valid_vals}
    if not vals:
        return json.dumps({"success": False, "message": "No hay campos para actualizar."})

    # Phase 1: Preview — read current data and show what would change
    if not confirmed:
        try:
            _preview_fields = valid_partner_fields(
                *creds, ["name", "vat", "email", "phone", "mobile", "street", "city"],
            )
            current = odoo_read(*creds, "res.partner", [partner_id], _preview_fields)[0]
            changes = []
            for field, new_val in vals.items():
                old_val = current.get(field, "") or ""
                if str(old_val) != str(new_val):
                    changes.append({"campo": field, "actual": str(old_val), "nuevo": str(new_val)})

            if not changes:
                return json.dumps({
                    "success": False,
                    "preview": True,
                    "message": "Los datos proporcionados son iguales a los actuales. No hay cambios.",
                    "datos_actuales": {k: current.get(k, "") for k in vals},
                }, ensure_ascii=False, indent=2)

            return json.dumps({
                "success": True,
                "preview": True,
                "confirmed": False,
                "partner_id": partner_id,
                "nombre": current.get("name", ""),
                "cambios": changes,
                "message": (
                    "PREVIEW — muestra estos cambios al cliente y pide confirmacion. "
                    "Si confirma, llama update_partner de nuevo con confirmed=true y los mismos datos."
                ),
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    try:
        odoo_write(*creds, "res.partner", [partner_id], vals)
        _post_fields = valid_partner_fields(
            *creds, ["id", "name", "vat", "email", "phone", "mobile", "street", "city"],
        )
        partner = odoo_read(*creds, "res.partner", [partner_id], _post_fields)[0]

        # Sync updated data to partner_embeddings (RAG)
        try:
            tenant_id = creds[0]
            tenant_slug = await _get_tenant_slug(tenant_id)
            supabase_url = settings.supabase_url or "http://supabase-kong:8000"
            supabase_key = settings.supabase_service_key or settings.supabase_jwt_secret

            # Build updated content and metadata for RAG
            p_name = partner.get("name", "")
            p_vat = partner.get("vat", "")
            p_email = partner.get("email", "")
            p_phone = partner.get("phone", "") or partner.get("mobile", "")
            p_street = partner.get("street", "")
            p_city = partner.get("city", "")

            content = (
                f"{p_name} | RUC/Cedula: {p_vat} | "
                f"Email: {p_email} | Telefono: {p_phone} | "
                f"Ciudad: {p_city} | Direccion: {p_street}"
            )
            metadata = {
                "name": p_name, "vat": p_vat, "email": p_email,
                "phone": p_phone, "city": p_city, "street": p_street,
            }

            async with httpx.AsyncClient(timeout=10) as client:
                await client.patch(
                    f"{supabase_url}/rest/v1/partner_embeddings?odoo_id=eq.{partner_id}",
                    headers=_postgrest_headers(supabase_key,
                                               schema=f"tenant_{tenant_slug}",
                                               prefer="return=minimal"),
                    json={"name": p_name, "vat": p_vat,
                          "content": content, "metadata": metadata},
                )

            # Also update contact_profiles
            async with httpx.AsyncClient(timeout=10) as client:
                await client.patch(
                    f"{supabase_url}/rest/v1/contact_profiles?partner_id=eq.{partner_id}",
                    headers=_postgrest_headers(supabase_key,
                                               schema=f"tenant_{tenant_slug}",
                                               prefer="return=minimal"),
                    json={"name": p_name, "vat": p_vat,
                          "email": p_email, "phone": p_phone},
                )
            logger.info("Synced partner %s to RAG + contact_profiles", partner_id)
        except Exception as sync_err:
            logger.warning("Failed to sync partner to RAG: %s", sync_err)

        # Invalidate old knowledge_facts and insert new ones
        try:
            tenant_id = creds[0]
            tenant_slug = await _get_tenant_slug(tenant_id)
            supabase_url = settings.supabase_url or "http://supabase-kong:8000"
            supabase_key = settings.supabase_service_key or settings.supabase_jwt_secret
            kg_schema = f"tenant_{tenant_slug}"
            kg_entity = f"partner_{partner_id}"
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()

            async with httpx.AsyncClient(timeout=10) as client:
                for field, new_val in vals.items():
                    # Invalidate old fact for this field
                    await client.patch(
                        (f"{supabase_url}/rest/v1/knowledge_facts"
                         f"?entity=eq.{kg_entity}&predicate=eq.{field}"
                         f"&valid_until=is.null"),
                        headers=_postgrest_headers(supabase_key,
                                                    schema=kg_schema,
                                                    prefer="return=minimal"),
                        json={"valid_until": now_iso},
                    )
                    # Insert new fact
                    import uuid as _uuid
                    await client.post(
                        f"{supabase_url}/rest/v1/knowledge_facts",
                        headers=_postgrest_headers(supabase_key,
                                                    schema=kg_schema,
                                                    prefer="return=minimal"),
                        json={
                            "id": str(_uuid.uuid4()),
                            "entity": kg_entity,
                            "predicate": field,
                            "object": str(new_val),
                            "confidence": 1.0,
                            "source": "update_partner",
                            "valid_from": now_iso,
                            "metadata": {},
                        },
                    )
            logger.info("KG facts updated for partner %s: %s", partner_id, list(vals.keys()))
        except Exception as kg_err:
            logger.warning("Failed to update knowledge_facts: %s", kg_err)

        return json.dumps({
            "success": True,
            "partner_id": partner_id,
            "name": partner["name"],
            "vat": partner.get("vat", ""),
            "email": partner.get("email", ""),
            "phone": partner.get("phone", ""),
            "street": partner.get("street", ""),
            "city": partner.get("city", ""),
            "updated_fields": list(vals.keys()),
            "message": f"Datos de {partner['name']} actualizados: {', '.join(vals.keys())}.",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "message": "Error al actualizar el cliente en Odoo.",
        }, ensure_ascii=False)


async def _identify_customer(creds: tuple, cedula_ruc: str) -> str:
    """Identify a customer by cedula/RUC with validation and balance lookup."""
    from mcp_odoo.tools.generic import odoo_search as _search, valid_partner_fields
    from mcp_odoo.tools.sales import odoo_check_balance

    clean = cedula_ruc.strip().replace("-", "").replace(" ", "")
    valid, msg, doc_type = _validate_cedula_or_ruc(clean)
    if not valid:
        return json.dumps({"found": False, "error": msg}, ensure_ascii=False)

    # Drop fields absent on this Odoo version (e.g. mobile in Odoo 17+).
    _partner_fields = valid_partner_fields(
        *creds,
        ["name", "vat", "email", "phone", "mobile", "street", "city",
         "credit_limit", "customer_rank", "supplier_rank", "property_payment_term_id"],
    )

    # Search by VAT in Odoo
    partners = _search(
        *creds,
        "res.partner",
        [["vat", "=", clean]],
        fields=_partner_fields,
        limit=1,
    )

    if not partners:
        # Try with RUC if cedula was given (some partners stored with 001 suffix)
        if doc_type == "cedula":
            partners = _search(
                *creds,
                "res.partner",
                [["vat", "=", clean + "001"]],
                fields=_partner_fields,
                limit=1,
            )

    if not partners:
        return json.dumps({
            "found": False,
            "cedula_ruc": clean,
            "doc_type": doc_type,
            "message": (
                f"No encontre un cliente con {doc_type} {clean} en el sistema. "
                "Si desea, puedo crear un nuevo cliente. Necesitaria: nombre completo, "
                "correo electronico y direccion."
            ),
        }, ensure_ascii=False, indent=2)

    partner = partners[0]
    partner_id = partner["id"]

    # Get balance info
    balance = odoo_check_balance(*creds, partner_id)

    is_customer = partner.get("customer_rank", 0) > 0
    is_supplier = partner.get("supplier_rank", 0) > 0
    partner_type = "cliente" if is_customer else ("proveedor" if is_supplier else "contacto")

    result = {
        "found": True,
        "partner_id": partner_id,
        "name": partner.get("name", ""),
        "vat": partner.get("vat", ""),
        "email": partner.get("email") or "",
        "phone": partner.get("phone") or partner.get("mobile") or "",
        "address": f"{partner.get('street', '') or ''}, {partner.get('city', '') or ''}".strip(", "),
        "credit_limit": "[Requiere verificacion OTP]",
        "total_due": "[Requiere verificacion OTP - use request_otp]",
        "total_overdue": "[Requiere verificacion OTP - use request_otp]",
        "partner_type": partner_type,
        "can_sell_to": True,  # Any partner can receive a quotation in Odoo
        "payment_term": partner.get("property_payment_term_id", [None, ""])[1] if isinstance(partner.get("property_payment_term_id"), list) else "",
    }
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


async def _get_tenant_config(request: Request) -> dict:
    """Get tenant Odoo config from JWT or environment defaults."""
    from mcp_odoo.config import settings
    from mcp_odoo.auth.encryption import decrypt_credentials

    # Try JWT from Authorization header
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            from mcp_odoo.auth.jwt_validator import decode_tenant_jwt
            tenant = decode_tenant_jwt(auth_header.split(" ", 1)[1])
            tenant_id = tenant["tenant_id"]

            # Load from Supabase
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{settings.supabase_url}/rest/v1/tenants",
                    params={"id": f"eq.{tenant_id}", "select": "odoo_url,odoo_db,odoo_user,odoo_password_encrypted"},
                    headers={
                        "apikey": settings.supabase_service_key or settings.supabase_jwt_secret,
                        "Authorization": f"Bearer {settings.supabase_service_key or settings.supabase_jwt_secret}",
                        "Accept-Profile": "public",
                    },
                )
            if resp.status_code == 200 and resp.json():
                data = resp.json()[0]
                return {
                    "tenant_id": tenant_id,
                    "url": data["odoo_url"],
                    "db": data["odoo_db"],
                    "user": data["odoo_user"],
                    "password": decrypt_credentials(data["odoo_password_encrypted"]),
                }
        except Exception as e:
            logger.warning(f"JWT auth failed, falling back to env: {e}")

    # Fallback: use X-Tenant-ID header to lookup config from Supabase
    tenant_id = request.headers.get("x-tenant-id", "default")
    if tenant_id and tenant_id != "default":
        try:
            import httpx
            supabase_key = settings.supabase_service_key or settings.supabase_jwt_secret
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{settings.supabase_url}/rest/v1/tenants",
                    params={"id": f"eq.{tenant_id}", "select": "odoo_url,odoo_db,odoo_user,odoo_password_encrypted"},
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Accept-Profile": "public",
                    },
                    timeout=10,
                )
            if resp.status_code == 200 and resp.json():
                data = resp.json()[0]
                return {
                    "tenant_id": tenant_id,
                    "url": data["odoo_url"],
                    "db": data["odoo_db"],
                    "user": data["odoo_user"],
                    "password": decrypt_credentials(data["odoo_password_encrypted"]),
                }
        except Exception as e:
            logger.warning(f"X-Tenant-ID lookup failed: {e}")

    # Final fallback to environment
    return {
        "tenant_id": tenant_id,
        "url": getattr(settings, 'odoo_url', ""),
        "db": getattr(settings, 'odoo_db', ""),
        "user": getattr(settings, 'odoo_user', ""),
        "password": getattr(settings, 'odoo_password', ""),
    }


# ---------------------------------------------------------------------------
# Live product cache (in-process TTL dict).
#
# Pattern: Tidio Lyro / Sierra AI / MCP best practices.
# RAG (pgvector) is for SEMANTIC DISCOVERY only — given a fuzzy query,
# return candidate odoo_ids. ALL display data (price, stock, code,
# description) comes LIVE from Odoo via XML-RPC, NOT from the embedding
# metadata. The embedding snapshot would go stale within minutes for
# any volatile field, and the industry consensus is "never embed
# volatile numbers, always read live from the system of record."
#
# To absorb burst traffic (10 users asking about the same SKU in the
# same minute), we cache the per-product Odoo read for a short TTL.
# 30s matches the MCP best-practices guidance ("stock prices expire
# in seconds") and is small enough that staleness in chat is not
# observable.
#
# Cache key: (tenant_id, product_id) → {qty_available, list_price, ...}
# ---------------------------------------------------------------------------
import time as _t

# --- _card auto-attach ----------------------------------------------------
#
# Sale-order tools that mutate or read state should return a `_card`
# envelope so the orchestrator (niko/orchestrator.py:_extract_order_card_from_messages)
# can render an inline OrderCard with Editar/PDF/Confirmar buttons.
# Only `create_quotation`, `add_to_quotation` and `get_quotation` build it
# themselves; the rest of the quotation tools (Sprint C) just return
# {success, order_id, ...} and the LLM ends up rendering a markdown
# table that Telegram does NOT render correctly.
#
# Solution: after every successful quotation-tool call, if the response
# JSON has `order_id` but no `_card`, do ONE read of sale.order and
# inject `_card` so the OrderCard always shows.
_QUOTATION_TOOLS_NEEDING_CARD = {
    "get_latest_quotation",
    "get_active_quotation",
    "get_quotation",
    "get_quotation_state_summary",
    "update_quotation_line",
    "remove_quotation_line",
    "add_quotation_line",
    "change_quotation_customer",
    "apply_global_discount",
    "set_quotation_header",
    "recalculate_quotation",
    "transition_quotation",
    "sign_quotation",
}


# Tools that take an `order_id` and may receive a name-like string
# from the LLM ("VENTA122172", "122172"). For each call we attempt to
# coerce the value to an int — if that fails we look the order up by
# name in Odoo and rewrite args["order_id"] in-place.
_TOOLS_WITH_ORDER_ID = {
    "get_quotation",
    "render_quotation_pdf",
    "send_quotation",
    "confirm_quotation",
    "add_to_quotation",
    "update_quotation_line",
    "remove_quotation_line",
    "add_quotation_line",
    "set_quotation_header",
    "apply_global_discount",
    "change_quotation_customer",
    "recalculate_quotation",
    "get_quotation_state_summary",
    "transition_quotation",
    "sign_quotation",
    "niko_send_sign_request",
    "create_payphone_link",
}


async def _resolve_order_id_alias(request: Request, tool_name: str, args: dict) -> dict | None:
    """Validate ``args["order_id"]`` is a REAL Odoo order_id.

    Strict policy (operator decision, May 2026): the LLM MUST pass the
    numeric primary key of sale.order (e.g. 113603), never the human
    name (\"VENTA122172\") nor its digit suffix (\"122172\").

    Why strict: the orchestrator already injects ``order_id=N`` for the
    active quotation in the system prompt's "IDs identificados" block,
    so the LLM has the real id every turn. Allowing fallbacks like
    \"VENTA{n}\" or fuzzy ilike was unsafe — collisions between
    VENTA122172 and VENTA1221720 could mutate the wrong order. Better
    to reject than to risk wrong-record mutation on a destructive tool.

    Returns:
      * ``None`` when args are valid (passthrough).
      * ``dict`` error envelope when the id is not a real existing
        sale.order — the dispatcher serializes it as ``isError=true``
        so the LLM has to fetch the real id (via get_latest_quotation
        / get_quotation_state_summary) before retrying.
    """
    if tool_name not in _TOOLS_WITH_ORDER_ID:
        return None
    raw = args.get("order_id")
    if raw is None:
        return None

    try:
        tc = await _get_tenant_config(request)
        from mcp_odoo.tools.generic import odoo_search
    except Exception as e:
        logger.debug("order_id resolver skipped (tc): %s", e)
        return None

    creds = (tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"])

    def _exists_as_id(candidate: int) -> bool:
        try:
            rows = odoo_search(
                *creds, "sale.order", [["id", "=", candidate]],
                fields=["id"], limit=1,
            )
            return bool(rows)
        except Exception:
            return False

    def _reject(detail: str) -> dict:
        return {
            "success": False,
            "error_code": "order_id_must_be_real_int",
            "error_detail": detail,
            "hint": (
                "order_id debe ser el id NUMERICO real de sale.order "
                "(ej. 113603), NO el name humano (VENTA122172) ni su "
                "sufijo (122172). Si solo conoces el name, llama "
                "find_quotation_by_name(name='VENTA...') que devuelve "
                "el order_id correcto. Tambien aparece en el bloque "
                "'IDs identificados' del system prompt cuando hay "
                "cotizacion activa."
            ),
            "received": raw,
        }

    # Accept only ints / numeric strings that map to an existing record.
    if isinstance(raw, int) and raw > 0:
        if _exists_as_id(raw):
            return None
        return _reject(
            f"order_id={raw} no existe en sale.order. Quizas confundiste "
            f"el name (VENTA{raw}) con el order_id real."
        )

    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            n = int(s)
            if _exists_as_id(n):
                args["order_id"] = n
                return None
            return _reject(
                f"order_id={s!r} no existe en sale.order. Quizas confundiste "
                f"el name (VENTA{s}) con el order_id real."
            )
        return _reject(
            f"order_id={raw!r} no es entero. Pasa el id numerico real, "
            f"NO el name."
        )

    return _reject(
        f"order_id tiene tipo {type(raw).__name__}, esperaba un entero "
        f"positivo."
    )


def _validate_args_against_schema(tool_name: str, args: dict) -> dict | None:
    """Validate ``args`` against the inputSchema declared in MCP_TOOLS.

    Catches the most common LLM mistakes BEFORE invoking the tool:
      * missing required field
      * required field has wrong primitive type (object vs array, etc.)

    Returns ``None`` when args are OK. Returns a dict ``{success: False,
    error_code, error_detail, missing, expected_schema}`` when invalid.
    The dict is serialised to the LLM as the tool result so it can
    self-correct on the next AIMessage with the exact structure the
    tool expects.
    """
    if not isinstance(args, dict):
        return {
            "success": False,
            "error_code": "invalid_args_type",
            "error_detail": (
                f"`arguments` must be a JSON object, got {type(args).__name__}."
            ),
        }

    spec = next((t for t in MCP_TOOLS if t.get("name") == tool_name), None)
    if not spec:
        return None  # unknown tool — let the dispatcher handle it
    schema = (spec.get("inputSchema") or {})
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing: list[str] = []
    type_errors: list[dict] = []

    for field_name in required:
        if field_name not in args:
            missing.append(field_name)
            continue
        # Light type check on common primitives (object/array). Allow None
        # for optional fields; here we are checking required ones so they
        # must be present and the right shape.
        prop = properties.get(field_name) or {}
        expected_type = prop.get("type")
        value = args[field_name]
        if expected_type == "array" and not isinstance(value, list):
            type_errors.append({
                "field": field_name,
                "expected": "array",
                "got": type(value).__name__,
            })
        elif expected_type == "object" and not isinstance(value, dict):
            type_errors.append({
                "field": field_name,
                "expected": "object",
                "got": type(value).__name__,
            })
        elif expected_type == "integer" and not isinstance(value, int):
            type_errors.append({
                "field": field_name,
                "expected": "integer",
                "got": type(value).__name__,
            })

    if not missing and not type_errors:
        return None  # all good

    detail_lines: list[str] = []
    if missing:
        detail_lines.append(
            "Missing required field(s): " + ", ".join(repr(m) for m in missing)
            + "."
        )
    if type_errors:
        for te in type_errors:
            detail_lines.append(
                f"Field {te['field']!r} expected type {te['expected']!r}, "
                f"got {te['got']!r}."
            )
    detail_lines.append(
        "Re-call the tool with the EXACT structure shown in `expected_schema`."
    )

    return {
        "success": False,
        "error_code": "invalid_arguments",
        "error_detail": " ".join(detail_lines),
        "missing": missing,
        "type_errors": type_errors,
        "expected_schema": {
            "type": schema.get("type", "object"),
            "properties": properties,
            "required": required,
        },
    }


def _maybe_attach_card(tc: dict, tool_name: str, text: str) -> str:
    """Inject a `_card` envelope when a quotation tool returned `order_id`
    but didn't build one. Idempotent — leaves text untouched on any failure.
    """
    if tool_name not in _QUOTATION_TOOLS_NEEDING_CARD:
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text
    if not data.get("success"):
        return text
    order_id = data.get("order_id")
    if not order_id or data.get("_card"):
        return text
    try:
        creds = (tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"])
        from mcp_odoo.tools.sales import _card_for_order  # local import: avoid circular
        card = _card_for_order(*creds, int(order_id))
        if card:
            data["_card"] = card
            return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logger.debug("attach_card skipped for %s: %s", tool_name, e)
    return text


_PRODUCT_CACHE_TTL_SECONDS = 30
_product_cache: dict[tuple[str, int], tuple[float, dict]] = {}


def _product_cache_get(tenant_id: str, product_id: int) -> dict | None:
    entry = _product_cache.get((tenant_id, product_id))
    if not entry:
        return None
    expires_at, data = entry
    if expires_at < _t.time():
        _product_cache.pop((tenant_id, product_id), None)
        return None
    return data


def _product_cache_set(tenant_id: str, product_id: int, data: dict) -> None:
    _product_cache[(tenant_id, product_id)] = (
        _t.time() + _PRODUCT_CACHE_TTL_SECONDS,
        data,
    )
    # Best-effort eviction: if cache grows past 5000 entries (e.g.,
    # accumulated stale rows), drop the oldest 1000. Cheap O(n) sweep,
    # runs at most once per cache write past the threshold.
    if len(_product_cache) > 5000:
        cutoff = _t.time()
        expired = [k for k, (exp, _) in _product_cache.items() if exp < cutoff]
        for k in expired[:1000]:
            _product_cache.pop(k, None)


# ---------------------------------------------------------------------------
# Per-query result cache (TTL 60s).
#
# When the user asks "dame procesadores", we run the full hybrid search
# (vector + ILIKE) once, rank the results in-stock-first, and cache the
# WHOLE ranked list for 60 seconds. Subsequent paginated calls
# (offset=10, offset=20, ...) hit the cache and return slices instantly
# without re-running the search OR re-fetching from Odoo.
#
# Key: (tenant_id, normalized_query). Value: (expires_at, ranked_list).
# ---------------------------------------------------------------------------
_QUERY_CACHE_TTL = 60
_query_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _query_cache_get(tenant_id: str, query_key: str) -> list[dict] | None:
    entry = _query_cache.get((tenant_id, query_key))
    if not entry:
        return None
    expires_at, data = entry
    if expires_at < _t.time():
        _query_cache.pop((tenant_id, query_key), None)
        return None
    return data


def _query_cache_set(tenant_id: str, query_key: str, ranked: list[dict]) -> None:
    _query_cache[(tenant_id, query_key)] = (_t.time() + _QUERY_CACHE_TTL, ranked)
    if len(_query_cache) > 200:
        cutoff = _t.time()
        for k in [k for k, (exp, _) in _query_cache.items() if exp < cutoff][:50]:
            _query_cache.pop(k, None)


# Max number of product ids to read from Odoo in a single XML-RPC call.
# Larger batches trip Odoo's HTTP / nginx 502 timeouts on big catalogs
# (Carmen Solis hit it at ~167 ids), so we chunk on the client side.
_LIVE_FETCH_CHUNK = 30


async def _fetch_products_live(
    tenant_id: str, product_ids: list[int]
) -> dict[int, dict]:
    """Read display data LIVE from Odoo for the given product ids.

    Returns {odoo_id: {name, code, price, qty, virtual, categ, description}}.
    Uses an in-process TTL cache so repeat queries within 30s for the
    same SKU don't pound Odoo. If the tenant has no Odoo wired (shell
    tenant), returns an empty dict and the caller should fall back to
    the snapshot data. Large id lists are chunked into batches of
    _LIVE_FETCH_CHUNK so we don't 502 Odoo.
    """
    if not product_ids:
        return {}

    # Partition into cache hits + misses
    out: dict[int, dict] = {}
    misses: list[int] = []
    for pid in product_ids:
        cached = _product_cache_get(tenant_id, pid)
        if cached is not None:
            out[pid] = cached
        else:
            misses.append(pid)

    if not misses:
        return out

    tc = await _get_tenant_config_by_id(tenant_id)
    if not tc or not tc.get("url"):
        return out  # tenant without Odoo — caller falls back to snapshot

    # Read from product.template (NOT product.product) because the
    # embedding indexes templates — that's what Odoo 13's UI shows
    # in its kanban list view (action=287&model=product.template).
    # product.product holds variants and IDs do not match across the
    # two tables for catalogs without variants.
    from mcp_odoo.tools.generic import odoo_search as _read

    # Chunk the misses to avoid 502s on large catalogs.
    all_rows: list[dict] = []
    for i in range(0, len(misses), _LIVE_FETCH_CHUNK):
        chunk = misses[i : i + _LIVE_FETCH_CHUNK]
        try:
            rows = _read(
                tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"],
                "product.template",
                [["id", "in", chunk]],
                fields=[
                    "id", "name", "default_code", "list_price", "standard_price",
                    "qty_available", "virtual_available", "uom_id",
                    "categ_id", "description_sale", "barcode", "active",
                ],
                limit=len(chunk),
            )
            all_rows.extend(rows or [])
        except Exception as exc:
            logger.warning(
                f"_fetch_products_live chunk {i}-{i + len(chunk)} failed: {exc}"
            )
            # Continue with remaining chunks — partial results are
            # better than no results.

    for row in all_rows:
        pid = row.get("id")
        if not isinstance(pid, int):
            continue
        data = {
            "id": pid,
            "name": row.get("name") or "",
            "code": row.get("default_code") or "",
            "price": row.get("list_price") or 0,
            "cost": row.get("standard_price") or 0,
            "qty": row.get("qty_available") or 0,
            "virtual": row.get("virtual_available") or 0,
            "uom": (row.get("uom_id") or [None, ""])[1] if isinstance(row.get("uom_id"), list) else "",
            "category": (row.get("categ_id") or [None, ""])[1] if isinstance(row.get("categ_id"), list) else "",
            "description": row.get("description_sale") or "",
            "barcode": row.get("barcode") or "",
            "active": bool(row.get("active", True)),
        }
        _product_cache_set(tenant_id, pid, data)
        out[pid] = data

    return out


_PRICELIST_CACHE_TTL = 60  # seconds; tenants edit pricelists rarely
_pricelist_cache: dict[tuple[str, int, int], tuple[float, float]] = {}
# key: (tenant_id, partner_id, template_id) → (timestamp, pricelist_price)


def _pricelist_cache_get(tenant_id: str, partner_id: int, template_id: int) -> float | None:
    key = (tenant_id, partner_id, template_id)
    hit = _pricelist_cache.get(key)
    if not hit:
        return None
    ts, price = hit
    if _t.time() - ts > _PRICELIST_CACHE_TTL:
        _pricelist_cache.pop(key, None)
        return None
    return price


def _pricelist_cache_set(tenant_id: str, partner_id: int, template_id: int, price: float) -> None:
    _pricelist_cache[(tenant_id, partner_id, template_id)] = (_t.time(), price)


async def _apply_pricelist_to_live(
    tenant_id: str, partner_id: int, live: dict[int, dict]
) -> None:
    """Rewrite ``live[tid]["price"]`` to the partner's pricelist price.

    B1 (prod 2026-05-16, sim Mario v3): clientes B2B con pricelist veían
    list_price en search_products ($206.31) pero create_quotation aplicaba
    su pricelist ($229.99) — diferencia de hasta 12% que confunde al
    cliente y rompe la confianza ("¿me cobran más de lo que ofrecen?").

    Pre-condición: ``live`` ya viene de ``_fetch_products_live`` (precios
    públicos). Esta función lo muta in-place: cada entry conserva
    ``list_price`` original para auditoría y sobrescribe ``price`` con el
    valor de la pricelist del partner. Si la pricelist no afecta a un
    producto (no hay regla específica), Odoo devuelve el list_price y la
    sobrescritura es no-op.

    Implementación: una sola llamada ``read`` a ``product.template`` con
    context ``{pricelist, partner}`` — Odoo 13 computa
    ``product.template.price`` server-side aplicando todas las reglas de
    la pricelist en un round-trip. Probamos antes con
    ``pricelist.get_product_price`` pero ese método espera browse records
    (no ids) y falla por XML-RPC con AttributeError ``categ_id``.

    Cache 60s por (tenant, partner, template) para amortizar la siguiente
    página de la misma búsqueda.
    """
    if not partner_id or not live:
        return
    tc = await _get_tenant_config_by_id(tenant_id)
    if not tc or not tc.get("url"):
        return  # tenant without Odoo wired — nothing to compute

    from mcp_odoo.tools.generic import odoo_read, odoo_call_method

    # Resolve partner pricelist once.
    try:
        partners = odoo_read(
            tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"],
            "res.partner", [partner_id],
            ["id", "property_product_pricelist"],
        )
    except Exception as exc:
        logger.warning(
            "_apply_pricelist_to_live: partner read failed tenant=%s partner=%s err=%s",
            tenant_id, partner_id, exc,
        )
        return
    if not partners:
        return
    pricelist_raw = partners[0].get("property_product_pricelist")
    if isinstance(pricelist_raw, list) and pricelist_raw:
        pricelist_id = pricelist_raw[0]
    elif isinstance(pricelist_raw, int):
        pricelist_id = pricelist_raw
    else:
        return  # no pricelist configured — keep list_price
    if not pricelist_id:
        return

    # Split live entries into cached vs miss
    cached_prices: dict[int, float] = {}
    misses: list[int] = []
    for tid in live.keys():
        cached = _pricelist_cache_get(tenant_id, partner_id, tid)
        if cached is not None:
            cached_prices[tid] = cached
        else:
            misses.append(tid)

    # Batch-read the misses with pricelist context applied.
    # Iter 76b: usar product.product (variants) en lugar de
    # product.template — el cascade pricelist (base_pricelist_id) solo
    # se computa correctamente sobre variant.price, no template.price.
    # Tecnosmart pricelist 33 PVP tiene 25+ rules con base_pricelist_id
    # por categoría que solo aplican en variant level.
    fresh_prices: dict[int, float] = {}
    if misses:
        try:
            # Resolve template_ids → variant_ids via odoo_search (no
            # odoo_call_method search_read — signature confusion).
            from mcp_odoo.tools.generic import odoo_search as _osr
            variant_rows = _osr(
                tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"],
                "product.product",
                [["product_tmpl_id", "in", misses], ["active", "=", True]],
                ["id", "product_tmpl_id"],
                len(misses) * 4,
            )
            tmpl_to_variant: dict[int, int] = {}
            for vr in variant_rows or []:
                tmpl_field = vr.get("product_tmpl_id")
                if isinstance(tmpl_field, list) and tmpl_field:
                    tmpl_id = tmpl_field[0]
                elif isinstance(tmpl_field, int):
                    tmpl_id = tmpl_field
                else:
                    continue
                # First variant wins (default). Skip if already mapped.
                if tmpl_id not in tmpl_to_variant:
                    tmpl_to_variant[tmpl_id] = vr["id"]

            variant_ids = list(tmpl_to_variant.values())
            if not variant_ids:
                raise ValueError("no variants resolved")

            rows = odoo_call_method(
                tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"],
                "product.product", "read",
                variant_ids,
                [["id", "price"]],
                {"context": {"pricelist": pricelist_id, "partner": partner_id}},
            )
            variant_to_price = {r["id"]: r.get("price")
                                for r in (rows or []) if isinstance(r, dict)}
            for tmpl_id, var_id in tmpl_to_variant.items():
                price_val = variant_to_price.get(var_id)
                if price_val is None:
                    continue
                try:
                    p = float(price_val)
                except (TypeError, ValueError):
                    continue
                if p <= 0:
                    continue
                fresh_prices[tmpl_id] = p
                _pricelist_cache_set(tenant_id, partner_id, tmpl_id, p)
        except Exception as exc:
            logger.warning(
                "_apply_pricelist_to_live: batch read failed pricelist=%s n=%d err=%s",
                pricelist_id, len(misses), exc,
            )

    # Apply rewritten prices in-place
    for tid, data in live.items():
        if not isinstance(data, dict):
            continue
        new_price = cached_prices.get(tid) or fresh_prices.get(tid)
        if new_price is None:
            continue
        data["list_price"] = data.get("price", 0)
        data["price"] = new_price


async def _get_tenant_config_by_id(tenant_id: str) -> dict | None:
    """Look up Odoo creds for a tenant_id without needing a Request.

    Used by `_rag_search` to enrich product results with live qty_available
    from Odoo. Returns the same shape as `_get_tenant_config()` or None if
    the tenant has no Odoo wired.
    """
    from mcp_odoo.config import settings
    from mcp_odoo.auth.encryption import decrypt_credentials
    import httpx

    if not tenant_id or tenant_id == "default":
        return None
    supabase_key = settings.supabase_service_key or settings.supabase_jwt_secret
    if not supabase_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/tenants",
                params={
                    "id": f"eq.{tenant_id}",
                    "select": "odoo_url,odoo_db,odoo_user,odoo_password_encrypted",
                },
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Accept-Profile": "public",
                },
            )
        if resp.status_code != 200 or not resp.json():
            return None
        data = resp.json()[0]
        if not (data.get("odoo_url") and data.get("odoo_password_encrypted")):
            return None
        return {
            "tenant_id": tenant_id,
            "url": data["odoo_url"],
            "db": data["odoo_db"],
            "user": data["odoo_user"],
            "password": decrypt_credentials(data["odoo_password_encrypted"]),
        }
    except Exception as exc:
        logger.warning(f"_get_tenant_config_by_id({tenant_id}) failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Tenant slug cache: tenant_id -> slug
# ---------------------------------------------------------------------------
_tenant_slug_cache: dict[str, str] = {}


async def _get_tenant_slug(tenant_id: str) -> str:
    """Resolve tenant_id (UUID) to slug (e.g. 'tecnosmart'). Cached."""
    if tenant_id in _tenant_slug_cache:
        return _tenant_slug_cache[tenant_id]

    from mcp_odoo.config import settings
    import httpx

    supabase_url = settings.supabase_url or "https://supabase.galapagos.tech"
    supabase_key = settings.supabase_service_key or settings.supabase_jwt_secret

    # Try Supabase REST first. If it's down (DNS / connect / 5xx), fall
    # through to the hard-coded fallback dict so a Supabase outage does
    # not kill product search — qwen2.5 specifically poisons its session
    # if a tool call returns ConnectError mid-conversation.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{supabase_url}/rest/v1/tenants",
                params={"id": f"eq.{tenant_id}", "select": "slug"},
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Accept-Profile": "public",
                },
            )
            if resp.status_code == 200 and resp.json():
                slug = resp.json()[0]["slug"]
                _tenant_slug_cache[tenant_id] = slug
                return slug
    except Exception as exc:
        print(f"[mcp_odoo] _get_tenant_slug: supabase unreachable ({exc!r}), using fallback", flush=True)

    # Fallback: derive from known defaults
    _fallback = {
        "6b898738-41f9-48ce-a3e4-ad8a9ad9af77": "tecnosmart",
        "66461837-e408-4279-a1a6-1977acf2241f": "csolish",
        "f8a2bf44-afce-497d-8562-bfe33b9023d1": "tecnosmart",
    }
    slug = _fallback.get(tenant_id, "tecnosmart")
    _tenant_slug_cache[tenant_id] = slug
    return slug


# ---------------------------------------------------------------------------
# Product intent classification (audit 2026-05-13)
# ---------------------------------------------------------------------------
#
# When the customer asks for a "laptop", the semantic + ILIKE search
# also pulls in "TECLADO PARA LAPTOP HP", "CABLE DE SEGURIDAD PARA
# LAPTOP", "KIT DE LIMPIEZA PARA LAPTOP" etc. We classify the query
# into a coarse intent so we can drop accessories/parts when the
# customer obviously wants a *complete* product. The classification
# is purely keyword-based — fast, deterministic, no ML dependency.
#
# Each kind maps to:
#   * a set of category hints (Odoo categ_id substrings)
#   * a set of code prefixes that count as the "right" kind
#   * a set of disqualifying tokens (when present in the candidate
#     row's name, exclude regardless of category)
#
# The filter is BEST-EFFORT: if it would zero out the result list, we
# silently return the unfiltered ranking so the customer still sees
# something.

_INTENT_RULES: list[dict] = [
    # Computing platforms — main draw
    {
        "kind": "laptop_complete",
        "query_any": ["laptop", "notebook", "portatil", "portátil", "ultrabook"],
        "query_exclude_part": [
            "teclado", "pantalla", "bateria", "batería", "cargador",
            "fuente", "repuesto", "carcasa", "bisagra", "memoria",
            "ram", "ssd", "disco",
        ],
        "category_hints": ["laptop", "notebook", "portable", "computadores y portables"],
        "code_prefixes": ["LAP"],
        "name_excludes": [
            "teclado", "pantalla", "bateria", "batería", "cargador",
            "carcasa", "bisagra", "cable de seguridad", "kit de limpieza",
            "candado",
        ],
    },
    {
        "kind": "desktop_complete",
        "query_any": ["desktop", "escritorio", "all in one", "all-in-one", "torre", "workstation"],
        "query_exclude_part": ["repuesto", "case", "cooler", "fuente"],
        "category_hints": ["desktop", "escritorio", "all in one", "computadores"],
        "code_prefixes": ["PCD", "PCS"],
        "name_excludes": ["fuente para", "carcasa", "kit de"],
    },
    {
        "kind": "monitor",
        "query_any": ["monitor", "pantalla", "display"],
        "query_exclude_part": ["cable", "soporte", "brazo", "repuesto", "limpieza"],
        "category_hints": ["monitor", "tv"],
        "code_prefixes": ["MON"],
        "name_excludes": ["cable", "soporte", "brazo", "kit de limpieza"],
    },
    {
        "kind": "printer",
        "query_any": ["impresora", "multifuncional", "printer"],
        "query_exclude_part": ["tinta", "toner", "cartucho", "papel", "repuesto"],
        "category_hints": ["impresora", "printer"],
        "code_prefixes": ["IMP"],
        "name_excludes": ["tinta", "toner", "cartucho", "papel"],
    },
    # PC chassis. Audit 2026-05-30 (Tecnosmart PC-build): a "case/
    # gabinete" search pulled in "CASE COMBO ... TECLADO - MOUSE -
    # PARLANTES" starter bundles (peripherals, NOT a PC chassis) because
    # their name starts with "CASE". 51 of 776 CAS-prefixed products are
    # such bundles. The LLM, mid PC-build, rewrote those combos into
    # fabricated gaming cases ("CASE GAMER XTECH XT-100", keeping the real
    # code/price) — inventing products and showing combo prices on a fake
    # chassis. We drop the bundles so only real chassis reach the model;
    # with clean candidates the model grounds verbatim (proven: the same
    # tenant renders real monitors/products correctly). ``query_exclude_
    # part`` keeps the bundle visible when the customer EXPLICITLY asks for
    # a "case combo / con teclado y mouse".
    {
        "kind": "case_chassis",
        "query_any": ["gabinete", "chasis", "chassis", "case"],
        "query_exclude_part": ["combo", "teclado", "mouse", "parlante"],
        "category_hints": ["gabinete", "chasis", "case", "torre", "tower"],
        "code_prefixes": ["CAS"],
        "name_excludes": ["combo", "teclado", "mouse", "parlante"],
    },
    {
        "kind": "mouse",
        "query_any": ["mouse", "raton", "ratón"],
        "query_exclude_part": ["mousepad", "pad para mouse"],
        "category_hints": ["mouse"],
        "code_prefixes": ["MOU"],
        "name_excludes": ["mousepad"],
    },
    {
        "kind": "keyboard",
        "query_any": ["teclado", "keyboard"],
        "query_exclude_part": ["para laptop", "para notebook", "repuesto"],
        "category_hints": ["teclado", "keyboard"],
        "code_prefixes": ["TEC"],
        "name_excludes": ["para laptop", "para notebook"],
    },
]


def _classify_product_intent(query: str) -> dict | None:
    """Classify a product query into one of :data:`_INTENT_RULES`.

    Returns the matching rule dict or ``None`` when no rule fires
    (generic query like "algo para mi oficina"). Multiple matches
    resolve to the FIRST one in the list (laptop_complete wins over
    monitor when both keywords appear, matching the usual user phrasing
    "una laptop con monitor").
    """
    q = (query or "").lower()
    for rule in _INTENT_RULES:
        if not any(kw in q for kw in rule["query_any"]):
            continue
        # Demote to the "part/accessory" variant when the user query
        # mentions parts (e.g. "teclado para laptop" → keyboard, not
        # laptop_complete). We model this by returning None for those
        # cases — the caller falls back to unfiltered semantic search,
        # which is the right answer because the semantic rank will
        # actually surface the parts the customer wants.
        if any(part in q for part in rule.get("query_exclude_part", [])):
            return None
        return rule
    return None


def _apply_kind_filter(
    ranked: list[dict], intent_rule: dict
) -> tuple[list[dict], int]:
    """Drop rows that don't match ``intent_rule``.

    Returns ``(filtered, dropped_count)``. A row passes when ANY of
    these are true:
      * its ``code`` starts with one of the rule's ``code_prefixes``
      * its live ``category`` contains one of ``category_hints``
    A row is REJECTED when its ``name`` contains one of
    ``name_excludes`` (regardless of the positive matches above) —
    that catches "TECLADO PARA LAPTOP" that lives in a laptop-adjacent
    Odoo category but is clearly an accessory.

    When the filter would leave 0 rows, the caller is expected to
    fall back to the unfiltered ranking (handled in ``_rag_search``).
    """
    prefixes = tuple(p.upper() for p in intent_rule.get("code_prefixes", []))
    hints = [h.lower() for h in intent_rule.get("category_hints", [])]
    excludes = [e.lower() for e in intent_rule.get("name_excludes", [])]
    kept: list[dict] = []
    dropped = 0
    for r in ranked:
        live = r.get("_live") or {}
        name_low = (live.get("name") or r.get("name") or "").lower()
        code = (live.get("code") or r.get("code") or "").upper()
        category_low = (live.get("category") or "").lower()
        if excludes and any(x in name_low for x in excludes):
            dropped += 1
            continue
        passes = False
        if prefixes and code.startswith(prefixes):
            passes = True
        elif hints and any(h in category_low for h in hints):
            passes = True
        if passes:
            kept.append(r)
        else:
            dropped += 1
    return kept, dropped


async def _direct_by_code(tenant_id: str, code: str) -> list[dict] | None:
    """Resolve a product by ``default_code`` straight from Odoo.

    Used by :func:`_rag_search` as the code-direct shortcut: when the
    query looks like a product code we skip the embedding + ILIKE merge
    and read the live row from Odoo by ``default_code``. Returns a
    list-of-one in the same shape :func:`_format_ranked_page` expects
    (``[{"odoo_id": int, "_live": dict, ...}]``), or ``None`` when no
    such code exists in the tenant's catalog (caller falls back to the
    semantic search).
    """
    tc = await _get_tenant_config_by_id(tenant_id)
    if not tc or not tc.get("url"):
        return None
    from mcp_odoo.tools.generic import odoo_search as _read
    rows = _read(
        tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"],
        "product.template",
        [["default_code", "=ilike", code]],
        fields=[
            "id", "name", "default_code", "list_price", "standard_price",
            "qty_available", "virtual_available", "categ_id",
            "description_sale", "active",
        ],
        limit=1,
    )
    if not rows:
        return None
    p = rows[0]
    if not p.get("active", True):
        return None
    live = {
        "name": p.get("name", ""),
        "code": p.get("default_code", "") or "",
        "price": float(p.get("list_price") or 0),
        "cost": float(p.get("standard_price") or 0),
        "qty": float(p.get("qty_available") or 0),
        "virtual": float(p.get("virtual_available") or 0),
        "category": p.get("categ_id", [None, ""])[1]
                    if isinstance(p.get("categ_id"), list) else "",
        "description": p.get("description_sale") or "",
    }
    return [{
        "odoo_id": p["id"],
        "code": live["code"],
        "name": live["name"],
        "_live": live,
        "metadata": {"odoo_id": p["id"], "code": live["code"]},
    }]


async def _rag_search(
    query: str,
    top_k: int = 10,
    offset: int = 0,
    tenant_id: str = "",
    price_min: float | None = None,
    price_max: float | None = None,
    category_path: str | None = None,
    partner_id: int | None = None,
) -> str:
    """Hybrid product search: pgvector RRF + ILIKE merge + live Odoo + paginated.

    1. Look up the cached ranked list for (tenant, query). If hit and
       not expired, slice it [offset:offset+top_k] and return immediately.
    2. On cache miss: run the full hybrid search (vector RRF + ILIKE),
       live-fetch all candidates from Odoo, re-rank in-stock-first,
       cache the WHOLE ranked list for 60s, then return the requested
       page.
    3. There is NO hard cap on top_k or candidate pool size — the user
       can ask for as many as the catalog has. The pagination is for
       UX (Telegram messages have a length limit) and to absorb burst
       traffic.

    Price filtering
    ---------------
    ``price_min`` / ``price_max`` (USD, inclusive) filter the ranked
    list AT PRESENTATION TIME — they are NOT part of the cache key, so
    the same ranked list serves any budget. This matters because the
    LLM was caught presenting products above the customer's stated
    budget (B3 bug, 2026-05-12). When the customer says "máximo 500",
    we must not show items priced over 500. Items with unknown live
    price (``_live is None`` — tenants without Odoo wired) are kept
    only when no price filter is requested; otherwise we drop them
    because we can't certify they fit the budget.
    """
    # Code-direct shortcut. When the query looks like a product code
    # (3-letter prefix + 3-4 digits, e.g. "RAM0413", "LAP0030", "MOU0154"),
    # bypass the embedding + ILIKE merge entirely and read live from
    # Odoo by default_code. Reasons:
    #   * Embedding for a code string is wasteful (no semantic content).
    #   * The ILIKE fallback would still match accessories named
    #     "TECLADO PARA LAPTOP HP MOU0154" — same noise we are trying
    #     to avoid.
    #   * Odoo XMLRPC read by code is O(1) — fastest path.
    #
    # If the code is malformed or doesn't exist in Odoo, we fall through
    # to the regular semantic search so the customer still gets options.
    import re as _re_local
    _code_match = _re_local.fullmatch(
        r"\s*([A-Z]{2,4}\d{3,5})\s*", query, _re_local.IGNORECASE,
    )
    if _code_match:
        _code = _code_match.group(1).upper()
        try:
            _direct = await _direct_by_code(tenant_id, _code)
            if _direct:
                if partner_id:
                    _direct_live = {
                        r["odoo_id"]: dict(r["_live"])
                        for r in _direct
                        if isinstance(r.get("odoo_id"), int)
                        and isinstance(r.get("_live"), dict)
                    }
                    if _direct_live:
                        await _apply_pricelist_to_live(
                            tenant_id, partner_id, _direct_live,
                        )
                        for r in _direct:
                            oid = r.get("odoo_id")
                            if oid in _direct_live:
                                r["_live"] = _direct_live[oid]
                return _format_ranked_page(
                    _direct, top_k=1, offset=0,
                    price_min=price_min, price_max=price_max,
                    category_path=category_path,
                    query=query,
                )
        except Exception as _direct_exc:
            print(f"[RAG] code-direct lookup failed: {_direct_exc}", flush=True)
        # Falls through to semantic search if code lookup returned nothing.

    # Cache key isolates per-partner pricing so a B2B customer's pricelist
    # never leaks into the anonymous result set (and vice versa).
    cache_key = query.strip().lower()
    if partner_id:
        cache_key = f"{cache_key}|p{partner_id}"
    cached_ranked = _query_cache_get(tenant_id, cache_key)
    if cached_ranked is not None:
        return _format_ranked_page(
            cached_ranked, top_k, offset,
            price_min=price_min, price_max=price_max,
            category_path=category_path,
            query=query,
        )

    # Cache miss — fetch a wide candidate pool. We aim for at least
    # 5x the page size or 100, whichever is bigger. This is just a
    # hint to the RAG: the actual size depends on what the catalog
    # has. There's no hard cap.
    candidate_k = max(top_k * 5, 100)
    from mcp_odoo.config import settings
    import httpx

    ollama_url = settings.ollama_url or "https://llama.galapagos.tech"
    supabase_url = settings.supabase_url or "https://supabase.galapagos.tech"
    supabase_key = settings.supabase_service_key or settings.supabase_jwt_secret
    tenant_id = tenant_id or settings.default_tenant_id or "6b898738-41f9-48ce-a3e4-ad8a9ad9af77"
    embedding_model = settings.embedding_model or "bge-m3"

    if not supabase_key:
        return "Error: SUPABASE_SERVICE_KEY not configured"

    tenant_slug = await _get_tenant_slug(tenant_id)

    async with httpx.AsyncClient(timeout=30) as client:
        # Generate embedding for semantic component
        embedding = None
        try:
            resp = await client.post(f"{ollama_url}/api/embed",
                                     json={"model": embedding_model, "input": [query]})
            resp.raise_for_status()
            embedding = resp.json()["embeddings"][0]
        except Exception as e:
            print(f"[RAG] Embedding failed: {e}", flush=True)

        results = []

        if embedding:
            # RRF hybrid search: semantic + keyword fused via search_tenant_products
            try:
                resp = await client.post(
                    f"{supabase_url}/rest/v1/rpc/search_tenant_products",
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Accept-Profile": "public",
                        "Content-Profile": "public",
                        "Content-Type": "application/json",
                        "Content-Profile": "public",
                    },
                    json={
                        "p_tenant_slug": tenant_slug,
                        "query_embedding": embedding,
                        "query_text": query,
                        "match_count": candidate_k,
                    },
                )
                resp.raise_for_status()
                results = resp.json()
            except Exception as e:
                print(f"[RAG] RRF search failed: {e}, trying fallback", flush=True)

        # ── Hybrid: ALSO run a direct ILIKE on name/code/content. ────
        # Reason: pgvector + RRF is great for fuzzy queries ("laptop
        # para oficina") but it FILTERS by cosine similarity threshold
        # so a literal query like "procesador" — which appears in 242
        # product names — only returns the top ~5 strict matches and
        # the bot looks broken vs Odoo's UI which shows them all.
        # ILIKE on the name expands the candidate pool to include any
        # product whose name contains the query verbatim. We dedupe
        # by odoo_id and prefer the vector-ranked entry for items
        # that appear in BOTH (since vector ranking is more precise
        # when it does match).
        seen_ids: set = set()
        merged: list[dict] = []
        for r in results:
            oid = r.get("odoo_id") or (r.get("metadata") or {}).get("odoo_id")
            if oid in seen_ids:
                continue
            if isinstance(oid, int):
                seen_ids.add(oid)
            merged.append(r)

        # Only run ILIKE fallback if vector search returned few results.
        # When embeddings work well (≥5 results), ILIKE just adds noise.
        skip_ilike = len(merged) >= 5
        try:
            if skip_ilike:
                raise Exception("skip — enough vector results")

            search_terms = query.replace(" ", "%")
            resp = await client.get(
                f"{supabase_url}/rest/v1/product_embeddings",
                params={
                    "or": f"(name.ilike.%{search_terms}%,content.ilike.%{search_terms}%)",
                    "select": "odoo_id,name,code,content,metadata",
                    "limit": str(candidate_k),
                    "order": "name",
                },
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Accept-Profile": f"tenant_{tenant_slug}",
                },
            )
            if resp.status_code == 200:
                for r in resp.json():
                    oid = r.get("odoo_id")
                    if oid in seen_ids:
                        continue
                    if isinstance(oid, int):
                        seen_ids.add(oid)
                    merged.append(r)
        except Exception as e:
            print(f"[RAG] ILIKE merge failed: {e}", flush=True)
        results = merged

    if not results:
        return "No encontre productos que coincidan con esa busqueda. Intenta con otros terminos."

    # ── Tidio Lyro / Sierra AI hybrid pattern ─────────────────────────
    # The pgvector RAG above told us WHICH products are relevant. We
    # now read ALL display data (name, price, qty, code, category, etc.)
    # LIVE from Odoo via XML-RPC. The embedding metadata is NOT used for
    # display anymore — it would go stale within minutes for any
    # volatile field. Industry consensus (Tidio Lyro for Shopify, Sierra
    # for retail, MCP best practices): "never embed volatile numbers,
    # always read live from the system of record".
    #
    # _fetch_products_live transparently caches each product for 30s
    # to absorb burst traffic when many users ask about the same SKU.
    odoo_ids: list[int] = []
    for r in results:
        oid = r.get("odoo_id") or (r.get("metadata") or {}).get("odoo_id")
        if isinstance(oid, int):
            odoo_ids.append(oid)

    live = await _fetch_products_live(tenant_id, odoo_ids)

    # ── B1: rewrite prices to the partner's pricelist ────────────────
    # When a partner_id is in scope (customer is identified), the prices
    # shown to them must match what create_quotation will charge. Without
    # this rewrite the catalog shows list_price and the quotation
    # silently applies the B2B pricelist — a $200 vs $230 divergence we
    # caught in sim Mario v3 (RUC 0919258160001, partner_id=33).
    #
    # ``_fetch_products_live`` returns references to the per-product
    # cache; mutating in place would leak B2B prices into anonymous
    # callers. We clone the dicts before rewriting.
    # Iter 76 fix B: si NO hay partner_id identificado, usar el
    # consumidor_final_partner_id como fallback para que el catálogo
    # SIEMPRE muestre el precio PVP real (no list_price base).
    # Carlos-LLM v9 reveló: search devolvía $15.30 (list_price) cuando
    # la cotización Odoo creaba con $19.99 (pricelist 33 PVP regla 147
    # formula). Owner confirmó $19.99 = PRECIO CORRECTO para PVP.
    # Fix: aplicar pricelist siempre (con default consumer_final partner)
    # para que el cliente vea desde el inicio el precio al que cotizará.
    _effective_partner_id = partner_id
    if not _effective_partner_id:
        try:
            from mcp_odoo.tools.generic import odoo_search
            tc_for_default = await _get_tenant_config_by_id(tenant_id)
            if tc_for_default:
                params = odoo_search(
                    tc_for_default["tenant_id"], tc_for_default["url"],
                    tc_for_default["db"], tc_for_default["user"],
                    tc_for_default["password"],
                    "ir.config_parameter",
                    [["key", "=", "sale.end_customer_default_id"]],
                    ["value"], 1,
                )
                if params:
                    try:
                        _effective_partner_id = int(params[0]["value"])
                    except (ValueError, TypeError, KeyError):
                        pass
        except Exception as exc:
            logger.warning("search_products default partner lookup failed: %s", exc)
    if _effective_partner_id:
        live = {pid: dict(data) if isinstance(data, dict) else data
                for pid, data in live.items()}
        await _apply_pricelist_to_live(tenant_id, _effective_partner_id, live)

    # ── Re-rank: in-stock first, out-of-stock second ─────────────────
    # The RAG returned candidate_k products by semantic relevance.
    # Within that pool, we prefer products with stock > 0 because no
    # salesman should lead with "we don't have any". Order within each
    # group is preserved (so the most relevant in-stock product comes
    # first, then the second-most-relevant in-stock, etc.).
    in_stock: list[dict] = []
    out_of_stock: list[dict] = []
    unknown_stock: list[dict] = []  # tenants without Odoo wired
    for r in results:
        oid = r.get("odoo_id") or (r.get("metadata") or {}).get("odoo_id")
        live_data = live.get(oid) if isinstance(oid, int) else None
        # Stash live_data on the row so the formatter doesn't have to
        # re-look it up. This is also what gets cached for paginated
        # follow-up requests, so a second call (offset=10) doesn't
        # need to re-fetch from Odoo.
        r["_live"] = live_data
        if live_data is None:
            unknown_stock.append(r)
        elif (live_data.get("qty") or 0) > 0:
            in_stock.append(r)
        else:
            out_of_stock.append(r)
    # FULL ranked list (no slicing here — slicing happens in
    # _format_ranked_page so paginated calls can hit different slices
    # of the same cached list).
    ranked = in_stock + out_of_stock + unknown_stock

    # Intent-aware filter — drop accessories/parts when the customer
    # clearly wants a complete product (e.g. "laptop oficina" filters
    # out "TECLADO PARA LAPTOP HP"). When the filter would leave zero
    # rows we keep the unfiltered list so the customer still gets
    # options (better than "no encontré").
    intent_rule = _classify_product_intent(query)
    intent_kind = intent_rule.get("kind") if intent_rule else None
    if intent_rule:
        filtered, dropped = _apply_kind_filter(ranked, intent_rule)
        if filtered:
            print(
                f"[RAG] intent={intent_kind}: kept {len(filtered)}/"
                f"{len(ranked)} rows (dropped {dropped} accessories/parts)",
                flush=True,
            )
            ranked = filtered
        else:
            print(
                f"[RAG] intent={intent_kind}: filter would zero result "
                f"({len(ranked)} candidates) — keeping unfiltered",
                flush=True,
            )

    _query_cache_set(tenant_id, cache_key, ranked)
    return _format_ranked_page(
        ranked, top_k, offset,
        price_min=price_min, price_max=price_max,
        category_path=category_path,
        intent_kind=intent_kind,
        query=query,
    )


def _format_ranked_page(
    ranked: list[dict],
    top_k: int,
    offset: int,
    price_min: float | None = None,
    price_max: float | None = None,
    category_path: str | None = None,
    intent_kind: str | None = None,
    query: str | None = None,
) -> str:
    """Render a slice [offset:offset+top_k] of a pre-ranked product list.

    Returns a JSON string with the structured shape:

        {
          "header": str,                       # to render at top of message
          "rows": [
            {
              "template_id": int,              # Odoo product.template id (CANONICAL)
              "code": str,                     # default_code, e.g. "CPU0199"
              "line_text": str                 # pre-formatted block ready to copy
            },
            ...
          ],
          "footer": str,
          "instructions_internal": str         # rendering rules for the LLM
        }

    Why JSON instead of plain text:
      - The model needs the canonical `template_id` to call create_quotation
        without hallucinating ids. With plain text it had to infer ids and
        was inventing nonexistent ones (e.g. 100252).
      - `line_text` is precomputed server-side so the model has zero work
        to do — it just concatenates header + line_texts + footer to answer
        the user. Format is guaranteed (no qwen reformatting).
      - `template_id` is in the JSON the model sees but NOT in `line_text`
        so it never appears in the user message unless the model explicitly
        echoes it (which the SOUL.md REGLA forbids).

    The SOUL.md REGLA #1 teaches the model the contract:
      - "Cuando search_products devuelve este JSON, responde al usuario
         concatenando header + line_text de cada row + footer. NUNCA
         muestres template_id. Memoriza la posicion (1,2,3...) → template_id
         para usar despues en create_quotation."

    Price filtering
    ---------------
    When ``price_min`` and/or ``price_max`` are set, the ranked list is
    filtered BEFORE pagination so the LLM never sees items the customer
    cannot afford. Items whose live price is None (tenant without Odoo
    wired) are dropped when ANY price bound is active — we cannot
    certify they meet the budget. The envelope's ``filter_applied``
    flag and an explicit header note tell the LLM the result is
    budget-respecting so the response copy can be adjusted accordingly.
    """
    import json as _json

    price_filter_active = price_min is not None or price_max is not None
    category_filter_active = bool(category_path)
    total_before_filter = len(ranked)

    if category_filter_active:
        # Bug M4: filter ranked rows by Odoo categ_id.complete_name
        # (case-insensitive substring). Reads category from the live
        # row (_live.category) populated by _fetch_products_live.
        # Bug M4-FALLBACK (Niko 2026-05-13): when the LLM passes a
        # category_path like "SSD" but the Odoo category is
        # "Almacenamiento / SSDs Internos", the substring miss zeroed
        # the result list (0 products → bot says "no encontré"). Now
        # we ALSO try token-overlap matching as a fallback, and if the
        # filter would leave 0 rows we silently DROP the filter and
        # return the original ranked list (with a header note that
        # the category filter could not be applied).
        cat_low = category_path.lower()
        cat_tokens = {
            t for t in cat_low.replace("/", " ").replace("-", " ").split()
            if len(t) >= 3
        }
        filtered_cat: list[dict] = []
        for r in ranked:
            live_data = r.get("_live")
            if live_data is None:
                continue
            cat_str = (live_data.get("category") or "").lower()
            if not cat_str:
                continue
            if cat_low in cat_str:
                filtered_cat.append(r)
                continue
            # Fallback: token overlap (e.g. category_path="SSD"
            # matches cat "Almacenamiento / SSDs Internos" via "ssd")
            if cat_tokens and any(
                tok in cat_str for tok in cat_tokens
            ):
                filtered_cat.append(r)
        if filtered_cat:
            ranked = filtered_cat
        # Else: keep original ranked unchanged — better to show
        # products that ranked well by query than to return zero.

    if price_filter_active:
        filtered: list[dict] = []
        for r in ranked:
            live_data = r.get("_live")
            # Drop unknown-price rows under any active filter: we can't
            # guarantee they're in budget so showing them would
            # reintroduce the very bug we're fixing.
            if live_data is None:
                continue
            price = live_data.get("price")
            if price is None:
                continue
            try:
                p = float(price)
            except (TypeError, ValueError):
                continue
            if price_min is not None and p < float(price_min):
                continue
            if price_max is not None and p > float(price_max):
                continue
            filtered.append(r)
        ranked = filtered

    total = len(ranked)
    page = ranked[offset : offset + top_k]
    in_stock_total = sum(
        1 for r in ranked if r.get("_live") and (r["_live"].get("qty") or 0) > 0
    )
    fallback_count = sum(1 for r in page if r.get("_live") is None)

    # Emoji badges 1-10. Beyond that fall back to ASCII brackets.
    BADGES = [
        "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
    ]

    rows: list[dict] = []
    for idx, r in enumerate(page, start=offset + 1):
        live_data = r.get("_live")

        if live_data:
            name = live_data["name"]
            code = live_data["code"]
            price = live_data["price"]
            qty = live_data["qty"]
        else:
            meta = r.get("metadata", {}) or {}
            name = meta.get("name") or r.get("name") or "?"
            code = meta.get("code") or r.get("code") or ""
            price = meta.get("price") or 0
            qty = None

        # Canonical Odoo product.template id (already verified by the rest
        # of the pipeline). Skip rows without an id — they cannot be quoted.
        tmpl_id = r.get("odoo_id") or (r.get("metadata") or {}).get("odoo_id")
        if not isinstance(tmpl_id, int):
            continue

        badge_idx = idx - offset - 1
        badge = BADGES[badge_idx] if 0 <= badge_idx < 10 else f"【{idx}】"

        code_inline = f"  ·  {code}" if code else ""
        title = f"{badge}  {name}{code_inline}"

        if price:
            price_part = f"💰 USD {price:.2f}"
        else:
            price_part = "💰 consultar"

        if qty is None:
            stock_part = ""  # tenant without Odoo wired
        elif qty > 0:
            qty_str = str(int(qty)) if qty == int(qty) else f"{qty:.2f}"
            stock_part = f"   📦 {qty_str} disponibles"
        else:
            stock_part = "   📦 agotado"

        line_text = f"{title}\n      {price_part}{stock_part}"

        # cost is deliberately NOT echoed in the row dict any more.
        # Audit 2026-05-13: the LLM was seeing internal margin data on
        # every product list, response_guard had to sanitize. The few
        # legitimate consumers (seller-asks-for-margin-based-discount in
        # the internal B2B assistant) request cost explicitly via
        # get_product_details. For the customer-facing search_products
        # response there is no use case for `cost` reaching the prompt.
        in_stock_flag = qty is not None and qty > 0
        rows.append({
            "template_id": tmpl_id,
            "code": code or "",
            "name": name or "",
            "price": round(float(price), 2) if price else 0,
            "in_stock": in_stock_flag,
            "line_text": line_text,
        })

    # Header
    showing_from = offset + 1
    showing_to = min(offset + top_k, total)
    has_more = showing_to < total

    # Format a human-friendly budget hint reused in header + footer below.
    def _fmt_budget() -> str:
        if price_min is not None and price_max is not None:
            return f"USD {price_min:.2f}–{price_max:.2f}"
        if price_max is not None:
            return f"hasta USD {price_max:.2f}"
        if price_min is not None:
            return f"desde USD {price_min:.2f}"
        return ""

    budget_str = _fmt_budget()

    if price_filter_active and total == 0:
        # The filter eliminated every candidate. Tell the LLM explicitly
        # so it surfaces "no tengo nada en ese presupuesto" instead of
        # making up products.
        header = (
            f"📦 0 productos en el presupuesto ({budget_str}). "
            f"Se evaluaron {total_before_filter} candidatos pero ninguno "
            f"cumple el limite."
        )
    elif fallback_count and fallback_count == len(page):
        header = (
            f"📦 {total} productos en catalogo (no puedo confirmar "
            f"precio/stock ahora — Odoo no responde):"
        )
    elif in_stock_total == 0 and total > 0:
        header = (
            f"📦 {total} productos relacionados"
            + (f" en presupuesto ({budget_str})" if price_filter_active else "")
            + ", NINGUNO con stock disponible:"
        )
    else:
        budget_note = f" · 💵 presupuesto {budget_str}" if price_filter_active else ""
        header = (
            f"📦 {total} productos ({in_stock_total} con stock){budget_note} · "
            f"Mostrando {showing_from}-{showing_to}"
        )

    # State-aware footer — adapts to (a) empty result, (b) all-out-of-stock,
    # (c) normal in-stock pagination. Audit 2026-05-13: generic footer
    # ("Dime el numero o codigo para cotizar") was misleading when EVERY
    # row was "agotado" because the customer had nothing to pick.
    #
    # Format conventions (matches what WhatsApp/Telegram render as
    # native buttons or selectable options):
    #   * Numbered ACTIONS use plain "1." / "2." / "3." — these are
    #     menus of next steps, not product picks.
    #   * Product PICKS use emoji badges (1️⃣ 2️⃣ ...) inside the rows
    #     themselves (handled by ``line_text``), not the footer.
    #   * "👉" prefixes are kept for non-numbered conversational cues
    #     (single line hints).
    footer_parts: list[str] = []
    if total == 0:
        # Empty result. Already covered by the header but we offer the
        # most useful next steps so the LLM doesn't have to improvise.
        footer_parts.append("¿Qué prefieres hacer?")
        if price_filter_active:
            footer_parts.append("1. Subir el presupuesto para ver más opciones")
            footer_parts.append("2. Revisar otra categoría")
        else:
            footer_parts.append("1. Buscar por otra categoría")
            footer_parts.append("2. Probar con otra marca o palabra clave")
    elif in_stock_total == 0:
        # All candidates are out of stock — the "pick a number" prompt
        # is useless here. Offer numbered next-step ACTIONS instead.
        footer_parts.append("¿Qué prefieres hacer?")
        if price_filter_active:
            footer_parts.append("1. Buscar opciones disponibles sin límite de presupuesto")
            footer_parts.append("2. Revisar otra categoría dentro del mismo presupuesto")
        else:
            footer_parts.append("1. Revisar otra categoría o variante")
            footer_parts.append("2. Que registre tu interés y te avise cuando entre stock")
    else:
        # Normal case — we have at least one in-stock candidate.
        footer_parts.append(
            "👉 Dime el número (ej: 1) o el código (ej: CPU0245) para cotizar."
        )
        if has_more:
            footer_parts.append(
                f"👉 Di \"siguiente\" para ver los próximos "
                f"{min(top_k, total - showing_to)}."
            )
    footer = "\n".join(footer_parts)

    payload = {
        "display_type": "list_data",
        "header": header,
        "rows": rows,
        "footer": footer,
        # Iter 89: echo the original query back so the orchestrator's
        # last_list extractor (niko/agent/orchestrator.py:_extract_
        # last_list_from_messages) can label items by category when a
        # turn issues multiple search_products calls (e.g. "dame
        # teclado, mouse y memoria").
        "query": query or "",
        "intent_kind": intent_kind,  # informational; LLM may use it for copy
        "filter_applied": {
            "price_min": price_min,
            "price_max": price_max,
        } if price_filter_active else None,
        "instructions_internal": (
            # Rendering core ------------------------------------------
            "CONTRATO DE RENDER (obligatorio): escribe header, linea en "
            "blanco, line_text de cada row VERBATIM (sin reformatear, sin "
            "resumir nombres, sin reordenar), linea en blanco, footer "
            "VERBATIM. NUNCA muestres template_id al usuario. Memoriza la "
            "posicion (1,2,3...) -> template_id para create_quotation / "
            "add_to_quotation. NUNCA inventes template_id; usa exactamente "
            "el que aparece en este JSON. "
            # Filtrado por stock (NUEVO 2026-05-13) -------------------
            "FILTRADO POR STOCK: si algunas rows tienen in_stock=true y "
            "otras false, muestra SOLO las rows con in_stock=true en la "
            "primera respuesta — el cliente no necesita ver agotadas si "
            "hay disponibles. El header dice cuantas hay con stock; mantenlo. "
            "Si TODOS los rows tienen in_stock=false, muestra solo 3-5 "
            "como referencia para que el cliente vea modelos existentes; "
            "el footer dinamico ya ofrece la accion correcta (subir "
            "presupuesto / cambiar categoria / registrar interes). "
            # Footer policy -------------------------------------------
            "FOOTER: copia el campo 'footer' EXACTAMENTE como viene. NO "
            "agregues opciones que no esten en el footer (ej. 'Filtrar por "
            "presupuesto especifico', 'Te aviso cuando entre stock', "
            "'Laptops usadas'). Si necesitas una opcion adicional, deja "
            "que el cliente la pida; no la inventes en el menu. "
            # Promesas de follow-up ----------------------------------
            "Promesas tipo 'te aviso cuando entre stock' o 'registro tu "
            "interes': solo puedes hacerlas si PRIMERO llamaste save_memory "
            "con el contenido especifico ('cliente quiere <codigo> cuando "
            "regrese a stock') y/o create_task. Sin tool call previo, NO "
            "hagas la promesa. "
            # Filtros pre-aplicados ----------------------------------
            "Si filter_applied no es null, el resultado YA respeta el "
            "presupuesto. No filtres de nuevo, no avises 'me sale sobre tu "
            "presupuesto'. Si rows esta vacio (total=0), el footer ya "
            "trae opciones numeradas; usalo verbatim. NO inventes "
            "categorias inexistentes (usadas, reacondicionadas, etc). "
            # Formato de menus ---------------------------------------
            "Convencion: emoji 1️⃣ 2️⃣... SOLO para datos (productos, "
            "opciones tangibles). Numeros planos '1.' '2.' para menus "
            "de ACCIONES. El footer ya respeta esa convencion."
        ),
    }
    return _json.dumps(payload, ensure_ascii=False)


async def _rag_search_partners(query: str, top_k: int = 5, tenant_id: str = "") -> str:
    """Hybrid partner search: semantic + keyword via tenant schema."""
    from mcp_odoo.config import settings
    import httpx

    ollama_url = settings.ollama_url or "https://llama.galapagos.tech"
    supabase_url = settings.supabase_url or "https://supabase.galapagos.tech"
    supabase_key = settings.supabase_service_key or settings.supabase_jwt_secret
    tenant_id = tenant_id or settings.default_tenant_id or "6b898738-41f9-48ce-a3e4-ad8a9ad9af77"
    embedding_model = settings.embedding_model or "bge-m3"

    if not supabase_key:
        return "Error: SUPABASE_SERVICE_KEY not configured"

    tenant_slug = await _get_tenant_slug(tenant_id)

    async with httpx.AsyncClient(timeout=30) as client:
        embedding = None
        try:
            resp = await client.post(f"{ollama_url}/api/embed",
                                     json={"model": embedding_model, "input": [query]})
            resp.raise_for_status()
            embedding = resp.json()["embeddings"][0]
        except Exception as e:
            print(f"[RAG] Partner embedding failed: {e}", flush=True)
            return "Error generando embedding para busqueda de contactos."

        # Hybrid search: semantic + keyword via tenant-specific wrapper
        resp = await client.post(
            f"{supabase_url}/rest/v1/rpc/search_tenant_partners",
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Accept-Profile": "public",
                "Content-Profile": "public",
                "Content-Type": "application/json",
                "Content-Profile": "public",
            },
            json={
                "p_tenant_slug": tenant_slug,
                "query_embedding": embedding,
                "query_text": query,
                "match_count": top_k,
            },
        )
        resp.raise_for_status()
        results = resp.json()

    import json as _json_partners

    if not results:
        return _json_partners.dumps(
            {"display_type": "list_data", "results": "No encontre contactos que coincidan con esa busqueda."},
            ensure_ascii=False,
        )

    lines = []
    partners_struct: list[dict] = []
    for r in results:
        meta = r.get("metadata", {}) if isinstance(r.get("metadata"), dict) else {}
        partner_type = meta.get("type", "contacto")
        score = r.get("score", 0)
        # `odoo_id` is the res.partner.id in Odoo — REQUIRED by the
        # mutation tools (create_quotation, etc.) as `partner_id`. Surface
        # it both inline (in the human-readable line) and structured
        # (in the partners array) so the LLM doesn't need a second tool
        # call (identify_customer) just to translate VAT → partner_id.
        odoo_id = r.get("odoo_id") or meta.get("odoo_id")
        name = meta.get("name") or r.get("name") or "?"
        vat = meta.get("vat") or r.get("vat", "")
        city = meta.get("city", "")
        email = meta.get("email", "")
        phone = meta.get("phone", "")
        prefix = f"[partner_id={odoo_id}] " if odoo_id else ""
        lines.append(
            f"- {prefix}{name} | {vat} | {city} | {email} | "
            f"Tel: {phone} | Tipo: {partner_type} (score: {score:.2f})"
        )
        partners_struct.append({
            "partner_id": odoo_id,
            "name": name,
            "vat": vat,
            "city": city,
            "email": email,
            "phone": phone,
            "type": partner_type,
            "score": score,
        })
    summary = f"Encontre {len(results)} contactos:\n" + "\n".join(lines)
    return _json_partners.dumps(
        {
            "display_type": "list_data",
            "results": summary,
            "partners": partners_struct,
        },
        ensure_ascii=False,
    )
