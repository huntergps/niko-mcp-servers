"""MCP Memory Server — Shared contact memory across Hermes agents.

Exposes contact profiles, memories, and conversation RAG as MCP tools.
Hermes connects via: url: "http://mcp-memory:8090/mcp"

Multi-tenant: providers are cached per tenant_id. Tenant routing comes from
X-Tenant-ID / X-Agent-Slug headers; env vars TENANT_ID / TENANT_SLUG are
kept as fallback for single-container deployments.
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

# Import the shared memory provider
import sys

sys.path.insert(0, os.path.dirname(__file__))
from memory_provider import SupabaseMemoryProvider

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://localhost:8000")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TENANT_SLUG = os.environ.get("TENANT_SLUG", "")
TENANT_ID = os.environ.get("TENANT_ID", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
REDIS_URL = os.environ.get("REDIS_URL", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8090"))


# ---------------------------------------------------------------------------
# Per-tenant provider cache
# ---------------------------------------------------------------------------

_providers: dict[str, SupabaseMemoryProvider] = {}
_slug_cache: dict[str, str] = {}  # tenant_id -> tenant_slug


async def _resolve_slug(tenant_id: str) -> str:
    """Resolve tenant_slug from tenant_id via Supabase REST, with cache."""
    if tenant_id in _slug_cache:
        return _slug_cache[tenant_id]
    if not tenant_id or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/tenants",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                },
                params={"id": f"eq.{tenant_id}", "select": "slug"},
            )
            resp.raise_for_status()
            rows = resp.json()
            slug = rows[0]["slug"] if rows else ""
    except Exception as e:
        logger.warning(f"Failed to resolve slug for tenant_id={tenant_id}: {e}")
        slug = ""
    _slug_cache[tenant_id] = slug
    return slug


def get_provider_for_tenant(
    tenant_id: str = "", tenant_slug: str = ""
) -> SupabaseMemoryProvider:
    """Return a cached provider scoped to the given tenant."""
    tid = tenant_id or TENANT_ID
    slug = tenant_slug or TENANT_SLUG
    key = tid or slug
    if not key:
        key = "__default__"
    if key not in _providers:
        _providers[key] = SupabaseMemoryProvider(
            supabase_url=SUPABASE_URL,
            service_key=SUPABASE_SERVICE_KEY,
            tenant_slug=slug,
            tenant_id=tid,
            ollama_url=OLLAMA_URL,
        )
    return _providers[key]


def get_provider() -> SupabaseMemoryProvider:
    """Backwards-compatible helper using env-var defaults."""
    return get_provider_for_tenant()


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "get_contact_profile",
        "description": (
            "Cargar el perfil de un contacto por su canal e ID de usuario. "
            "Retorna nombre, cedula/RUC, email, telefono, ID de Odoo, nivel de verificacion "
            "y preferencias. Este perfil es COMPARTIDO entre todos los agentes del tenant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Canal de comunicacion (telegram, whatsapp, web, etc.)",
                },
                "channel_user_id": {
                    "type": "string",
                    "description": "ID del usuario en el canal",
                },
            },
            "required": ["channel", "channel_user_id"],
        },
    },
    {
        "name": "save_contact_profile",
        "description": (
            "Crear o actualizar el perfil de un contacto. "
            "Usa esto cuando descubras datos nuevos del cliente: nombre, cedula, email, etc. "
            "Los datos se comparten automaticamente con todos los agentes del tenant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Canal (telegram, whatsapp, web)"},
                "channel_user_id": {"type": "string", "description": "ID del usuario en el canal"},
                "name": {"type": "string", "description": "Nombre del contacto"},
                "vat": {"type": "string", "description": "Cedula (10 digitos) o RUC (13 digitos)"},
                "email": {"type": "string", "description": "Correo electronico"},
                "phone": {"type": "string", "description": "Telefono"},
                "odoo_partner_id": {"type": "integer", "description": "ID del partner en Odoo"},
            },
            "required": ["channel", "channel_user_id"],
        },
    },
    {
        "name": "memory_identify_customer",
        "description": (
            "Buscar un contacto por su cedula o RUC en los perfiles almacenados en memoria. "
            "Util para verificar si ya conocemos a un cliente antes de buscarlo en Odoo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cedula_ruc": {
                    "type": "string",
                    "description": "Cedula (10 digitos) o RUC (13 digitos)",
                },
            },
            "required": ["cedula_ruc"],
        },
    },
    {
        "name": "search_memories",
        "description": (
            "Buscar hechos y datos conocidos sobre un contacto usando busqueda semantica. "
            "Estos datos fueron extraidos automaticamente de conversaciones previas con CUALQUIER agente. "
            "Ejemplo: 'preferencias de envio', 'productos que compra', 'nombre de empresa'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Texto de busqueda semantica",
                },
                "channel_user_id": {
                    "type": "string",
                    "description": "ID del usuario en el canal (opcional si se usa partner_id)",
                },
                "partner_id": {
                    "type": "integer",
                    "description": "ID del partner en Odoo (opcional, para busqueda cross-channel)",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Numero de resultados",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Guardar un hecho o dato importante sobre un contacto. "
            "Usa esto cuando aprendas algo nuevo del cliente que otros agentes deban saber. "
            "Ejemplo: 'Prefiere factura electronica', 'Es distribuidor de zona norte'. "
            "La deduplicacion es automatica."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Canal"},
                "channel_user_id": {"type": "string", "description": "ID usuario"},
                "memory": {"type": "string", "description": "El hecho o dato a recordar"},
                "category": {
                    "type": "string",
                    "description": "Categoria: personal, preference, professional, intent, service, general",
                    "default": "general",
                },
            },
            "required": ["channel", "channel_user_id", "memory"],
        },
    },
    {
        "name": "search_conversations",
        "description": (
            "Busqueda semantica en el historial de conversaciones de un contacto, "
            "cruzando TODOS los agentes del tenant. Util para recordar lo que se hablo "
            "en interacciones anteriores. Ejemplo: 'la cotizacion de laptops que pidio ayer'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto de busqueda"},
                "channel_user_id": {
                    "type": "string",
                    "description": "ID del usuario (opcional para filtrar)",
                },
                "channel": {
                    "type": "string",
                    "description": "Canal (opcional para filtrar)",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Numero de resultados",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "load_user_context",
        "description": (
            "Cargar el contexto completo de un usuario: perfil + memorias. "
            "Retorna un bloque de texto formateado listo para inyectar en el system prompt. "
            "Usa esto al inicio de cada conversacion para saber quien es el contacto."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Canal"},
                "channel_user_id": {"type": "string", "description": "ID del usuario"},
            },
            "required": ["channel", "channel_user_id"],
        },
    },
    # ----- Conversation RAG (populate conversation_embeddings) -----
    {
        "name": "save_conversation_turn",
        "description": (
            "Guardar un turno de conversacion para RAG. "
            "Llamar despues de cada interaccion para que otros agentes puedan buscar en el historial."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Canal (telegram, whatsapp)"},
                "channel_user_id": {"type": "string", "description": "ID del usuario"},
                "content": {"type": "string", "description": "Texto de la conversacion (pregunta + respuesta)"},
                "direction": {"type": "string", "enum": ["inbound", "outbound"], "default": "outbound"},
                "summary": {"type": "string", "description": "Resumen breve del turno (opcional)"},
            },
            "required": ["channel", "channel_user_id", "content"],
        },
    },
    # ----- Inter-agent coordination (part of shared memory) -----
    {
        "name": "send_agent_message",
        "description": (
            "Enviar un mensaje a otro agente de la misma empresa. "
            "Usa esto para pedir ayuda, delegar tareas, o escalar problemas. "
            "Ejemplo: Ventas pide a Inventarios verificar stock."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient_agent": {"type": "string", "description": "Slug del agente destino (ej: 'inventarios')"},
                "content": {"type": "string", "description": "Mensaje a enviar"},
                "message_type": {
                    "type": "string",
                    "enum": ["text", "task_request", "task_result", "escalation", "handoff"],
                    "default": "text",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                    "default": "normal",
                },
            },
            "required": ["recipient_agent", "content"],
        },
    },
    {
        "name": "get_my_messages",
        "description": "Obtener mensajes no leidos dirigidos a este agente de parte de otros agentes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "create_task",
        "description": (
            "Crear una tarea para otro agente. "
            "Ejemplo: crear tarea de verificacion de stock, solicitud de aprobacion, etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Titulo breve de la tarea"},
                "description": {"type": "string", "description": "Descripcion detallada"},
                "assigned_to": {"type": "string", "description": "Slug del agente asignado"},
                "task_type": {
                    "type": "string",
                    "enum": ["general", "stock_check", "quotation", "approval", "escalation", "handoff"],
                    "default": "general",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "default": "medium",
                },
                "input_data": {"type": "object", "description": "Datos de entrada para la tarea"},
            },
            "required": ["title", "assigned_to"],
        },
    },
    {
        "name": "update_task",
        "description": "Actualizar estado de una tarea asignada a este agente.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "UUID de la tarea"},
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "completed", "failed", "cancelled"],
                },
                "result_data": {"type": "object", "description": "Resultado de la tarea"},
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "get_my_tasks",
        "description": "Obtener tareas pendientes asignadas a este agente.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "all"],
                    "default": "pending",
                },
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    # ----- Operator / Customer separation -----
    {
        "name": "set_default_customer",
        "description": (
            "Establecer el cliente predeterminado del operador. "
            "Cuando el operador compra para si mismo, usar su cedula guardada. "
            "Cuando compra para otra persona/empresa, cambiar temporalmente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Canal (telegram, whatsapp, web)"},
                "channel_user_id": {"type": "string", "description": "ID del usuario en el canal"},
                "vat": {"type": "string", "description": "Cedula/RUC del cliente para esta transaccion"},
                "name": {"type": "string", "description": "Nombre del cliente (si es diferente al operador)"},
                "set_as_default": {
                    "type": "boolean",
                    "default": False,
                    "description": "Si true, guardar como cliente predeterminado (default_vat)",
                },
            },
            "required": ["channel", "channel_user_id", "vat"],
        },
    },
    # ----- Cross-channel identity linking -----
    {
        "name": "link_contacts",
        "description": (
            "Vincular dos identidades del mismo cliente en canales diferentes. "
            "Ejemplo: el cliente Juan usa WhatsApp Y Telegram. Llamar cuando descubras "
            "que el mismo cliente tiene perfiles en diferentes canales (misma cédula, mismo nombre, etc.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_a": {"type": "string", "description": "Primer canal (telegram, whatsapp)"},
                "user_id_a": {"type": "string", "description": "ID del usuario en el primer canal"},
                "channel_b": {"type": "string", "description": "Segundo canal"},
                "user_id_b": {"type": "string", "description": "ID del usuario en el segundo canal"},
            },
            "required": ["channel_a", "user_id_a", "channel_b", "user_id_b"],
        },
    },
    {
        "name": "get_customer_history",
        "description": (
            "Obtener historial completo de un cliente en TODOS los canales. "
            "Busca por cédula/RUC o por canal+user_id. Retorna perfil, memorias "
            "y conversaciones anteriores de todos los canales vinculados."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cedula_ruc": {"type": "string", "description": "Cédula o RUC del cliente (opcional)"},
                "channel": {"type": "string", "description": "Canal actual (opcional)"},
                "channel_user_id": {"type": "string", "description": "ID del usuario (opcional)"},
            },
        },
    },
    # ----- Knowledge Graph tools -----
    {
        "name": "kg_query",
        "description": (
            "Consultar hechos del grafo de conocimiento sobre una entidad. "
            "Ejemplo: kg_query(entity='Juan Perez') retorna todos los hechos conocidos. "
            "Opcionalmente filtrar por predicado y consultar en un punto en el tiempo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Nombre de la entidad a consultar"},
                "predicate": {"type": "string", "description": "Filtrar por predicado (opcional)"},
                "as_of": {"type": "string", "description": "Consultar hechos validos en esta fecha ISO (opcional)"},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "kg_add",
        "description": (
            "Agregar un hecho al grafo de conocimiento como triple (entidad, predicado, objeto). "
            "Ejemplo: kg_add('Juan Perez', 'trabaja_en', 'Ferreteria El Clavo'). "
            "Los hechos son temporales: se puede especificar desde cuando es valido."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entidad sujeto"},
                "predicate": {"type": "string", "description": "Relacion o propiedad"},
                "object": {"type": "string", "description": "Valor u objeto de la relacion"},
                "valid_from": {"type": "string", "description": "Fecha ISO desde cuando es valido (default: ahora)"},
                "source_memory_id": {"type": "string", "description": "UUID de la memoria origen (opcional)"},
                "confidence": {"type": "number", "description": "Confianza 0.0-1.0 (default: 1.0)"},
            },
            "required": ["entity", "predicate", "object"],
        },
    },
    {
        "name": "kg_invalidate",
        "description": (
            "Marcar un hecho como ya no valido (set valid_until=ahora). "
            "Ejemplo: kg_invalidate('Juan Perez', 'trabaja_en', 'Ferreteria El Clavo') "
            "cuando cambia de trabajo. El hecho no se borra, queda como historico."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entidad sujeto"},
                "predicate": {"type": "string", "description": "Relacion o propiedad"},
                "object": {"type": "string", "description": "Valor especifico a invalidar (opcional, si vacio invalida todos con ese predicado)"},
            },
            "required": ["entity", "predicate"],
        },
    },
    {
        "name": "kg_timeline",
        "description": (
            "Obtener la linea de tiempo cronologica de todos los hechos de una entidad. "
            "Muestra hechos activos y expirados, util para ver la evolucion del conocimiento."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entidad a consultar"},
            },
            "required": ["entity"],
        },
    },
    # ----- MemPalace: Wings & Rooms tools -----
    {
        "name": "list_wings",
        "description": (
            "Listar todas las alas (wings) del palacio de memoria con conteo de contactos. "
            "Las alas organizan contactos por categoria: clientes, prospectos, proveedores, vip."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_rooms",
        "description": (
            "Listar habitaciones (rooms) dentro de un ala o todas las habitaciones. "
            "Las rooms son subcategorias dentro de un wing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Nombre del ala para filtrar (opcional)"},
            },
        },
    },
    {
        "name": "assign_contact_wing",
        "description": (
            "Asignar un contacto a un ala del palacio de memoria. "
            "Ejemplo: mover un prospecto al ala 'clientes' cuando realiza su primera compra."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Canal del contacto"},
                "channel_user_id": {"type": "string", "description": "ID del usuario en el canal"},
                "wing": {"type": "string", "description": "Nombre del ala destino"},
            },
            "required": ["channel", "channel_user_id", "wing"],
        },
    },
    {
        "name": "classify_memory_room",
        "description": (
            "Clasificar una memoria en una habitacion (room) del palacio de memoria. "
            "Ejemplo: asignar la memoria 'prefiere factura electronica' al room 'preferencias'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "UUID de la memoria"},
                "room": {"type": "string", "description": "Nombre de la habitacion"},
                "wing": {"type": "string", "description": "Nombre del ala (opcional, se infiere si no se da)"},
            },
            "required": ["memory_id", "room"],
        },
    },
]


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers
# ---------------------------------------------------------------------------

def _make_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _execute_tool(tool_name: str, args: dict, ctx: dict | None = None) -> str:
    """Execute an MCP tool and return text result."""
    ctx = ctx or {}
    tenant_id = ctx.get("tenant_id") or TENANT_ID
    tenant_slug = ctx.get("tenant_slug", "")
    # Resolve slug from id if we only have the id
    if tenant_id and not tenant_slug:
        tenant_slug = await _resolve_slug(tenant_id)
    provider = get_provider_for_tenant(tenant_id, tenant_slug)

    if tool_name == "get_contact_profile":
        profile = await provider.get_contact_profile(
            args["channel"], args["channel_user_id"]
        )
        if not profile:
            return json.dumps(
                {"found": False, "message": "No se encontro perfil para este contacto."},
                ensure_ascii=False,
            )
        return json.dumps(
            {"found": True, "profile": profile},
            ensure_ascii=False, indent=2, default=str,
        )

    if tool_name == "save_contact_profile":
        result = await provider.upsert_contact_profile(
            channel=args["channel"],
            channel_user_id=args["channel_user_id"],
            name=args.get("name"),
            vat=args.get("vat"),
            email=args.get("email"),
            phone=args.get("phone"),
            odoo_partner_id=args.get("odoo_partner_id"),
        )
        if result:
            # --- Operator/Customer separation: manage known_customers ---
            vat_val = args.get("vat", "").strip().replace("-", "").replace(" ", "")
            if vat_val and tenant_slug:
                try:
                    await _update_known_customers(
                        tenant_slug,
                        args["channel"],
                        args["channel_user_id"],
                        vat_val,
                        args.get("name"),
                    )
                except Exception as e:
                    logger.warning(f"update_known_customers failed: {e}")

            # Auto-link: if vat provided, check for same vat in other channels
            # Also check ALL known_customers vats for linking
            auto_linked = None
            if vat_val and tenant_slug:
                try:
                    auto_linked = await _auto_link_by_vat(
                        tenant_slug, args["channel"], args["channel_user_id"], vat_val
                    )
                except Exception as e:
                    logger.warning(f"Auto-link by vat failed: {e}")
            resp = {"success": True, "profile": result}
            if auto_linked:
                resp["auto_linked"] = auto_linked
            return json.dumps(resp, ensure_ascii=False, indent=2, default=str)
        return json.dumps(
            {"success": False, "error": "No se pudo guardar el perfil."},
            ensure_ascii=False,
        )

    if tool_name == "memory_identify_customer":
        raw = args["cedula_ruc"].strip().replace("-", "").replace(" ", "")
        # Normalize cedula (10 digits) → RUC (13 digits, +001) and try both
        candidates = [raw]
        if len(raw) == 10 and raw.isdigit():
            candidates.append(raw + "001")
        elif len(raw) == 13 and raw.isdigit() and raw.endswith("001"):
            candidates.append(raw[:10])

        # 1) Search in tenant.contact_profiles (live profiles, fastest)
        for vat in candidates:
            profile = await provider.get_contact_by_vat(vat)
            if profile:
                return json.dumps(
                    {"found": True, "source": "contact_profiles", "profile": profile},
                    ensure_ascii=False, indent=2, default=str,
                )

        # 2) Fallback: search in tenant.partner_embeddings (RAG of Odoo res.partner)
        try:
            import httpx
            for vat in candidates:
                async with httpx.AsyncClient(timeout=10) as client:
                    pe_resp = await client.get(
                        f"{SUPABASE_URL}/rest/v1/partner_embeddings",
                        params={
                            "select": "odoo_id,name,vat,metadata",
                            "vat": f"eq.{vat}",
                            "limit": "1",
                        },
                        headers={
                            "apikey": SUPABASE_SERVICE_KEY,
                            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                            "Accept-Profile": f"tenant_{tenant_slug}",
                        },
                    )
                    if pe_resp.status_code == 200:
                        rows = pe_resp.json() or []
                        if rows:
                            row = rows[0]
                            return json.dumps(
                                {
                                    "found": True,
                                    "source": "partner_embeddings",
                                    "profile": {
                                        "odoo_partner_id": row.get("odoo_id"),
                                        "name": row.get("name"),
                                        "vat": row.get("vat"),
                                        "metadata": row.get("metadata"),
                                    },
                                },
                                ensure_ascii=False, indent=2, default=str,
                            )
        except Exception as e:
            logger.warning(f"identify_customer partner_embeddings lookup failed: {e}")

        # 3) Last resort: live XML-RPC search via odoo_pool (source of truth)
        try:
            from memory_provider import odoo_search_partner_by_vat
            for vat in candidates:
                live = await odoo_search_partner_by_vat(tenant_id, vat)
                if live:
                    return json.dumps(
                        {"found": True, "source": "odoo_live", "profile": live},
                        ensure_ascii=False, indent=2, default=str,
                    )
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"identify_customer odoo live lookup failed: {e}")

        return json.dumps(
            {
                "found": False,
                "tried": candidates,
                "message": f"No se encontro cliente con cedula/RUC {raw} en contactos, RAG ni Odoo en vivo.",
            },
            ensure_ascii=False,
        )

    if tool_name == "search_memories":
        results = await provider.search_memories_semantic(
            query=args["query"],
            channel_user_id=args.get("channel_user_id"),
            partner_id=args.get("partner_id"),
            top_k=args.get("top_k", 5),
        )
        if not results:
            return json.dumps(
                {"count": 0, "memories": [], "message": "No se encontraron memorias relevantes."},
                ensure_ascii=False,
            )
        return json.dumps(
            {"count": len(results), "memories": results},
            ensure_ascii=False, indent=2, default=str,
        )

    if tool_name == "save_memory":
        result = await provider.save_memory(
            channel=args["channel"],
            channel_user_id=args["channel_user_id"],
            memory=args["memory"],
            category=args.get("category", "general"),
        )
        if result:
            return json.dumps(
                {"success": True, "memory_id": result.get("id", "")},
                ensure_ascii=False,
            )
        return json.dumps(
            {"success": False, "error": "No se pudo guardar la memoria."},
            ensure_ascii=False,
        )

    if tool_name == "search_conversations":
        results = await provider.search_conversations(
            query=args["query"],
            channel_user_id=args.get("channel_user_id"),
            channel=args.get("channel"),
            top_k=args.get("top_k", 10),
        )
        if not results:
            return json.dumps(
                {"count": 0, "results": [], "message": "No se encontraron conversaciones relevantes."},
                ensure_ascii=False,
            )
        return json.dumps(
            {"count": len(results), "results": results},
            ensure_ascii=False, indent=2, default=str,
        )

    if tool_name == "set_default_customer":
        return await _set_default_customer(tenant_slug, args)

    if tool_name == "load_user_context":
        context = await _load_user_context_with_customers(
            provider, tenant_slug, args["channel"], args["channel_user_id"]
        )
        if not context:
            return "No hay contexto previo para este contacto. Es la primera interaccion."
        return context

    # ----- Conversation RAG: save_conversation_turn -----
    if tool_name == "save_conversation_turn":
        try:
            import httpx

            content = args["content"]
            # Generate embedding via Ollama bge-m3
            async with httpx.AsyncClient(timeout=30) as client:
                embed_resp = await client.post(
                    f"{OLLAMA_URL}/api/embed",
                    json={"model": "bge-m3", "input": [content]},
                )
                embed_resp.raise_for_status()
                embedding = embed_resp.json()["embeddings"][0]

                # Insert into tenant schema conversation_embeddings (RAG)
                resp = await client.post(
                    f"{SUPABASE_URL}/rest/v1/conversation_embeddings",
                    headers={
                        "apikey": SUPABASE_SERVICE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                        "Content-Type": "application/json",
                        "Content-Profile": f"tenant_{tenant_slug}",
                        "Prefer": "return=representation",
                    },
                    json={
                        "channel": args["channel"],
                        "channel_user_id": args["channel_user_id"],
                        "content": content,
                        "direction": args.get("direction", "outbound"),
                        "summary": args.get("summary"),
                        "embedding": embedding,
                    },
                )
                resp.raise_for_status()

                # ALSO persist to tenant.conversation_messages so the dashboard
                # /conversations endpoint sees the full history (not just live).
                try:
                    msg_resp = await client.post(
                        f"{SUPABASE_URL}/rest/v1/conversation_messages",
                        headers={
                            "apikey": SUPABASE_SERVICE_KEY,
                            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                            "Content-Type": "application/json",
                            "Content-Profile": f"tenant_{tenant_slug}",
                            "Prefer": "return=minimal",
                        },
                        json={
                            "channel": args["channel"],
                            "channel_user_id": args["channel_user_id"],
                            "direction": args.get("direction", "outbound"),
                            "message_text": content,
                            "message_type": "text",
                            "agent_name": ctx.get("agent_slug"),
                            "tools_called": args.get("tools_called") or None,
                            "response_ms": args.get("response_ms"),
                            "tokens_used": args.get("tokens_used"),
                        },
                    )
                    if msg_resp.status_code >= 400:
                        logger.warning(
                            "conversation_messages insert failed: %s",
                            msg_resp.text[:200],
                        )
                except Exception as e:
                    logger.warning(f"conversation_messages persist failed: {e}")

            return json.dumps({"success": True}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"save_conversation_turn error: {e}")
            return json.dumps(
                {"success": False, "error": str(e)}, ensure_ascii=False
            )

    # ----- Inter-agent coordination tools -----
    # These use public.agent_messages and public.agent_tasks via Supabase REST
    # tenant_id already resolved at top of function
    agent_slug = ctx.get("agent_slug", "")

    if tool_name == "send_agent_message":
        return await _coord_send_message(tenant_id, agent_slug, args)

    if tool_name == "get_my_messages":
        return await _coord_get_messages(tenant_id, agent_slug, args)

    if tool_name == "create_task":
        return await _coord_create_task(tenant_id, agent_slug, args)

    if tool_name == "update_task":
        return await _coord_update_task(tenant_id, agent_slug, args)

    if tool_name == "get_my_tasks":
        return await _coord_get_tasks(tenant_id, agent_slug, args)

    # ----- Cross-channel identity linking tools -----
    if tool_name == "link_contacts":
        return await _link_contacts(tenant_slug, provider, args)

    if tool_name == "get_customer_history":
        return await _get_customer_history_with_known(tenant_slug, provider, args)

    # ----- Knowledge Graph tools -----
    if tool_name == "kg_query":
        return await _kg_query(tenant_slug, args)

    if tool_name == "kg_add":
        return await _kg_add(tenant_slug, args)

    if tool_name == "kg_invalidate":
        return await _kg_invalidate(tenant_slug, args)

    if tool_name == "kg_timeline":
        return await _kg_timeline(tenant_slug, args)

    # ----- MemPalace: Wings & Rooms tools -----
    if tool_name == "list_wings":
        return await _list_wings(tenant_slug)

    if tool_name == "list_rooms":
        return await _list_rooms(tenant_slug, args)

    if tool_name == "assign_contact_wing":
        return await _assign_contact_wing(tenant_slug, args)

    if tool_name == "classify_memory_room":
        return await _classify_memory_room(tenant_slug, args)

    return json.dumps({"error": f"Herramienta desconocida: {tool_name}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Cross-channel identity linking helpers
# ---------------------------------------------------------------------------

def _tenant_headers(tenant_slug: str) -> dict:
    """Headers for tenant-scoped Supabase REST calls."""
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Accept-Profile": f"tenant_{tenant_slug}",
        "Content-Profile": f"tenant_{tenant_slug}",
    }


async def _auto_link_by_vat(
    tenant_slug: str, channel: str, channel_user_id: str, vat: str
) -> dict | None:
    """Check if another profile with the same vat exists in a different channel.
    If so, auto-link them via link_tenant_contacts RPC.
    Also searches known_customers JSONB arrays for cross-channel matches."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        # Find all profiles with this vat in the tenant schema
        # Search both the vat column AND the known_customers JSONB array
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/contact_profiles",
            headers=_tenant_headers(tenant_slug),
            params={
                "or": f"(vat.eq.{vat},known_customers.cs.[{{\"vat\":\"{vat}\"}}])",
                "select": "id,channel,channel_user_id,org_id,vat,known_customers",
            },
        )
        resp.raise_for_status()
        profiles = resp.json()

        if len(profiles) < 2:
            return None  # No other profile to link

        # Find current profile and a different-channel profile
        current = None
        other = None
        for p in profiles:
            if p["channel"] == channel and p["channel_user_id"] == channel_user_id:
                current = p
            elif other is None:
                other = p

        if not current or not other:
            return None

        # Already linked (same org_id)?
        if current.get("org_id") and current["org_id"] == other.get("org_id"):
            return {"already_linked": True, "org_id": current["org_id"]}

        # Call RPC to link them
        rpc_resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/link_tenant_contacts",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "p_tenant_slug": tenant_slug,
                "contact_id_a": current["id"],
                "contact_id_b": other["id"],
            },
        )
        rpc_resp.raise_for_status()
        org_id = rpc_resp.json()
        logger.info(
            f"Auto-linked contacts by vat={vat}: "
            f"{channel}/{channel_user_id} <-> {other['channel']}/{other['channel_user_id']} "
            f"org_id={org_id}"
        )
        return {
            "linked": True,
            "org_id": org_id,
            "linked_channel": other["channel"],
            "linked_user_id": other["channel_user_id"],
        }


