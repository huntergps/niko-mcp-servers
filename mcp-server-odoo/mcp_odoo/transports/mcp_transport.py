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
# Price display formatter
# ---------------------------------------------------------------------------
#
# Tokenizer-safe "USD 1,383.00" renderer lives in mcp_odoo.tools.formatters
# so both this transport and the sales tools share one implementation. See
# that module for the background on why the comma separator matters.

from mcp_odoo.tools.formatters import format_price_display as _format_price_display


# ---------------------------------------------------------------------------
# Ecuador cedula / RUC validation
# ---------------------------------------------------------------------------

def _validate_cedula_ecuador(cedula: str) -> tuple[bool, str]:
    """Validate Ecuadorian cedula (10 digits, modulo-10 check)."""
    if not cedula or not cedula.isdigit() or len(cedula) != 10:
        return False, "La cedula debe tener exactamente 10 digitos numericos."

    province = int(cedula[:2])
    if province < 1 or province > 24:
        return False, f"Codigo de provincia invalido: {province}."

    third = int(cedula[2])
    if third >= 6:
        return False, "Tercer digito invalido para cedula de persona natural."

    coefficients = [2, 1, 2, 1, 2, 1, 2, 1, 2]
    total = 0
    for i in range(9):
        val = int(cedula[i]) * coefficients[i]
        if val >= 10:
            val -= 9
        total += val

    check = (10 - (total % 10)) % 10
    if check != int(cedula[9]):
        return False, "Digito verificador invalido."

    return True, "OK"


def _validate_ruc_ecuador(ruc: str) -> tuple[bool, str]:
    """Validate Ecuadorian RUC (13 digits, starts with valid cedula or entity code)."""
    if not ruc or not ruc.isdigit() or len(ruc) != 13:
        return False, "El RUC debe tener exactamente 13 digitos numericos."

    if not ruc.endswith("001"):
        return False, "El RUC debe terminar en 001."

    third = int(ruc[2])
    if third < 6:
        # Natural person RUC — validate cedula portion
        valid, msg = _validate_cedula_ecuador(ruc[:10])
        if not valid:
            return False, f"RUC de persona natural invalido: {msg}"
    elif third == 6:
        # Public entity
        pass
    elif third == 9:
        # Private entity (sociedad)
        pass
    else:
        return False, f"Tercer digito invalido: {third}."

    return True, "OK"


def _validate_cedula_or_ruc(value: str) -> tuple[bool, str, str]:
    """Validate and classify a cedula or RUC. Returns (valid, message, type)."""
    clean = value.strip().replace("-", "").replace(" ", "")
    if len(clean) == 10:
        valid, msg = _validate_cedula_ecuador(clean)
        return valid, msg, "cedula"
    elif len(clean) == 13:
        valid, msg = _validate_ruc_ecuador(clean)
        return valid, msg, "ruc"
    else:
        return False, "Debe ser una cedula (10 digitos) o RUC (13 digitos).", "unknown"


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
            # Create a verification session (valid 24h)
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
                },
            )
            return True, "Codigo verificado correctamente. Ahora puede consultar datos financieros."

        remaining = max_attempts - attempts - 1
        if remaining <= 0:
            await client.patch(
                f"{supabase_url}/rest/v1/verification_tokens?id=eq.{token_id}",
                headers=headers, json={"used": True},
            )
            return False, "Codigo incorrecto. No quedan intentos. Solicite uno nuevo."
        return False, f"Codigo incorrecto. Quedan {remaining} intento(s)."


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


