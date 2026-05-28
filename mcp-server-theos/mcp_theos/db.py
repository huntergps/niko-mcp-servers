"""Postgres access for the RAG path.

PostgREST does NOT expose tenant schemas (PGRST106 ``Invalid schema``),
so the only way to read ``tenant_<slug>.product_embeddings`` from the
MCP container is via a direct psycopg connection. We mirror the same
DSN convention used by ``services/rag-sync/tenant_db.py``.

The vector column is rendered as a text literal (``[0.1, 0.2, ...]``)
and Postgres' implicit cast turns it into ``vector(1024)`` — that
avoids registering pgvector's psycopg adapter for a single SELECT.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg

from mcp_theos.config import settings

log = logging.getLogger(__name__)

# tenant_<slug> identifier guard — same regex the niko side uses.
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def _db_url() -> str:
    url = settings.supabase_db_url or settings.supabase_db_internal_url
    if not url:
        raise RuntimeError(
            "mcp-theos: neither SUPABASE_DB_URL nor "
            "SUPABASE_DB_INTERNAL_URL is set — RAG path disabled"
        )
    return url


@contextmanager
def tenant_conn(schema: str) -> Iterator[psycopg.Connection]:
    """Open a connection with ``search_path`` set to the tenant schema."""
    safe = _validate_ident(schema)
    conn = psycopg.connect(_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{safe}", public')
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _to_vector_literal(vec: list[float]) -> str:
    """Render a Python float sequence into the pgvector text literal."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def similar_products(
    schema: str,
    query_embedding: list[float],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Top-K nearest products from ``tenant_<schema>.product_embeddings``.

    Returns dicts with ``code``, ``name``, ``odoo_id`` and a normalized
    ``similarity`` score in [0..1] (1 = identical). The cosine distance
    operator ``<=>`` is used; ``1 - distance`` gives similarity.
    """
    if not query_embedding:
        return []
    safe_schema = _validate_ident(schema)
    vec = _to_vector_literal(query_embedding)
    sql = f'''
        SELECT odoo_id,
               name,
               code,
               (1 - (embedding <=> %s::vector)) AS similarity
          FROM "{safe_schema}"."product_embeddings"
         ORDER BY embedding <=> %s::vector
         LIMIT %s
    '''
    with tenant_conn(safe_schema) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (vec, vec, int(limit)))
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def similar_partners(
    schema: str,
    query_embedding: list[float],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Top-K nearest partners from ``tenant_<schema>.partner_embeddings``."""
    if not query_embedding:
        return []
    safe_schema = _validate_ident(schema)
    vec = _to_vector_literal(query_embedding)
    sql = f'''
        SELECT odoo_id,
               name,
               vat,
               (1 - (embedding <=> %s::vector)) AS similarity
          FROM "{safe_schema}"."partner_embeddings"
         ORDER BY embedding <=> %s::vector
         LIMIT %s
    '''
    with tenant_conn(safe_schema) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (vec, vec, int(limit)))
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