async def _update_known_customers(
    tenant_slug: str,
    channel: str,
    channel_user_id: str,
    vat: str,
    name: str | None = None,
) -> None:
    """Add a vat to the operator's known_customers list (deduplicated).

    Also sets default_vat if not already set.
    """
    import httpx
    from datetime import datetime, timezone

    headers = _tenant_headers(tenant_slug)

    async with httpx.AsyncClient(timeout=10) as client:
        # Fetch current profile
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/contact_profiles",
            headers=headers,
            params={
                "channel": f"eq.{channel}",
                "channel_user_id": f"eq.{channel_user_id}",
                "select": "id,default_vat,known_customers,name",
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return

        profile = rows[0]
        known = profile.get("known_customers") or []
        if not isinstance(known, list):
            known = []

        now = datetime.now(timezone.utc).isoformat()

        # Check if this vat already exists in known_customers
        found = False
        for entry in known:
            if entry.get("vat") == vat:
                entry["last_used"] = now
                if name:
                    entry["name"] = name
                found = True
                break

        if not found:
            known.append({
                "vat": vat,
                "name": name or "",
                "last_used": now,
            })

        # Build update payload
        update: dict = {"known_customers": known}

        # Set default_vat if not yet set
        if not profile.get("default_vat"):
            update["default_vat"] = vat

        # Patch the profile
        headers_patch = dict(headers)
        headers_patch["Prefer"] = "return=minimal"
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/contact_profiles",
            headers=headers_patch,
            params={
                "channel": f"eq.{channel}",
                "channel_user_id": f"eq.{channel_user_id}",
            },
            json=update,
        )


