"""Supabase Memory Provider for Hermes Agent.

Enables shared memory across agents of the same tenant via Supabase pgvector.
Implements the pluggable memory interface from Hermes v0.7.0.

Three memory layers:
1. Contact Profiles -- identity shared across all agents
2. Contact Memories -- facts learned from conversations (Mem0 pattern)
3. Conversation RAG -- semantic search across all agent conversations

All data is scoped by tenant_slug (tenant schema in Supabase).
Embeddings use bge-m3 (1024d) via Ollama.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://localhost:8000")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TENANT_SLUG = os.environ.get("TENANT_SLUG", "")
TENANT_ID = os.environ.get("TENANT_ID", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b")

# Supabase REST helpers
_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def _schema() -> str:
    """Return the tenant schema name."""
    return f"tenant_{TENANT_SLUG}"


def _headers_with_schema() -> dict:
    """Headers that target the tenant schema via PostgREST."""
    h = dict(_HEADERS)
    # PostgREST uses Accept-Profile / Content-Profile for schema routing
    h["Accept-Profile"] = _schema()
    h["Content-Profile"] = _schema()
    return h


# ---------------------------------------------------------------------------
# Embedding generation via Ollama
# ---------------------------------------------------------------------------

async def _get_embedding(text: str) -> list[float]:
    """Generate a 1024-d embedding using Ollama bge-m3."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBEDDING_MODEL, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        # Ollama returns {"embeddings": [[...]]} for /api/embed
        embeddings = data.get("embeddings") or [data.get("embedding", [])]
        return embeddings[0]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# LLM helpers for memory extraction (Mem0 pattern)
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """Analyze this conversation and extract key facts about the customer.
Return ONLY a JSON array of objects, each with "memory" (the fact) and "category".
Categories: personal, preference, professional, intent, service, general.

Rules:
- Extract facts about the CUSTOMER, not the agent.
- Be specific: "Prefiere envio a domicilio" not "Habló sobre envios".
- Include names, IDs, preferences, complaints, purchase patterns.
- If no facts can be extracted, return [].
- Max 10 facts per conversation.

Conversation:
{conversation}

JSON array:"""


async def _extract_memories_llm(messages: list[dict]) -> list[dict]:
    """Use LLM to extract factual memories from conversation messages."""
    # Format conversation
    lines = []
    for msg in messages:
        role = msg.get("role", msg.get("direction", "unknown"))
        content = msg.get("content", msg.get("message_text", ""))
        lines.append(f"{role}: {content}")
    conversation_text = "\n".join(lines[-20:])  # Last 20 messages max

    if len(conversation_text.strip()) < 30:
        return []

    prompt = _EXTRACT_PROMPT.format(conversation=conversation_text)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": LLM_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 1024},
                },
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()

            # Parse JSON — handle common LLM output quirks
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            if not raw.startswith("["):
                # Try to find array in response
                start = raw.find("[")
                end = raw.rfind("]")
                if start >= 0 and end > start:
                    raw = raw[start : end + 1]
                else:
                    return []

            memories = json.loads(raw)
            if not isinstance(memories, list):
                return []
            return [
                m for m in memories
                if isinstance(m, dict) and "memory" in m
            ]
    except Exception as e:
        logger.warning(f"Memory extraction failed: {e}")
        return []


# ===================================================================
# SupabaseMemoryProvider — Hermes pluggable memory interface
# ===================================================================