def _send_otp_email(email: str, code: str, company_name: str = "TecnoSmart", tenant_id: str | None = None, supa_url: str | None = None, supa_key: str | None = None) -> tuple[bool, str]:
    """Send OTP via SMTP with HTML template. Returns (success, message).

    First tries tenant-specific SMTP config from Supabase, falls back to env vars.
    """
    import os
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText as _MIMEText

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
            "Buscar productos en el catalogo usando busqueda hibrida "
            "(semantica + literal). Devuelve PAGINADO: la primera llamada "
            "trae los primeros top_k productos (default 10) PRIORIZANDO los "
            "que tienen stock. Si el cliente pide ver mas, llama de nuevo "
            "con el MISMO query y offset=10 para los siguientes 10. "
            "Resultados con precio y stock LIVE de Odoo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto de busqueda natural (ej: 'laptop para oficina', 'tinta epson', 'procesador')"},
                "top_k": {"type": "integer", "description": "Resultados por pagina (default 10). NO hay limite maximo — pide todos los que necesites; el sistema devuelve hasta lo que encuentre el catalogo.", "default": 10},
                "offset": {"type": "integer", "description": "Offset para paginacion. Usa 0 en la primera llamada, top_k en la siguiente, etc.", "default": 0},
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
            "Cada linea DEBE traer **product_code** (SKU del catalogo, ej. 'VID0581') — los "
            "SKUs vienen del campo `code` de cada resultado de `search_products`. "
            "Si NO tienes el SKU exacto, llama `search_products` primero. NUNCA inventes "
            "codigos. El campo `product_id` (template_id numerico) sigue aceptado solo por "
            "compatibilidad legacy y se va a remover. "
            "IMPORTANTE: Antes de llamar esta herramienta, confirma con el cliente el resumen "
            "de productos, cantidades y precios. "
            "Para ventas a CONSUMIDOR FINAL (RUC 9999999999999): si la empresa requiere datos "
            "del consumidor final, pasa end_customer_name, end_customer_phone y end_customer_email."
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
                            "product_code": {
                                "type": "string",
                                "description": (
                                    "Codigo SKU del catalogo (ej. 'VID0581'). PREFERIDO. "
                                    "Tomalo del campo `code` del resultado de search_products."
                                ),
                            },
                            "product_id": {
                                "type": "integer",
                                "description": (
                                    "DEPRECATED — usa product_code. Aceptado por compat. "
                                    "Es el template_id numerico de Odoo."
                                ),
                            },
                            "quantity": {"type": "number", "default": 1},
                        },
                        # Cada linea debe traer product_code O product_id; el handler valida.
                    },
                },
                "notes": {"type": "string", "description": "Notas adicionales", "default": ""},
                "end_customer_name": {"type": "string", "description": "Nombre del consumidor final (solo para ventas a consumidor final)"},
                "end_customer_phone": {"type": "string", "description": "Telefono del consumidor final"},
                "end_customer_email": {"type": "string", "description": "Email del consumidor final"},
            },
            "required": ["partner_id", "lines"],
        },
    },
    {
        "name": "add_to_quotation",
        "description": (
            "Agregar uno o mas productos a una cotizacion EXISTENTE en estado borrador. "
            "USA ESTA en lugar de create_quotation cuando ya creaste una cotizacion para el "
            "cliente y quiere agregar mas productos. NO crea una orden nueva — modifica la existente. "
            "Cada linea DEBE traer **product_code** (SKU del catalogo). El campo `product_id` "
            "(template_id) se acepta solo por compat legacy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID de la cotizacion existente (devuelto por create_quotation)"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_code": {
                                "type": "string",
                                "description": (
                                    "Codigo SKU del catalogo (ej. 'VID0581'). PREFERIDO. "
                                    "Tomalo del campo `code` del resultado de search_products."
                                ),
                            },
                            "product_id": {
                                "type": "integer",
                                "description": (
                                    "DEPRECATED — usa product_code. Aceptado por compat. "
                                    "Es el template_id numerico de Odoo."
                                ),
                            },
                            "quantity": {"type": "number", "default": 1},
                        },
                        # Cada linea debe traer product_code O product_id; el handler valida.
                    },
                },
            },
            "required": ["order_id", "lines"],
        },
    },
    {
        "name": "change_quotation_customer",
        "description": (
            "Cambiar el cliente (partner_id) de una cotizacion en borrador. "
            "Usa cuando el vendedor descubre a mitad del flujo que la cotizacion "
            "estaba destinada a otro cliente — Odoo permite reasignar partner_id "
            "mientras state in ('draft','sent'). En cotizaciones confirmadas falla. "
            "Si seller_context esta presente, valida que la cotizacion pertenezca "
            "a este vendedor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID interno de Odoo de la cotizacion a reasignar.",
                },
                "new_partner_id": {
                    "type": "integer",
                    "description": (
                        "partner_id del NUEVO cliente. Tomalo del resultado de "
                        "search_partner (campo `odoo_id` del primer match) o "
                        "identify_customer (campo `partner_id`)."
                    ),
                },
            },
            "required": ["order_id", "new_partner_id"],
        },
    },
    {
        "name": "update_quotation_line",
        "description": (
            "Modificar la cantidad (product_uom_qty) de una linea existente "
            "en una cotizacion en borrador. Usar cuando el vendedor pide "
            "'cambiar a 5 unidades', 'pon 10 en lugar de 3', 'quita 1 unidad' "
            "(restas a la cantidad actual). Si la nueva cantidad es 0, el "
            "MCP devuelve quantity_zero — pide confirmacion al vendedor para "
            "borrar la linea con remove_quotation_line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "line_id": {
                    "type": "integer",
                    "description": "ID de la linea (sale.order.line). Tomalo de get_quotation o del array order_line.",
                },
                "quantity": {
                    "type": "number",
                    "description": "Nueva cantidad final (NO incremento). Si pides 'quitar 1' calcula nueva = actual - 1.",
                },
            },
            "required": ["order_id", "line_id", "quantity"],
        },
    },
    {
        "name": "remove_quotation_line",
        "description": (
            "Eliminar una linea de una cotizacion en borrador. SOLO llama "
            "esta tool DESPUES de que el vendedor confirmo explicitamente "
            "el borrado. Si llegaste aqui via update_quotation_line con "
            "cantidad 0 → primero pregunta '¿Confirmas eliminar la linea?' "
            "y solo si responde si llamas remove."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "line_id": {"type": "integer"},
            },
            "required": ["order_id", "line_id"],
        },
    },
    {
        "name": "get_active_quotation",
        "description": (
            "Buscar la cotizacion en borrador mas reciente de un cliente. Util cuando "
            "perdiste el track del order_id o cuando el cliente regresa despues. Devuelve "
            "order_id, name, total y lineas si existe; success:false si no hay borradores activos."
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
            "Listar las cotizaciones/ordenes de venta recientes de un cliente — formato COMPACTO "
            "(solo cabecera + totales, SIN lineas). SIEMPRE usa esta tool cuando el cliente pida "
            "ver sus cotizaciones, su historial, sus ultimas compras. NUNCA respondas de memoria — "
            "el modelo alucina lineas. Cada orden devuelve order_id, name, state, state_label, "
            "total, subtotal, fecha y lines_count. Si el cliente pide el detalle de UNA cotizacion "
            "especifica, usa get_quotation despues."
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
            "Leer el detalle COMPLETO (cabecera + todas las lineas) de UNA cotizacion por order_id. "
            "Usa esto SOLO cuando el cliente pida ver el detalle de una cotizacion especifica "
            "('muestrame VENTA120704', 'que tiene esa cotizacion'). Para listar las cotizaciones "
            "recientes usa list_quotations en su lugar (mas barato en tokens)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID de la cotizacion (devuelto por list_quotations o create_quotation)"},
            },
            "required": ["order_id"],
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
            "Usa despues de crear la cotizacion, si el cliente solicita recibirla por email."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID de la orden/cotizacion en Odoo"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "confirm_quotation",
        "description": (
            "Confirmar una cotizacion y convertirla en orden de venta. "
            "Solo usar cuando el cliente pida confirmar/aprobar la cotizacion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID de la orden/cotizacion en Odoo"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "apply_discount",
        "description": (
            "Aplicar un porcentaje de descuento a una cotizacion en estado 'draft' o 'sent'. "
            "Si pasas line_id, el descuento se aplica solo a esa linea. "
            "Si no pasas line_id, se aplica a TODAS las lineas del pedido. "
            "Recuerda: este tool NO valida si el descuento esta dentro del umbral del agente — "
            "esa validacion ocurre antes (vias guardrails / approval flow). "
            "Tras aplicar, los totales (amount_total, amount_untaxed) se recalculan automaticamente "
            "y se devuelven en el resultado."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "description": "ID interno de Odoo de la cotizacion"},
                "discount_pct": {"type": "number", "description": "Porcentaje de descuento (0-100). Ej: 5 = 5%"},
                "line_id": {"type": "integer", "description": "Opcional: ID de la linea especifica. Sin esto, aplica a todas las lineas."},
                "reason": {"type": "string", "description": "Opcional: motivo del descuento. Se guarda como nota interna en la cotizacion."},
            },
            "required": ["order_id", "discount_pct"],
        },
    },
    {
        "name": "list_my_quotations",
        "description": (
            "Listar las cotizaciones de un vendedor especifico (sale.order.user_id). "
            "Por defecto trae las cotizaciones activas (state in ['draft','sent']). "
            "Util para que un vendedor B2B revise sus pendientes o para retomar cotizaciones "
            "abiertas con un cliente. Ordenado por fecha descendente, limite default 20."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "salesperson_user_id": {"type": "integer", "description": "ID del usuario Odoo (res.users.id) del vendedor"},
                "state": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["draft", "sent", "sale", "done", "cancel"]},
                    "description": "Filtro de estados. Default: ['draft','sent']",
                },
                "limit": {"type": "integer", "description": "Maximo de resultados", "default": 20},
            },
            "required": ["salesperson_user_id"],
        },
    },
    {
        "name": "schedule_visit",
        "description": (
            "Agendar una visita a un cliente creando un mail.activity tipo 'Meeting' en su ficha. "
            "El vendedor responsable (salesperson_user_id) la vera en su CRM/calendario. "
            "Usar cuando el vendedor B2B pida 'agendar visita a Juan Perez el viernes'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del partner (res.partner.id) — el cliente a visitar"},
                "summary": {"type": "string", "description": "Titulo corto de la visita (ej: 'Demo producto X')"},
                "date_deadline": {"type": "string", "description": "Fecha de la visita en formato YYYY-MM-DD"},
                "salesperson_user_id": {"type": "integer", "description": "ID del vendedor responsable (res.users.id)"},
                "note": {"type": "string", "description": "Opcional: nota detallada (puede contener HTML)"},
            },
            "required": ["partner_id", "summary", "date_deadline", "salesperson_user_id"],
        },
    },
    # Backend-only tool — invocada por niko/auth/seller_otp.py durante el flujo
    # /login del bot B2B. NO incluida en tools_enabled de ningun agente (no
    # es LLM-facing); el filtro allowed_tools en tools/call la deja invisible
    # al LLM aunque aparezca en este registro. Mantiene el prefijo odoo_
    # porque la convencion sin-prefijo es solo para tools que el LLM ve.
    {
        "name": "odoo_lookup_user_by_email",
        "description": (
            "Backend RPC: localiza un res.users de Odoo por email "
            "(login OR partner_id.email). Usado por el flujo /login del bot "
            "B2B para validar que el vendedor existe antes de enviar OTP."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Email del vendedor a validar"},
            },
            "required": ["email"],
        },
    },
    # Backend-only tools — Sprint 2F. Generic ERP-agnostic contracts that
    # niko/integrations/ wrappers consume. Keep the odoo_ prefix because
    # they are NOT LLM-facing (no agent.tools_enabled lists them) and the
    # allowed_tools filter on tools/call (mcp_transport.py L1018) blocks
    # any LLM agent from calling them anyway.
    {
        "name": "odoo_get_discount_policy",
        "description": (
            "Backend RPC: lee la politica de descuento del ERP "
            "(max_pct desde ir.config_parameter sale.partner_max_sale_discount "
            "y la lista de supervisores autorizados desde el grupo "
            "account.group_account_manager). Devuelve un contrato "
            "agnostico-ERP que el core de niko consume."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "odoo_verify_seller_authorization",
        "description": (
            "Backend RPC: confirma que un email corresponde a un vendedor "
            "autorizado en el ERP — busca el res.users y verifica que "
            "pertenece al grupo sales_team.group_sale_salesman. Mas profundo "
            "que odoo_lookup_user_by_email: lookup + chequeo de grupo en una "
            "sola llamada."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Email del vendedor a verificar"},
            },
            "required": ["email"],
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
            "El cliente recibira un codigo de 6 digitos en su correo. "
            "Pidele que te lo escriba en el chat y luego usa verify_otp."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "email": {"type": "string", "description": "Email del cliente (de identify_customer)"},
                "channel": {"type": "string", "description": "Canal actual (telegram, whatsapp, etc)"},
                "channel_user_id": {"type": "string", "description": "ID del usuario en el canal"},
            },
            "required": ["partner_id", "email", "channel", "channel_user_id"],
        },
    },
    {
        "name": "verify_otp",
        "description": (
            "Verificar el codigo OTP de 6 digitos que el cliente proporciona. "
            "Si es correcto, se desbloquea el acceso a datos financieros por 24 horas. "
            "Despues de verificar, puedes llamar check_balance para obtener los datos."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer", "description": "ID del cliente en Odoo"},
                "channel": {"type": "string", "description": "Canal (telegram, whatsapp, etc)"},
                "code": {"type": "string", "description": "Codigo de 6 digitos proporcionado por el cliente"},
            },
            "required": ["partner_id", "channel", "code"],
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
            "REQUIERE confirmed=true para ejecutar. Sin confirmed, solo muestra preview. "
            "Flujo: 1) Llama SIN confirmed con los datos → valida cedula, consulta SRI, devuelve preview. "
            "2) Muestra al cliente todos los datos y pide confirmacion. "
            "3) Si confirma, llama DE NUEVO con confirmed=true → crea el cliente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre completo del cliente"},
                "vat": {"type": "string", "description": "Cedula o RUC"},
                "email": {"type": "string", "description": "Correo electronico"},
                "phone": {"type": "string", "description": "Telefono"},
                "mobile": {"type": "string", "description": "Celular"},
                "street": {"type": "string", "description": "Direccion"},
                "city": {"type": "string", "description": "Ciudad"},
                "confirmed": {"type": "boolean", "description": "true para ejecutar, false/omitido para preview"},
            },
            "required": ["name", "vat"],
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
            "Obtener configuraciones de la empresa relevantes para ventas: "
            "partner_id del consumidor final (RUC 9999999999999), "
            "si se requieren datos del consumidor final (pedir_end_customer_data), "
            "y el monto maximo SRI para facturas de consumidor final (sri_invoice_limit). "
            "Llamar esto ANTES de crear una cotizacion de consumidor final."
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

        try:
            text = await _execute_tool(request, tool_name, args)
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