async def _set_default_customer(tenant_slug: str, args: dict) -> str:
    """Handler for set_default_customer tool."""
    import httpx
    from datetime import datetime, timezone

    channel = args["channel"]
    channel_user_id = args["channel_user_id"]
    vat = args["vat"].strip().replace("-", "").replace(" ", "")
    name = args.get("name")
    set_as_default = args.get("set_as_default", False)

    if not tenant_slug:
        return json.dumps(
            {"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False
        )

    headers = _tenant_headers(tenant_slug)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Fetch current profile
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/contact_profiles",
                headers=headers,
                params={
                    "channel": f"eq.{channel}",
                    "channel_user_id": f"eq.{channel_user_id}",
                    "select": "id,name,default_vat,known_customers,vat",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return json.dumps(
                    {"error": "No se encontro perfil para este operador."},
                    ensure_ascii=False,
                )

            profile = rows[0]
            known = profile.get("known_customers") or []
            if not isinstance(known, list):
                known = []

            now = datetime.now(timezone.utc).isoformat()

            # Add/update in known_customers
            found = False
            for entry in known:
                if entry.get("vat") == vat:
                    entry["last_used"] = now
                    if name:
                        entry["name"] = name
                    found = True
                    break

            if not found:
                known.append({
                    "vat": vat,
                    "name": name or "",
                    "last_used": now,
                })

            # Build update: always update vat (last used) and known_customers
            update: dict = {
                "vat": vat,
                "known_customers": known,
            }
            if set_as_default:
                update["default_vat"] = vat

            headers_patch = dict(headers)
            headers_patch["Prefer"] = "return=representation"
            patch_resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/contact_profiles",
                headers=headers_patch,
                params={
                    "channel": f"eq.{channel}",
                    "channel_user_id": f"eq.{channel_user_id}",
                },
                json=update,
            )
            patch_resp.raise_for_status()
            updated = patch_resp.json()
            updated_profile = updated[0] if isinstance(updated, list) and updated else updated

            # Determine customer label
            customer_name = name or ""
            if not customer_name:
                for entry in known:
                    if entry.get("vat") == vat:
                        customer_name = entry.get("name", "")
                        break

            return json.dumps(
                {
                    "success": True,
                    "active_customer": {
                        "vat": vat,
                        "name": customer_name,
                    },
                    "is_default": set_as_default or (profile.get("default_vat") == vat),
                    "known_customers_count": len(known),
                    "profile": updated_profile,
                },
                ensure_ascii=False, indent=2, default=str,
            )

    except Exception as e:
        logger.error(f"set_default_customer error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _load_user_context_with_customers(
    provider, tenant_slug: str, channel: str, channel_user_id: str
) -> str:
    """Enhanced load_user_context that includes operator/customer info."""
    import httpx

    # Get base context from provider
    base_context = await provider.load_user_context(
        user_id=channel_user_id, channel=channel
    )

    if not tenant_slug:
        return base_context

    # Fetch the operator-level fields (default_vat, known_customers)
    headers = _tenant_headers(tenant_slug)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/contact_profiles",
                headers=headers,
                params={
                    "channel": f"eq.{channel}",
                    "channel_user_id": f"eq.{channel_user_id}",
                    "select": "name,default_vat,known_customers,vat",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return base_context

            profile = rows[0]
            default_vat = profile.get("default_vat")
            known = profile.get("known_customers") or []
            current_vat = profile.get("vat")
            operator_name = profile.get("name") or "Desconocido"

            if not isinstance(known, list):
                known = []

            # Build the operator/customer section
            parts = []
            parts.append("")
            parts.append("=== OPERADOR / CLIENTE ===")
            parts.append(f"Operador: {operator_name} ({channel})")

            if default_vat:
                # Find the name for the default vat
                default_name = ""
                for entry in known:
                    if entry.get("vat") == default_vat:
                        default_name = entry.get("name", "")
                        break
                label = f"{default_name} ({default_vat})" if default_name else default_vat
                parts.append(f"Cliente default: {label}")

            if current_vat and current_vat != default_vat:
                current_name = ""
                for entry in known:
                    if entry.get("vat") == current_vat:
                        current_name = entry.get("name", "")
                        break
                label = f"{current_name} ({current_vat})" if current_name else current_vat
                parts.append(f"Cliente activo (ultimo usado): {label}")

            if known:
                customer_labels = []
                for entry in known:
                    v = entry.get("vat", "")
                    n = entry.get("name", "")
                    customer_labels.append(f"{n} ({v})" if n else v)
                parts.append(f"Clientes conocidos: {', '.join(customer_labels)}")

            customer_section = "\n".join(parts)

            if base_context:
                return base_context + "\n" + customer_section
            return customer_section.strip()

    except Exception as e:
        logger.warning(f"_load_user_context_with_customers error: {e}")
        return base_context


async def _get_customer_history_with_known(
    tenant_slug: str, provider, args: dict
) -> str:
    """Enhanced get_customer_history that also searches known_customers vats."""
    import httpx

    cedula_ruc = args.get("cedula_ruc", "").strip().replace("-", "").replace(" ", "")
    channel = args.get("channel", "")
    channel_user_id = args.get("channel_user_id", "")

    if not tenant_slug:
        return json.dumps(
            {"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False
        )

    if not cedula_ruc and not (channel and channel_user_id):
        return json.dumps(
            {"error": "Debe proporcionar cedula_ruc o channel+channel_user_id."},
            ensure_ascii=False,
        )

    # Get the base result from the original handler
    base_result = await _get_customer_history(tenant_slug, provider, args)
    base_data = json.loads(base_result)

    # If lookup was by channel+user_id, also include known_customers info
    if channel and channel_user_id and base_data.get("found"):
        try:
            headers = _tenant_headers(tenant_slug)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/contact_profiles",
                    headers=headers,
                    params={
                        "channel": f"eq.{channel}",
                        "channel_user_id": f"eq.{channel_user_id}",
                        "select": "default_vat,known_customers",
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
                if rows:
                    profile = rows[0]
                    base_data["default_vat"] = profile.get("default_vat")
                    base_data["known_customers"] = profile.get("known_customers") or []

                    # Also fetch memories for ALL known customer vats
                    known = profile.get("known_customers") or []
                    if isinstance(known, list):
                        extra_memories = []
                        existing_vats = set()
                        for p in base_data.get("profiles", []):
                            if p.get("vat"):
                                existing_vats.add(p["vat"])

                        for entry in known:
                            v = entry.get("vat", "")
                            if v and v not in existing_vats:
                                # Search for profiles with this vat
                                vat_resp = await client.get(
                                    f"{SUPABASE_URL}/rest/v1/contact_profiles",
                                    headers=headers,
                                    params={
                                        "vat": f"eq.{v}",
                                        "select": "id,channel,channel_user_id",
                                    },
                                )
                                vat_resp.raise_for_status()
                                vat_profiles = vat_resp.json()
                                for vp in vat_profiles:
                                    try:
                                        mems = await provider.search_memories_semantic(
                                            query="*",
                                            channel_user_id=vp["channel_user_id"],
                                            top_k=10,
                                        )
                                        for m in mems:
                                            m["source_channel"] = vp["channel"]
                                            m["source_vat"] = v
                                        extra_memories.extend(mems)
                                    except Exception:
                                        pass

                        if extra_memories:
                            existing_mems = base_data.get("memories", [])
                            existing_mems.extend(extra_memories)
                            base_data["memories"] = existing_mems
                            base_data["memories_count"] = len(existing_mems)

        except Exception as e:
            logger.warning(f"_get_customer_history_with_known enrichment error: {e}")

    return json.dumps(base_data, ensure_ascii=False, indent=2, default=str)


async def _link_contacts(
    tenant_slug: str, provider, args: dict
) -> str:
    """Handler for link_contacts tool."""
    import httpx

    channel_a = args["channel_a"]
    user_id_a = args["user_id_a"]
    channel_b = args["channel_b"]
    user_id_b = args["user_id_b"]

    if not tenant_slug:
        return json.dumps(
            {"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False
        )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Look up both profiles
            profiles = {}
            for label, ch, uid in [("a", channel_a, user_id_a), ("b", channel_b, user_id_b)]:
                resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/contact_profiles",
                    headers=_tenant_headers(tenant_slug),
                    params={
                        "channel": f"eq.{ch}",
                        "channel_user_id": f"eq.{uid}",
                        "select": "id,channel,channel_user_id,name,vat,org_id",
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
                if not rows:
                    return json.dumps(
                        {"error": f"No se encontro perfil para {ch}/{uid}"},
                        ensure_ascii=False,
                    )
                profiles[label] = rows[0]

            # Already linked?
            if (
                profiles["a"].get("org_id")
                and profiles["a"]["org_id"] == profiles["b"].get("org_id")
            ):
                return json.dumps(
                    {
                        "already_linked": True,
                        "org_id": profiles["a"]["org_id"],
                        "profiles": profiles,
                    },
                    ensure_ascii=False, indent=2, default=str,
                )

            # Call RPC to link
            rpc_resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/link_tenant_contacts",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "p_tenant_slug": tenant_slug,
                    "contact_id_a": profiles["a"]["id"],
                    "contact_id_b": profiles["b"]["id"],
                },
            )
            rpc_resp.raise_for_status()
            org_id = rpc_resp.json()

            return json.dumps(
                {
                    "linked": True,
                    "org_id": org_id,
                    "profile_a": {
                        "channel": channel_a,
                        "user_id": user_id_a,
                        "name": profiles["a"].get("name"),
                    },
                    "profile_b": {
                        "channel": channel_b,
                        "user_id": user_id_b,
                        "name": profiles["b"].get("name"),
                    },
                },
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"link_contacts error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _get_customer_history(
    tenant_slug: str, provider, args: dict
) -> str:
    """Handler for get_customer_history tool."""
    import httpx

    cedula_ruc = args.get("cedula_ruc", "").strip().replace("-", "").replace(" ", "")
    channel = args.get("channel", "")
    channel_user_id = args.get("channel_user_id", "")

    if not tenant_slug:
        return json.dumps(
            {"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False
        )

    if not cedula_ruc and not (channel and channel_user_id):
        return json.dumps(
            {"error": "Debe proporcionar cedula_ruc o channel+channel_user_id."},
            ensure_ascii=False,
        )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            all_profiles = []

            if cedula_ruc:
                # Search by vat across all channels
                resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/contact_profiles",
                    headers=_tenant_headers(tenant_slug),
                    params={
                        "vat": f"eq.{cedula_ruc}",
                        "select": "*",
                    },
                )
                resp.raise_for_status()
                all_profiles = resp.json()

                # Also get linked profiles via org_id
                org_ids = {p["org_id"] for p in all_profiles if p.get("org_id")}
                for oid in org_ids:
                    rpc_resp = await client.post(
                        f"{SUPABASE_URL}/rest/v1/rpc/get_tenant_org_contacts",
                        headers={
                            "apikey": SUPABASE_SERVICE_KEY,
                            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "p_tenant_slug": tenant_slug,
                            "p_org_id": oid,
                        },
                    )
                    rpc_resp.raise_for_status()
                    linked = rpc_resp.json()
                    # Merge without duplicates
                    existing_ids = {p["id"] for p in all_profiles}
                    for lp in linked:
                        if lp["id"] not in existing_ids:
                            all_profiles.append(lp)
                            existing_ids.add(lp["id"])

            elif channel and channel_user_id:
                # Look up this profile, get org_id, then get all linked
                resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/contact_profiles",
                    headers=_tenant_headers(tenant_slug),
                    params={
                        "channel": f"eq.{channel}",
                        "channel_user_id": f"eq.{channel_user_id}",
                        "select": "*",
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
                if not rows:
                    return json.dumps(
                        {"found": False, "message": "No se encontro perfil para este contacto."},
                        ensure_ascii=False,
                    )

                profile = rows[0]
                all_profiles = [profile]

                # If has org_id, get all linked profiles
                if profile.get("org_id"):
                    rpc_resp = await client.post(
                        f"{SUPABASE_URL}/rest/v1/rpc/get_tenant_org_contacts",
                        headers={
                            "apikey": SUPABASE_SERVICE_KEY,
                            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "p_tenant_slug": tenant_slug,
                            "p_org_id": profile["org_id"],
                        },
                    )
                    rpc_resp.raise_for_status()
                    linked = rpc_resp.json()
                    existing_ids = {p["id"] for p in all_profiles}
                    for lp in linked:
                        if lp["id"] not in existing_ids:
                            all_profiles.append(lp)
                            existing_ids.add(lp["id"])

            # Gather memories for each linked profile
            all_memories = []
            channels_found = []
            for p in all_profiles:
                channels_found.append(f"{p['channel']}/{p['channel_user_id']}")
                try:
                    memories = await provider.search_memories_semantic(
                        query="*",
                        channel_user_id=p["channel_user_id"],
                        top_k=20,
                    )
                    for m in memories:
                        m["source_channel"] = p["channel"]
                        m["source_user_id"] = p["channel_user_id"]
                    all_memories.extend(memories)
                except Exception as e:
                    logger.warning(
                        f"Failed to get memories for {p['channel']}/{p['channel_user_id']}: {e}"
                    )

            return json.dumps(
                {
                    "found": True,
                    "profiles_count": len(all_profiles),
                    "profiles": all_profiles,
                    "linked_channels": channels_found,
                    "memories_count": len(all_memories),
                    "memories": all_memories,
                },
                ensure_ascii=False, indent=2, default=str,
            )

    except Exception as e:
        logger.error(f"get_customer_history error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Inter-agent coordination (Supabase REST on public schema)
# ---------------------------------------------------------------------------

async def _coord_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _coord_send_message(tenant_id: str, agent_slug: str, args: dict) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/agent_messages",
            headers=await _coord_headers(),
            json={
                "tenant_id": tenant_id,
                "sender_agent": agent_slug,
                "recipient_agent": args["recipient_agent"],
                "content": args["content"],
                "message_type": args.get("message_type", "text"),
                "priority": args.get("priority", "normal"),
            },
        )
        resp.raise_for_status()
        msg = resp.json()

        # TODO(gap-5): Publish notification to Redis pub/sub so listening
        # agents can react faster (channel: f"agent:{args['recipient_agent']}").
        # Requires adding a redis client dependency (e.g. redis[hiredis]).
        # Implement when Hermes supports background listeners.

        return json.dumps({
            "status": "sent",
            "message_id": msg[0]["id"] if msg else "",
            "to": args["recipient_agent"],
        }, ensure_ascii=False)


async def _coord_get_messages(tenant_id: str, agent_slug: str, args: dict) -> str:
    import httpx
    limit = args.get("limit", 10)
    async with httpx.AsyncClient(timeout=10) as client:
        # Fetch undelivered messages
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/agent_messages",
            headers=await _coord_headers(),
            params={
                "tenant_id": f"eq.{tenant_id}",
                "recipient_agent": f"eq.{agent_slug}",
                "delivered": "eq.false",
                "order": "created_at.asc",
                "limit": str(limit),
            },
        )
        resp.raise_for_status()
        messages = resp.json()

        if not messages:
            return "No tienes mensajes nuevos."

        # Mark as delivered
        ids = [m["id"] for m in messages]
        for mid in ids:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/agent_messages",
                headers=await _coord_headers(),
                params={"id": f"eq.{mid}"},
                json={"delivered": True},
            )

        lines = []
        for m in messages:
            lines.append(
                f"[{m['priority']}] De {m['sender_agent']}: {m['content']} "
                f"(tipo: {m['message_type']})"
            )
        return f"{len(messages)} mensajes:\n" + "\n".join(lines)


async def _coord_create_task(tenant_id: str, agent_slug: str, args: dict) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/agent_tasks",
            headers=await _coord_headers(),
            json={
                "tenant_id": tenant_id,
                "title": args["title"],
                "description": args.get("description", ""),
                "assigned_to": args["assigned_to"],
                "created_by": agent_slug,
                "task_type": args.get("task_type", "general"),
                "priority": args.get("priority", "medium"),
                "input_data": args.get("input_data", {}),
            },
        )
        resp.raise_for_status()
        task = resp.json()
        task_id = task[0]["id"] if task else ""

        # Notify the assigned agent
        await client.post(
            f"{SUPABASE_URL}/rest/v1/agent_messages",
            headers=await _coord_headers(),
            json={
                "tenant_id": tenant_id,
                "sender_agent": agent_slug,
                "recipient_agent": args["assigned_to"],
                "content": f"Nueva tarea: {args['title']}",
                "message_type": "task_request",
                "priority": args.get("priority", "medium"),
                "ref_id": task_id,
            },
        )

        return json.dumps({
            "status": "created",
            "task_id": task_id,
            "assigned_to": args["assigned_to"],
        }, ensure_ascii=False)


async def _coord_update_task(tenant_id: str, agent_slug: str, args: dict) -> str:
    import httpx
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    update = {"status": args["status"], "updated_at": now}
    if args["status"] == "in_progress":
        update["started_at"] = now
    elif args["status"] in ("completed", "failed"):
        update["completed_at"] = now
    if "result_data" in args:
        update["result_data"] = args["result_data"]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.patch(
            f"{SUPABASE_URL}/rest/v1/agent_tasks",
            headers=await _coord_headers(),
            params={"id": f"eq.{args['task_id']}"},
            json=update,
        )
        resp.raise_for_status()
        rows = resp.json()

        # Notify creator on completion
        if rows and args["status"] in ("completed", "failed"):
            task = rows[0]
            await client.post(
                f"{SUPABASE_URL}/rest/v1/agent_messages",
                headers=await _coord_headers(),
                json={
                    "tenant_id": tenant_id,
                    "sender_agent": agent_slug,
                    "recipient_agent": task.get("created_by", ""),
                    "content": f"Tarea '{task.get('title')}' {args['status']}",
                    "message_type": "task_result",
                    "priority": "normal",
                    "ref_id": args["task_id"],
                },
            )

        return json.dumps({"status": "updated", "task_id": args["task_id"]}, ensure_ascii=False)


async def _coord_get_tasks(tenant_id: str, agent_slug: str, args: dict) -> str:
    import httpx
    status_filter = args.get("status", "pending")
    limit = args.get("limit", 10)

    params = {
        "tenant_id": f"eq.{tenant_id}",
        "assigned_to": f"eq.{agent_slug}",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if status_filter != "all":
        params["status"] = f"eq.{status_filter}"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/agent_tasks",
            headers=await _coord_headers(),
            params=params,
        )
        resp.raise_for_status()
        tasks = resp.json()

    if not tasks:
        return "No tienes tareas pendientes."
    lines = []
    for t in tasks:
        lines.append(
            f"[{t['priority']}] {t['title']} (tipo: {t['task_type']}, "
            f"de: {t['created_by']}, id: {t['id']})"
        )
    return f"{len(tasks)} tareas:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Knowledge Graph helpers
# ---------------------------------------------------------------------------

async def _kg_query(tenant_slug: str, args: dict) -> str:
    """Query knowledge facts about an entity."""
    import httpx

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    entity = args["entity"]
    predicate = args.get("predicate")
    as_of = args.get("as_of")

    headers = _tenant_headers(tenant_slug)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            params: dict = {
                "entity": f"eq.{entity}",
                "select": "*",
                "order": "valid_from.desc",
            }
            if predicate:
                params["predicate"] = f"eq.{predicate}"

            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_facts",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            facts = resp.json()

            # Filter by as_of date if provided
            if as_of and facts:
                from datetime import datetime, timezone
                try:
                    as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                except ValueError:
                    return json.dumps({"error": f"Formato de fecha invalido: {as_of}"}, ensure_ascii=False)

                filtered = []
                for f in facts:
                    vf = datetime.fromisoformat(f["valid_from"].replace("Z", "+00:00"))
                    vu = f.get("valid_until")
                    if vf <= as_of_dt:
                        if vu is None:
                            filtered.append(f)
                        else:
                            vu_dt = datetime.fromisoformat(vu.replace("Z", "+00:00"))
                            if vu_dt > as_of_dt:
                                filtered.append(f)
                facts = filtered

            if not facts:
                return json.dumps(
                    {"count": 0, "facts": [], "message": f"No hay hechos conocidos sobre '{entity}'."},
                    ensure_ascii=False,
                )

            # Separate active vs expired
            active = [f for f in facts if f.get("valid_until") is None]
            expired = [f for f in facts if f.get("valid_until") is not None]

            return json.dumps(
                {
                    "count": len(facts),
                    "active": len(active),
                    "expired": len(expired),
                    "facts": facts,
                },
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"kg_query error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _kg_add(tenant_slug: str, args: dict) -> str:
    """Add a knowledge fact."""
    import httpx

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    headers = _tenant_headers(tenant_slug)
    headers["Prefer"] = "return=representation"

    payload: dict = {
        "entity": args["entity"],
        "predicate": args["predicate"],
        "object": args["object"],
        "source": "agent",
    }
    if args.get("valid_from"):
        payload["valid_from"] = args["valid_from"]
    if args.get("source_memory_id"):
        payload["source_memory_id"] = args["source_memory_id"]
    if args.get("confidence") is not None:
        payload["confidence"] = args["confidence"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/knowledge_facts",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            fact = result[0] if isinstance(result, list) and result else result

            return json.dumps(
                {"success": True, "fact_id": fact.get("id", ""), "fact": fact},
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"kg_add error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _kg_invalidate(tenant_slug: str, args: dict) -> str:
    """Mark knowledge facts as no longer valid."""
    import httpx
    from datetime import datetime, timezone

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    headers = _tenant_headers(tenant_slug)
    headers["Prefer"] = "return=representation"

    now = datetime.now(timezone.utc).isoformat()

    params: dict = {
        "entity": f"eq.{args['entity']}",
        "predicate": f"eq.{args['predicate']}",
        "valid_until": "is.null",  # Only invalidate currently active facts
    }
    if args.get("object"):
        params["object"] = f"eq.{args['object']}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/knowledge_facts",
                headers=headers,
                params=params,
                json={"valid_until": now},
            )
            resp.raise_for_status()
            updated = resp.json()
            count = len(updated) if isinstance(updated, list) else 0

            return json.dumps(
                {"success": True, "invalidated_count": count, "facts": updated},
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"kg_invalidate error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _kg_timeline(tenant_slug: str, args: dict) -> str:
    """Get chronological timeline of all facts about an entity."""
    import httpx

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    headers = _tenant_headers(tenant_slug)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_facts",
                headers=headers,
                params={
                    "entity": f"eq.{args['entity']}",
                    "select": "*",
                    "order": "valid_from.asc",
                },
            )
            resp.raise_for_status()
            facts = resp.json()

            if not facts:
                return json.dumps(
                    {"count": 0, "timeline": [], "message": f"No hay timeline para '{args['entity']}'."},
                    ensure_ascii=False,
                )

            # Build human-readable timeline
            timeline = []
            for f in facts:
                entry = {
                    "from": f["valid_from"],
                    "until": f.get("valid_until", "vigente"),
                    "fact": f"{f['predicate']}: {f['object']}",
                    "confidence": f.get("confidence", 1.0),
                    "source": f.get("source", "agent"),
                    "id": f["id"],
                }
                timeline.append(entry)

            return json.dumps(
                {"count": len(timeline), "entity": args["entity"], "timeline": timeline},
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"kg_timeline error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# MemPalace: Wings & Rooms helpers
# ---------------------------------------------------------------------------

async def _list_wings(tenant_slug: str) -> str:
    """List all wings with contact counts."""
    import httpx

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    headers = _tenant_headers(tenant_slug)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Get all wings
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/memory_wings",
                headers=headers,
                params={"select": "*", "order": "name.asc"},
            )
            resp.raise_for_status()
            wings = resp.json()

            # Get contact counts per wing_id
            for wing in wings:
                count_resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/contact_profiles",
                    headers={**headers, "Prefer": "count=exact"},
                    params={
                        "wing_id": f"eq.{wing['id']}",
                        "select": "id",
                        "limit": "0",
                    },
                )
                # Extract count from content-range header
                content_range = count_resp.headers.get("content-range", "")
                if "/" in content_range:
                    total = content_range.split("/")[-1]
                    wing["contact_count"] = int(total) if total != "*" else 0
                else:
                    wing["contact_count"] = 0

            return json.dumps(
                {"count": len(wings), "wings": wings},
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"list_wings error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _list_rooms(tenant_slug: str, args: dict) -> str:
    """List rooms, optionally filtered by wing."""
    import httpx

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    headers = _tenant_headers(tenant_slug)
    wing_name = args.get("wing")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            params: dict = {
                "select": "*, memory_wings(name, icon)",
                "order": "name.asc",
            }

            # If wing filter provided, first resolve wing_id
            if wing_name:
                wing_resp = await client.get(
                    f"{SUPABASE_URL}/rest/v1/memory_wings",
                    headers=headers,
                    params={"name": f"eq.{wing_name}", "select": "id"},
                )
                wing_resp.raise_for_status()
                wing_rows = wing_resp.json()
                if not wing_rows:
                    return json.dumps(
                        {"error": f"Ala '{wing_name}' no encontrada."},
                        ensure_ascii=False,
                    )
                params["wing_id"] = f"eq.{wing_rows[0]['id']}"

            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/memory_rooms",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            rooms = resp.json()

            return json.dumps(
                {"count": len(rooms), "rooms": rooms},
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"list_rooms error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _assign_contact_wing(tenant_slug: str, args: dict) -> str:
    """Assign a contact to a wing."""
    import httpx

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    headers = _tenant_headers(tenant_slug)
    channel = args["channel"]
    channel_user_id = args["channel_user_id"]
    wing_name = args["wing"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Resolve wing_id
            wing_resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/memory_wings",
                headers=headers,
                params={"name": f"eq.{wing_name}", "select": "id,name"},
            )
            wing_resp.raise_for_status()
            wing_rows = wing_resp.json()
            if not wing_rows:
                return json.dumps(
                    {"error": f"Ala '{wing_name}' no encontrada. Alas disponibles: clientes, prospectos, proveedores, vip."},
                    ensure_ascii=False,
                )
            wing_id = wing_rows[0]["id"]

            # Update contact profile
            patch_headers = dict(headers)
            patch_headers["Prefer"] = "return=representation"
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/contact_profiles",
                headers=patch_headers,
                params={
                    "channel": f"eq.{channel}",
                    "channel_user_id": f"eq.{channel_user_id}",
                },
                json={"wing_id": wing_id},
            )
            resp.raise_for_status()
            updated = resp.json()

            if not updated:
                return json.dumps(
                    {"error": f"No se encontro perfil para {channel}/{channel_user_id}."},
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "success": True,
                    "contact": updated[0] if isinstance(updated, list) else updated,
                    "wing": wing_name,
                },
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"assign_contact_wing error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _classify_memory_room(tenant_slug: str, args: dict) -> str:
    """Classify a memory into a room."""
    import httpx

    if not tenant_slug:
        return json.dumps({"error": "No se pudo determinar el tenant_slug."}, ensure_ascii=False)

    headers = _tenant_headers(tenant_slug)
    memory_id = args["memory_id"]
    room = args["room"]
    wing = args.get("wing")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            update: dict = {"room": room}
            if wing:
                update["wing"] = wing

            patch_headers = dict(headers)
            patch_headers["Prefer"] = "return=representation"
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/contact_memories",
                headers=patch_headers,
                params={"id": f"eq.{memory_id}"},
                json=update,
            )
            resp.raise_for_status()
            updated = resp.json()

            if not updated:
                return json.dumps(
                    {"error": f"No se encontro memoria con id {memory_id}."},
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "success": True,
                    "memory": updated[0] if isinstance(updated, list) else updated,
                    "room": room,
                    "wing": wing,
                },
                ensure_ascii=False, indent=2, default=str,
            )
    except Exception as e:
        logger.error(f"classify_memory_room error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# MCP request handler
# ---------------------------------------------------------------------------

async def _handle_mcp_request(body: dict, ctx: dict | None = None) -> dict | None:
    """Process a single MCP JSON-RPC request."""
    req_id = body.get("id")
    method = body.get("method", "")
    ctx = ctx or {}

    if method == "initialize":
        return _make_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mcp-memory", "version": "1.0.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _make_response(req_id, {"tools": MCP_TOOLS})

    if method == "tools/call":
        tool_name = body["params"]["name"]
        args = body["params"].get("arguments", {})

        try:
            text = await _execute_tool(tool_name, args, ctx)
            return _make_response(req_id, {
                "content": [{"type": "text", "text": text}],
            })
        except Exception as e:
            import traceback
            logger.error(f"Tool {tool_name} error: {e}\n{traceback.format_exc()}")
            return _make_response(req_id, {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            })

    if method == "ping":
        return _make_response(req_id, {})

    return _make_error(req_id, -32601, f"Unknown method: {method}")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        f"MCP Memory Server starting — tenant={TENANT_SLUG}, "
        f"supabase={SUPABASE_URL}, ollama={OLLAMA_URL}"
    )
    yield
    _providers.clear()
    _slug_cache.clear()


app = FastAPI(
    title="MCP Memory Server",
    description="Shared contact memory for Hermes multi-agent architecture",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "tenant": TENANT_SLUG,
        "supabase": bool(SUPABASE_URL and SUPABASE_SERVICE_KEY),
        "ollama": OLLAMA_URL,
    }


# ---------------------------------------------------------------------------
# MCP StreamableHTTP endpoint (same pattern as mcp-server-odoo)
# ---------------------------------------------------------------------------

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """StreamableHTTP MCP endpoint. Handles JSON-RPC over HTTP POST."""
    body = await request.json()
    # Pass tenant/agent context from headers for multi-tenant coordination
    ctx = {
        "tenant_id": request.headers.get("x-tenant-id", TENANT_ID),
        "agent_slug": request.headers.get("x-agent-slug", ""),
    }
    result = await _handle_mcp_request(body, ctx)
    return JSONResponse(result or {"jsonrpc": "2.0"})


# ---------------------------------------------------------------------------
# MCP SSE transport (for clients that prefer SSE)
# ---------------------------------------------------------------------------

_sse_sessions: dict[str, asyncio.Queue] = {}


@app.get("/sse")
async def sse_endpoint(request: Request):
    """MCP SSE transport: client opens GET /sse, receives events."""
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue

    async def event_stream():
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

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/messages")
async def sse_messages_endpoint(request: Request):
    """MCP SSE transport: receives JSON-RPC requests via POST."""
    session_id = request.query_params.get("sessionId", "")
    queue = _sse_sessions.get(session_id)
    if not queue:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    body = await request.json()
    ctx = {
        "tenant_id": request.headers.get("x-tenant-id", TENANT_ID),
        "agent_slug": request.headers.get("x-agent-slug", ""),
    }
    response = await _handle_mcp_request(body, ctx)
    if response is not None:
        await queue.put(response)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# REST API (for direct HTTP access, debugging, dashboard)
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class ProfileRequest(BaseModel):
    channel: str
    channel_user_id: str
    name: str | None = None
    vat: str | None = None
    email: str | None = None
    phone: str | None = None
    odoo_partner_id: int | None = None


@app.get("/api/profile/{channel}/{channel_user_id}")
async def api_get_profile(channel: str, channel_user_id: str):
    provider = get_provider()
    profile = await provider.get_contact_profile(channel, channel_user_id)
    if not profile:
        return JSONResponse({"found": False}, status_code=404)
    return {"found": True, "profile": profile}


@app.post("/api/profile")
async def api_upsert_profile(req: ProfileRequest):
    provider = get_provider()
    result = await provider.upsert_contact_profile(
        channel=req.channel,
        channel_user_id=req.channel_user_id,
        name=req.name,
        vat=req.vat,
        email=req.email,
        phone=req.phone,
        odoo_partner_id=req.odoo_partner_id,
    )
    if result:
        return {"success": True, "profile": result}
    return JSONResponse({"success": False}, status_code=500)


class SearchRequest(BaseModel):
    query: str
    channel_user_id: str | None = None
    partner_id: int | None = None
    top_k: int = 5


@app.post("/api/memories/search")
async def api_search_memories(req: SearchRequest):
    provider = get_provider()
    results = await provider.search_memories_semantic(
        query=req.query,
        channel_user_id=req.channel_user_id,
        partner_id=req.partner_id,
        top_k=req.top_k,
    )
    return {"count": len(results), "memories": results}


@app.post("/api/conversations/search")
async def api_search_conversations(req: SearchRequest):
    provider = get_provider()
    results = await provider.search_conversations(
        query=req.query,
        channel_user_id=req.channel_user_id,
        top_k=req.top_k,
    )
    return {"count": len(results), "results": results}


@app.get("/api/context/{channel}/{channel_user_id}")
async def api_load_context(channel: str, channel_user_id: str):
    provider = get_provider()
    context = await provider.load_user_context(
        user_id=channel_user_id, channel=channel,
    )
    return {"context": context}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
