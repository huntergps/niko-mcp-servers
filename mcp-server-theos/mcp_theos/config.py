"""Settings for mcp-server-theos.

Same envelope as mcp-server-odoo: AES-256-GCM with the shared
``ODOO_ENCRYPTION_KEY`` (kept for continuity — see niko/api/crypto.py
for the rationale) plus the Supabase REST endpoint used to look up
the tenant's Velneo connection.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    supabase_service_key: str = ""
    odoo_encryption_key: str = ""

    # Postgres direct connection — used by the RAG (pgvector similarity
    # against ``tenant_<slug>.product_embeddings``). Same DSN that
    # rag-sync uses. PostgREST does NOT expose tenant schemas so we have
    # to go through psycopg directly.
    supabase_db_url: str = ""
    supabase_db_internal_url: str = ""

    # Embeddings provider — must match what rag-sync used to index the
    # tenant. ``bge-m3`` returns 1024-d vectors which is what the
    # tenant_<slug>.product_embeddings columns are sized for.
    ollama_url: str = "http://ollama:11434"
    embedding_model: str = "bge-m3"

    host: str = "0.0.0.0"
    port: int = 8086

    velneo_http_timeout: float = 20.0
    velneo_default_page_size: int = 200
    velneo_max_pages: int = 50

    # Cap for the per-search RAG fan-out: how many product codes we
    # enrich with a follow-up ``visor_datos`` call. Each enrichment is
    # one HTTP round-trip to the ERP, so keep this small.
    rag_max_enrich: int = 10

    niko_api_url: str = "http://niko:8080"
    niko_public_url: str = "https://niko.galapagos.tech"

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