async def _execute_tool(request: Request, tool_name: str, args: dict) -> str:
    """Execute a tool and return text result."""
    from mcp_odoo.config import settings
    import httpx

    # Load tenant config (simplified — for now uses env/default tenant)
    # In production, extract JWT from MCP session headers
    tenant_config = await _get_tenant_config(request)

    tc = tenant_config  # shorthand
    creds = (tc["tenant_id"], tc["url"], tc["db"], tc["user"], tc["password"])

    # Read X-Seller-Context (sent by niko core when a B2B seller has
    # completed /login OTP). Plugin uses it to validate self-references
    # and to inject salesperson_user_id into quotation tools so niko
    # core does not need to know ERP-specific args.
    seller_ctx_raw = request.headers.get("x-seller-context") or ""
    seller_ctx: dict = {}
    if seller_ctx_raw:
        try:
            seller_ctx = json.loads(seller_ctx_raw) or {}
        except (json.JSONDecodeError, TypeError):
            seller_ctx = {}
    seller_uid: int | None = None
    seller_partner_id: int | None = None
    seller_partner_vat: str = ""
    if seller_ctx:
        try:
            seller_uid = int(seller_ctx.get("odoo_user_id")) \
                if seller_ctx.get("odoo_user_id") is not None else None
        except (TypeError, ValueError):
            seller_uid = None
        try:
            seller_partner_id = int(seller_ctx.get("partner_id")) \
                if seller_ctx.get("partner_id") is not None else None
        except (TypeError, ValueError):
            seller_partner_id = None
        seller_partner_vat = (seller_ctx.get("partner_vat") or "").strip()

    # ── B2B guards: applied centrally before the tool dispatcher so
    # individual tool functions stay simple. Each guard returns an
    # error envelope as JSON text — the LLM reads `error_code` and
    # corrects on the next turn.
    if seller_uid is not None:
        # Guard 1: identify_customer with seller's own VAT.
        if tool_name == "identify_customer" and seller_partner_vat:
            cedula_ruc = (str(args.get("cedula_ruc") or "")).strip()
            if cedula_ruc and cedula_ruc == seller_partner_vat:
                logger.warning(
                    "BLOCKED identify_customer: cedula_ruc %s equals "
                    "seller's own vat (seller_uid=%s)",
                    cedula_ruc, seller_uid,
                )
                # ``llm_action`` is an INTERNAL directive to the LLM in
                # Spanish (matches the model's working language). Rule
                # mcp-error-handling (priority 270) tells the LLM to
                # follow it silently and only show the final result.
                return json.dumps({
                    "success": False,
                    "error_code": "self_lookup_blocked",
                    "llm_action": (
                        "INTERNA: ese RUC es del vendedor. Llama "
                        "search_partner con el nombre del cliente que "
                        "mencionó el vendedor. NO muestres este error "
                        "al usuario."
                    ),
                }, ensure_ascii=False, indent=2)

        # Guard 2: create_quotation — validate partner_id + inject
        # salesperson_user_id.
        if tool_name == "create_quotation":
            raw_partner = args.get("partner_id")
            try:
                partner_id_arg = int(raw_partner) if raw_partner is not None else None
            except (TypeError, ValueError):
                partner_id_arg = None

            # Auto-inject partner_id from the persisted active_customer
            # state when the LLM forgot to pass it. This breaks the
            # missing_partner → search_partner → retry loop that
            # surfaced as GraphRecursionError. Niko orchestrator
            # populates seller_ctx.active_customer_partner_id whenever
            # a previous turn's search_partner returned a unique match.
            if partner_id_arg is None:
                try:
                    ac_pid = int(seller_ctx.get("active_customer_partner_id")) \
                        if seller_ctx.get("active_customer_partner_id") is not None else None
                except (TypeError, ValueError):
                    ac_pid = None
                if ac_pid is not None:
                    logger.info(
                        "INJECT create_quotation.partner_id=%d from "
                        "active_customer (seller_uid=%s, name=%r)",
                        ac_pid, seller_uid,
                        seller_ctx.get("active_customer_name") or "",
                    )
                    args["partner_id"] = ac_pid
                    partner_id_arg = ac_pid

            if partner_id_arg is None:
                logger.warning(
                    "BLOCKED create_quotation: missing partner_id "
                    "(seller_uid=%s)", seller_uid,
                )
                return json.dumps({
                    "success": False,
                    "error_code": "missing_partner",
                    "llm_action": (
                        "INTERNA: falta partner_id. Llama "
                        "search_partner con el nombre del cliente, toma "
                        "el campo `odoo_id` del primer resultado y "
                        "vuelve a llamar create_quotation pasando ese "
                        "valor como partner_id (entero). NO muestres "
                        "este error al usuario."
                    ),
                }, ensure_ascii=False, indent=2)

            if seller_partner_id is not None and partner_id_arg == seller_partner_id:
                logger.warning(
                    "BLOCKED create_quotation: partner_id %s equals "
                    "seller's own partner_id (seller_uid=%s)",
                    partner_id_arg, seller_uid,
                )
                return json.dumps({
                    "success": False,
                    "error_code": "wrong_partner",
                    "llm_action": (
                        "INTERNA: ese partner_id es del vendedor. "
                        "Llama search_partner con el nombre del cliente, "
                        "toma `odoo_id` del primer resultado y vuelve a "
                        "llamar create_quotation con ese valor. NO "
                        "muestres este error al usuario."
                    ),
                }, ensure_ascii=False, indent=2)

            # Inject salesperson_user_id from seller_context (override).
            if args.get("salesperson_user_id") != seller_uid:
                logger.info(
                    "INJECT create_quotation.salesperson_user_id=%s "
                    "(was=%s)", seller_uid, args.get("salesperson_user_id"),
                )
                args["salesperson_user_id"] = seller_uid

        # Guard 3: add_to_quotation — inject salesperson_user_id.
        if tool_name == "add_to_quotation":
            if args.get("salesperson_user_id") != seller_uid:
                logger.info(
                    "INJECT add_to_quotation.salesperson_user_id=%s "
                    "(was=%s)", seller_uid, args.get("salesperson_user_id"),
                )
                args["salesperson_user_id"] = seller_uid

    if tool_name == "search_products":
        return await _rag_search(
            args["query"],
            top_k=max(int(args.get("top_k", 10)), 1),
            offset=max(int(args.get("offset", 0)), 0),
            tenant_id=tc["tenant_id"],
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
        base_url = tc["url"].rstrip("/")
        results = []
        for p in products:
            # Build image URL: Odoo serves product images via /web/image
            has_image = bool(p.get("image_128"))
            image_url = (
                f"{base_url}/web/image/product.template/{p['id']}/image_256"
                if has_image else None
            )
            raw_price = p.get("list_price", 0) or 0
            raw_cost = p.get("standard_price", 0) or 0
            results.append({
                "id": p["id"],
                "code": p.get("default_code", ""),
                "name": p.get("name", ""),
                # Keep the numeric fields so any existing caller keeps
                # working; ALSO emit display strings that the LLM should
                # copy verbatim (tokenizer-safe comma-separated format).
                "price": raw_price,
                "price_display": _format_price_display(raw_price) if raw_price else "consultar",
                "cost": raw_cost,
                "cost_display": _format_price_display(raw_cost) if raw_cost else "consultar",
                "stock": p.get("qty_available", 0),
                "available": p.get("virtual_available", 0),
                "description": p.get("description_sale") or "",
                "category": p.get("categ_id", [None, ""])[1] if isinstance(p.get("categ_id"), list) else "",
                "barcode": p.get("barcode") or "",
                "image_url": image_url,
            })
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

    if tool_name == "search_partner":
        return await _rag_search_partners(args["query"], args.get("top_k", 5), tc["tenant_id"])

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
            *creds, args["order_id"], args["lines"],
            salesperson_user_id=args.get("salesperson_user_id"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "change_quotation_customer":
        from mcp_odoo.tools.sales import odoo_change_quotation_customer
        # Auto-inject salesperson_user_id from seller_context so the
        # plugin can verify the quotation belongs to this seller.
        sp_uid = seller_uid if seller_uid is not None else None
        result = odoo_change_quotation_customer(
            *creds,
            int(args["order_id"]),
            int(args["new_partner_id"]),
            salesperson_user_id=sp_uid,
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "update_quotation_line":
        from mcp_odoo.tools.sales import odoo_update_quotation_line
        result = odoo_update_quotation_line(
            *creds,
            int(args["order_id"]),
            int(args["line_id"]),
            float(args["quantity"]),
            salesperson_user_id=seller_uid,
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "remove_quotation_line":
        from mcp_odoo.tools.sales import odoo_remove_quotation_line
        result = odoo_remove_quotation_line(
            *creds,
            int(args["order_id"]),
            int(args["line_id"]),
            salesperson_user_id=seller_uid,
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
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "get_quotation":
        from mcp_odoo.tools.sales import odoo_get_quotation
        result = odoo_get_quotation(*creds, args["order_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "render_quotation_pdf":
        from mcp_odoo.tools.sales import odoo_render_quotation_pdf
        result = odoo_render_quotation_pdf(*creds, args["order_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "send_quotation":
        from mcp_odoo.tools.sales import odoo_send_quotation
        result = odoo_send_quotation(*creds, args["order_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "confirm_quotation":
        from mcp_odoo.tools.sales import odoo_confirm_sale_order
        result = odoo_confirm_sale_order(*creds, args["order_id"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "apply_discount":
        from mcp_odoo.tools import sales as _sales
        result = _sales.odoo_apply_discount(
            *creds,
            order_id=int(args["order_id"]),
            discount_pct=float(args["discount_pct"]),
            line_id=int(args["line_id"]) if args.get("line_id") is not None else None,
            reason=args.get("reason"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "list_my_quotations":
        from mcp_odoo.tools import sales as _sales
        result = _sales.odoo_list_my_quotations(
            *creds,
            salesperson_user_id=int(args["salesperson_user_id"]),
            state=args.get("state"),
            limit=int(args.get("limit", 20)),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "schedule_visit":
        from mcp_odoo.tools import sales as _sales
        result = _sales.odoo_schedule_visit(
            *creds,
            partner_id=int(args["partner_id"]),
            summary=args["summary"],
            date_deadline=args["date_deadline"],
            salesperson_user_id=int(args["salesperson_user_id"]),
            note=args.get("note"),
        )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # Backend-only — niko/auth/seller_otp.py via lookup_odoo_user_by_email wrapper.
    if tool_name == "odoo_lookup_user_by_email":
        from mcp_odoo.tools import sales as _sales
        result = _sales.odoo_lookup_user_by_email(*creds, email=args["email"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # Backend-only — Sprint 2F generic ERP-agnostic contracts.
    if tool_name == "odoo_get_discount_policy":
        from mcp_odoo.tools import sales as _sales
        result = _sales.odoo_get_discount_policy(*creds)
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    if tool_name == "odoo_verify_seller_authorization":
        from mcp_odoo.tools import sales as _sales
        result = _sales.odoo_verify_seller_authorization(*creds, email=args["email"])
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

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
        email = args["email"]
        channel = args["channel"]
        channel_user_id = args["channel_user_id"]

        if not email or "@" not in email:
            return json.dumps({"success": False, "error": "El cliente no tiene correo electronico registrado."}, ensure_ascii=False)

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
        success, msg = await _otp_verify(_supa_url, _supa_key, _tenant, args["partner_id"], args["channel"], args["code"])
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

    if tool_name == "lookup_sri":
        return await _lookup_sri(args["cedula_ruc"])

    if tool_name == "create_partner":
        return await _create_partner(creds, args)

    if tool_name == "update_partner":
        return await _update_partner(creds, args)

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
    from mcp_odoo.tools.generic import odoo_write, odoo_read
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

    # Phase 1: Preview — read current data and show what would change
    if not confirmed:
        try:
            current = odoo_read(*creds, "res.partner", [partner_id],
                               ["name", "vat", "email", "phone", "mobile", "street", "city"])[0]
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
        partner = odoo_read(*creds, "res.partner", [partner_id],
                           ["id", "name", "vat", "email", "phone", "mobile",
                            "street", "city"])[0]

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
    from mcp_odoo.tools.generic import odoo_search as _search
    from mcp_odoo.tools.sales import odoo_check_balance

    clean = cedula_ruc.strip().replace("-", "").replace(" ", "")
    valid, msg, doc_type = _validate_cedula_or_ruc(clean)
    if not valid:
        return json.dumps({"found": False, "error": msg}, ensure_ascii=False)

    # Search by VAT in Odoo
    partners = _search(
        *creds,
        "res.partner",
        [["vat", "=", clean]],
        fields=["name", "vat", "email", "phone", "mobile", "street", "city",
                "credit_limit", "customer_rank", "supplier_rank", "property_payment_term_id"],
        limit=1,
    )

    if not partners:
        # Try with RUC if cedula was given (some partners stored with 001 suffix)
        if doc_type == "cedula":
            partners = _search(
                *creds,
                "res.partner",
                [["vat", "=", clean + "001"]],
                fields=["name", "vat", "email", "phone", "mobile", "street", "city",
                        "credit_limit", "customer_rank", "supplier_rank", "property_payment_term_id"],
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


async def _rag_search(
    query: str, top_k: int = 10, offset: int = 0, tenant_id: str = ""
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
    """
    cache_key = query.strip().lower()
    cached_ranked = _query_cache_get(tenant_id, cache_key)
    if cached_ranked is not None:
        return _format_ranked_page(cached_ranked, top_k, offset)

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
    _query_cache_set(tenant_id, cache_key, ranked)
    return _format_ranked_page(ranked, top_k, offset)


def _format_ranked_page(ranked: list[dict], top_k: int, offset: int) -> str:
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
    """
    import json as _json

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

        # Use tokenizer-safe formatter ("USD 1,383.00" — comma thousands
        # separator prevents the BPE merge that drops the first digit of
        # 4+ digit prices on Qwen2.5/Qwen3 AWQ models. See
        # _format_price_display() docstring for details.
        price_display = _format_price_display(price) if price else "consultar"
        if price:
            price_part = f"💰 {price_display}"
        else:
            price_part = "💰 consultar"

        if qty is None:
            stock_part = ""  # tenant without Odoo wired
            stock_display = ""
        elif qty > 0:
            qty_str = str(int(qty)) if qty == int(qty) else f"{qty:.2f}"
            stock_part = f"   📦 {qty_str} disponibles"
            stock_display = f"{qty_str} disponibles"
        else:
            stock_part = "   📦 agotado"
            stock_display = "agotado"

        line_text = f"{title}\n      {price_part}{stock_part}"

        rows.append({
            "template_id": tmpl_id,
            "code": code or "",
            "line_text": line_text,
            # Structured fields (safe for LLM arithmetic) alongside the
            # already-rendered line_text. The LLM should PREFER line_text
            # for display and only touch price_raw for calculations.
            "price_raw": float(price) if price else 0.0,
            "price_display": price_display,
            "stock_raw": (float(qty) if qty is not None else None),
            "stock_display": stock_display,
        })

    # Header
    showing_from = offset + 1
    showing_to = min(offset + top_k, total)
    has_more = showing_to < total

    if fallback_count and fallback_count == len(page):
        header = (
            f"📦 {total} productos en catalogo (no puedo confirmar "
            f"precio/stock ahora — Odoo no responde):"
        )
    elif in_stock_total == 0 and total > 0:
        header = (
            f"📦 {total} productos relacionados, NINGUNO con stock disponible:"
        )
    else:
        header = (
            f"📦 {total} productos ({in_stock_total} con stock) · "
            f"Mostrando {showing_from}-{showing_to}"
        )

    # Footer: selection + pagination hints
    footer_parts: list[str] = []
    footer_parts.append(
        "👉 Dime el numero (ej: 1) o el codigo (ej: CPU0245) para cotizar."
    )
    if has_more:
        footer_parts.append(
            f"👉 Di \"siguiente\" para ver los proximos {min(top_k, total - showing_to)}."
        )
    footer = "\n".join(footer_parts)

    payload = {
        "header": header,
        "rows": rows,
        "footer": footer,
        "instructions_internal": (
            "Para responder al usuario: escribe header, luego una linea en blanco, "
            "luego cada line_text en orden separados por linea en blanco, luego "
            "linea en blanco, luego footer. NUNCA muestres template_id al usuario. "
            "Memoriza la posicion (1,2,3...) -> code (SKU) para usar despues en "
            "create_quotation/add_to_quotation. "
            "REGLA DE COTIZACION: al llamar create_quotation o add_to_quotation, "
            "en cada linea pasa **product_code** con el valor exacto del campo `code` "
            "que aparece en este JSON (ej. 'VID0581'). NUNCA inventes SKUs. NUNCA "
            "uses product_id (template_id numerico) — es legacy. Si no tienes el "
            "SKU exacto del producto que el cliente pidio, llama search_products "
            "otra vez. "
            "REGLA DE PRECIOS: el campo line_text ya contiene el precio formateado "
            "correctamente (ej: 'USD 1,383.00'). Coopialo VERBATIM — nunca reformatees "
            "el precio ni lo conviertas a '$NNNN' porque eso puede corromper los "
            "digitos. Si necesitas hacer aritmetica, usa price_raw (float) en vez "
            "de parsear line_text."
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

    if not results:
        return "No encontre contactos que coincidan con esa busqueda."

    lines = []
    for r in results:
        meta = r.get("metadata", {})
        partner_type = meta.get("type", "contacto")
        score = r.get("score", 0)
        lines.append(
            f"- {meta.get('name', r.get('name', '?'))} | {meta.get('vat', r.get('vat', ''))} | "
            f"{meta.get('city', '')} | {meta.get('email', '')} | "
            f"Tel: {meta.get('phone', '')} | Tipo: {partner_type} (score: {score:.2f})"
        )
    return f"Encontre {len(results)} contactos:\n" + "\n".join(lines)