class SupabaseMemoryProvider:
    """Shared memory provider using Supabase pgvector.

    Hermes calls:
      - load_user_context(user_id, channel) -> str  (before LLM call)
      - save_conversation(user_id, channel, messages, agent_slug)  (after reply)
      - search_history(query, user_id) -> list  (for RAG tools)
    """

    def __init__(
        self,
        supabase_url: str | None = None,
        service_key: str | None = None,
        tenant_slug: str | None = None,
        tenant_id: str | None = None,
        ollama_url: str | None = None,
    ):
        self.supabase_url = supabase_url or SUPABASE_URL
        self.service_key = service_key or SUPABASE_SERVICE_KEY
        self.tenant_slug = tenant_slug or TENANT_SLUG
        self.tenant_id = tenant_id or TENANT_ID
        self.ollama_url = ollama_url or OLLAMA_URL
        self._schema = f"tenant_{self.tenant_slug}"

    def _headers(self) -> dict:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
            "Accept-Profile": self._schema,
            "Content-Profile": self._schema,
        }

    # ------------------------------------------------------------------
    # 1. Contact Profiles
    # ------------------------------------------------------------------

    async def get_contact_profile(
        self, channel: str, channel_user_id: str
    ) -> dict | None:
        """Load contact profile from tenant schema."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.supabase_url}/rest/v1/contact_profiles"
                f"?channel=eq.{channel}&channel_user_id=eq.{channel_user_id}"
                f"&limit=1",
                headers=self._headers(),
            )
            if resp.status_code == 200 and resp.json():
                return resp.json()[0]
        return None

    async def get_contact_by_vat(self, vat: str) -> dict | None:
        """Find contact profile by cedula/RUC."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.supabase_url}/rest/v1/contact_profiles"
                f"?vat=eq.{vat}&limit=1",
                headers=self._headers(),
            )
            if resp.status_code == 200 and resp.json():
                return resp.json()[0]
        return None

    async def upsert_contact_profile(
        self,
        channel: str,
        channel_user_id: str,
        *,
        name: str | None = None,
        vat: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        odoo_partner_id: int | None = None,
        agent_id: str | None = None,
    ) -> dict | None:
        """Create or update a contact profile (shared across all agents)."""
        payload: dict = {
            "channel": channel,
            "channel_user_id": channel_user_id,
            "last_interaction_at": datetime.now(timezone.utc).isoformat(),
        }
        if name is not None:
            payload["name"] = name
        if vat is not None:
            payload["vat"] = vat
        if email is not None:
            payload["email"] = email
        if phone is not None:
            payload["phone"] = phone
        if odoo_partner_id is not None:
            payload["odoo_partner_id"] = odoo_partner_id
        if agent_id is not None:
            payload["last_agent_id"] = agent_id

        headers = self._headers()
        # PostgREST upsert via Prefer header
        headers["Prefer"] = "return=representation,resolution=merge-duplicates"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.supabase_url}/rest/v1/contact_profiles",
                headers=headers,
                json=payload,
            )
            if resp.status_code in (200, 201) and resp.json():
                return resp.json()[0] if isinstance(resp.json(), list) else resp.json()
            logger.error(f"Upsert contact failed: {resp.status_code} {resp.text[:300]}")
        return None

    # ------------------------------------------------------------------
    # 2. Contact Memories (Mem0 pattern — public schema)
    # ------------------------------------------------------------------

    async def get_contact_memories(
        self,
        channel_user_id: str,
        *,
        partner_id: int | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Load all memories for a contact (by channel_user_id or partner_id)."""
        headers = dict(self._headers())
        # contact_memories lives in PUBLIC schema
        headers["Accept-Profile"] = "public"

        filters = (
            f"?tenant_id=eq.{self.tenant_id}"
            f"&is_deleted=eq.false"
            f"&order=created_at.desc"
            f"&limit={limit}"
        )
        if partner_id:
            filters += f"&or=(channel_user_id.eq.{channel_user_id},partner_id.eq.{partner_id})"
        else:
            filters += f"&channel_user_id=eq.{channel_user_id}"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.supabase_url}/rest/v1/contact_memories{filters}",
                headers=headers,
            )
            if resp.status_code == 200:
                memories = resp.json()
                # Touch access counters (fire-and-forget)
                if memories:
                    ids = [m["id"] for m in memories]
                    try:
                        await client.post(
                            f"{self.supabase_url}/rest/v1/rpc/touch_contact_memories",
                            headers=headers,
                            json={"memory_ids": ids},
                        )
                    except Exception:
                        pass
                return memories
        return []

    async def search_memories_semantic(
        self,
        query: str,
        channel_user_id: str | None = None,
        partner_id: int | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """Semantic search over contact memories via RPC."""
        embedding = await _get_embedding(query)

        headers = dict(self._headers())
        headers["Accept-Profile"] = "public"
        headers["Content-Profile"] = "public"

        payload = {
            "query_embedding": embedding,
            "p_tenant_id": self.tenant_id,
            "match_count": top_k,
            "similarity_threshold": 0.6,
        }
        if channel_user_id:
            payload["p_channel_user_id"] = channel_user_id
        if partner_id:
            payload["p_partner_id"] = partner_id

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.supabase_url}/rest/v1/rpc/match_contact_memories",
                headers=headers,
                json=payload,
            )
            if resp.status_code == 200:
                return resp.json()
        return []

    async def save_memory(
        self,
        channel: str,
        channel_user_id: str,
        memory: str,
        category: str = "general",
        *,
        partner_id: int | None = None,
        agent_slug: str = "",
        trace_id: str = "",
    ) -> dict | None:
        """Save a single memory with deduplication."""
        memory_hash = _md5(memory.lower().strip())

        headers = dict(self._headers())
        headers["Accept-Profile"] = "public"
        headers["Content-Profile"] = "public"

        # Check for duplicate
        async with httpx.AsyncClient(timeout=10) as client:
            dup_resp = await client.get(
                f"{self.supabase_url}/rest/v1/contact_memories"
                f"?tenant_id=eq.{self.tenant_id}"
                f"&channel_user_id=eq.{channel_user_id}"
                f"&memory_hash=eq.{memory_hash}"
                f"&is_deleted=eq.false"
                f"&limit=1",
                headers=headers,
            )
            if dup_resp.status_code == 200 and dup_resp.json():
                logger.debug(f"Duplicate memory skipped: {memory[:60]}")
                return dup_resp.json()[0]

        # Generate embedding
        embedding = await _get_embedding(memory)

        # Check for semantic near-duplicate (similarity > 0.95)
        near_dups = await self.search_memories_semantic(
            memory, channel_user_id=channel_user_id, top_k=1
        )
        if near_dups and near_dups[0].get("similarity", 0) > 0.95:
            logger.debug(f"Semantic near-duplicate skipped: {memory[:60]}")
            return near_dups[0]

        payload = {
            "tenant_id": self.tenant_id,
            "channel": channel,
            "channel_user_id": channel_user_id,
            "memory": memory,
            "memory_hash": memory_hash,
            "category": category,
            "source_agent": agent_slug,
            "source_trace_id": trace_id,
            "embedding": embedding,
        }
        if partner_id is not None:
            payload["partner_id"] = partner_id

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.supabase_url}/rest/v1/contact_memories",
                headers=headers,
                json=payload,
            )
            if resp.status_code in (200, 201):
                result = resp.json()

                # Log to history
                if isinstance(result, list) and result:
                    result = result[0]
                try:
                    hist_headers = dict(headers)
                    hist_headers["Prefer"] = "return=minimal"
                    await client.post(
                        f"{self.supabase_url}/rest/v1/contact_memory_history",
                        headers=hist_headers,
                        json={
                            "memory_id": result["id"],
                            "event": "ADD",
                            "new_memory": memory,
                        },
                    )
                except Exception:
                    pass

                return result
            logger.error(f"Save memory failed: {resp.status_code} {resp.text[:300]}")
        return None

    # ------------------------------------------------------------------
    # 3. Conversation Embeddings (RAG across agents)
    # ------------------------------------------------------------------

    async def search_conversations(
        self,
        query: str,
        channel_user_id: str | None = None,
        channel: str | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Semantic search over conversation history across all agents."""
        embedding = await _get_embedding(query)

        payload: dict = {
            "query_embedding": embedding,
            "match_count": top_k,
            "match_threshold": 0.3,
        }
        if channel_user_id:
            payload["p_channel_user_id"] = channel_user_id
        if channel:
            payload["p_channel"] = channel

        headers = self._headers()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.supabase_url}/rest/v1/rpc/search_conversations",
                headers=headers,
                json=payload,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.error(f"search_conversations failed: {resp.status_code} {resp.text[:300]}")
        return []

    async def save_conversation_embedding(
        self,
        message_id: str | None,
        agent_id: str | None,
        channel: str,
        channel_user_id: str,
        direction: str,
        content: str,
        summary: str | None = None,
    ) -> dict | None:
        """Save a single message embedding for conversation RAG."""
        if len(content.strip()) < 10:
            return None

        embedding = await _get_embedding(content)

        payload = {
            "channel": channel,
            "channel_user_id": channel_user_id,
            "direction": direction,
            "content": content,
            "embedding": embedding,
        }
        if message_id:
            payload["message_id"] = message_id
        if agent_id:
            payload["agent_id"] = agent_id
        if summary:
            payload["summary"] = summary

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.supabase_url}/rest/v1/conversation_embeddings",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                return result[0] if isinstance(result, list) else result
        return None

    # ------------------------------------------------------------------
    # Hermes interface methods
    # ------------------------------------------------------------------

    async def load_user_context(self, user_id: str, channel: str) -> str:
        """Load contact profile + memories for injection into LLM context.

        Called by Hermes before each LLM call to enrich the system prompt.
        Returns a formatted string ready for injection.
        """
        parts = []

        # 1. Load contact profile
        profile = await self.get_contact_profile(channel, user_id)
        if profile:
            parts.append("=== PERFIL DEL CONTACTO ===")
            if profile.get("name"):
                parts.append(f"Nombre: {profile['name']}")
            if profile.get("vat"):
                parts.append(f"Cedula/RUC: {profile['vat']}")
            if profile.get("email"):
                parts.append(f"Email: {profile['email']}")
            if profile.get("phone"):
                parts.append(f"Telefono: {profile['phone']}")
            if profile.get("odoo_partner_id"):
                parts.append(f"ID Odoo: {profile['odoo_partner_id']}")
            if profile.get("verified_level", 0) > 0:
                parts.append(f"Nivel verificacion: {profile['verified_level']}")
            if profile.get("preferences"):
                prefs = profile["preferences"]
                if isinstance(prefs, dict) and prefs:
                    parts.append(f"Preferencias: {json.dumps(prefs, ensure_ascii=False)}")
            partner_id = profile.get("odoo_partner_id")
        else:
            partner_id = None

        # 2. Load contact memories
        memories = await self.get_contact_memories(
            user_id, partner_id=partner_id, limit=15
        )
        if memories:
            parts.append("")
            parts.append("=== DATOS CONOCIDOS DEL CONTACTO ===")
            for m in memories:
                cat = m.get("category", "")
                src = m.get("source_agent", "")
                text = m.get("memory", "")
                tag = f"[{cat}]" if cat else ""
                source = f" (via {src})" if src else ""
                parts.append(f"- {tag} {text}{source}")

        if not parts:
            return ""

        return "\n".join(parts)

    async def save_conversation(
        self,
        user_id: str,
        channel: str,
        messages: list[dict],
        agent_slug: str,
        *,
        agent_id: str | None = None,
        trace_id: str = "",
    ):
        """Save conversation: embed messages + extract memories.

        Called by Hermes after each conversation turn or at session end.
        """
        # 1. Save conversation embeddings for RAG
        for msg in messages:
            direction = msg.get("role", msg.get("direction", "unknown"))
            content = msg.get("content", msg.get("message_text", ""))
            msg_id = msg.get("id")
            if content and len(content.strip()) >= 10:
                await self.save_conversation_embedding(
                    message_id=msg_id,
                    agent_id=agent_id,
                    channel=channel,
                    channel_user_id=user_id,
                    direction=direction,
                    content=content,
                )

        # 2. Extract and save memories (Mem0 pattern)
        extracted = await _extract_memories_llm(messages)
        profile = await self.get_contact_profile(channel, user_id)
        partner_id = profile.get("odoo_partner_id") if profile else None

        for fact in extracted:
            memory_text = fact.get("memory", "").strip()
            category = fact.get("category", "general")
            if memory_text:
                await self.save_memory(
                    channel=channel,
                    channel_user_id=user_id,
                    memory=memory_text,
                    category=category,
                    partner_id=partner_id,
                    agent_slug=agent_slug,
                    trace_id=trace_id,
                )

        logger.info(
            f"Saved {len(messages)} embeddings, extracted {len(extracted)} memories "
            f"for {channel}:{user_id} via {agent_slug}"
        )

    async def search_history(self, query: str, user_id: str) -> list[dict]:
        """Semantic search across conversation history.

        Called by Hermes RAG tools or directly by agents.
        Returns list of relevant conversation snippets.
        """
        results = await self.search_conversations(
            query=query, channel_user_id=user_id, top_k=10
        )
        return [
            {
                "content": r.get("content", ""),
                "direction": r.get("direction", ""),
                "agent_id": r.get("agent_id"),
                "similarity": r.get("similarity", 0),
                "created_at": r.get("created_at", ""),
            }
            for r in results
        ]
