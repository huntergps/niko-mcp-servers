"""Embed + pgvector similarity for the MCP search_products tool.

The niko side already indexed tenant_<slug>.product_embeddings via
``services/rag-sync/reindex_velneo.py``. Here we just embed the query
through Ollama (same model the indexer used) and pull top-K rows.

We intentionally keep the embedding call lean — no provider switching,
no fallbacks, no fastembed — because the indexer pinned bge-m3 1024d
and any drift would silently give garbage results.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp_theos.config import settings
from mcp_theos.db import similar_partners, similar_products

logger = logging.getLogger(__name__)


async def embed_query(text: str) -> list[float]:
    """Single-text embedding via Ollama's batch endpoint.

    Hot path is cached for 1 hour (see ``mcp_theos.cache``) — the same
    customer query typed twice in close succession (typical Lila
    re-ask pattern) does NOT round-trip to Ollama the second time.
    """
    clean = (text or "").strip()
    if not clean:
        raise ValueError("empty query")
    from mcp_theos.cache import embedding_cache, make_embedding_key
    key = make_embedding_key(settings.embedding_model, clean)
    cached = embedding_cache.get(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{settings.ollama_url}/api/embed",
            json={"model": settings.embedding_model, "input": [clean]},
        )
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings") or []
    if not embeddings or not isinstance(embeddings[0], list):
        raise RuntimeError(
            f"ollama returned no embedding for {settings.embedding_model!r}"
        )
    embedding_cache.set(key, embeddings[0])
    return embeddings[0]


async def product_codes_by_similarity(
    schema: str, query: str, *, limit: int = 10,
) -> list[dict[str, Any]]:
    """Embed ``query`` and return top-K rows of ``product_embeddings``.

    Each entry has ``odoo_id`` (Velneo product PK), ``code`` (CODIGO),
    ``name`` and a similarity score in [0..1].
    """
    vec = await embed_query(query)
    return similar_products(schema, vec, limit=limit)


async def partner_matches_by_similarity(
    schema: str, query: str, *, limit: int = 5,
) -> list[dict[str, Any]]:
    """Embed ``query`` and return top-K rows of ``partner_embeddings``."""
    vec = await embed_query(query)
    return similar_partners(schema, vec, limit=limit)
