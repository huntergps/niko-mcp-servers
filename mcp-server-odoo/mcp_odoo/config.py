from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    supabase_service_key: str = ""
    odoo_encryption_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8085
    max_connections_per_tenant: int = 3
    xmlrpc_timeout: int = 15

    # RAG config
    ollama_url: str = "https://llama.galapagos.tech"
    embedding_model: str = "bge-m3"
    default_tenant_id: str = ""

    # RAG REST endpoint (env ``RAG_REST_URL``). The RAG embeddings store
    # (``product_embeddings`` / ``partner_embeddings`` tables + the
    # ``search_tenant_products`` / ``search_tenant_partners`` RPCs) is being
    # split off Supabase onto its own Postgres/PostgREST stack. Only those
    # RAG calls read this URL; everything else (OTP, tenants, knowledge_facts,
    # contact_profiles, PDFs, …) keeps using ``supabase_url``.
    #
    # Empty by default so that, when ``RAG_REST_URL`` is unset, the RAG calls
    # fall back to ``supabase_url`` via the ``rag_rest_url`` property below —
    # i.e. deploying this code alone is a NO-OP; the real cutover is setting
    # ``RAG_REST_URL`` in the container env. The service key / JWT secret is
    # shared by both stacks, so it is NOT duplicated here.
    rag_rest_url: str = ""

    # Single-tenant fallback (when no JWT)
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_user: str = ""
    odoo_password: str = ""

    # Niko backend (used by tools that need to dispatch through niko
    # channels, e.g. ``niko_send_sign_request`` which routes a
    # signature request to the customer via Telegram / WhatsApp).
    # In-cluster default points at the docker-compose service name;
    # override with the public URL for out-of-cluster callers.
    niko_api_url: str = "http://niko:8080"

    # ETA iter 81 — public base URL for niko-served PDFs (statements +
    # RIDEs). The MCP returns absolute URLs in the tool response so the
    # LLM can hand them off to the channel (WhatsApp / Telegram / web).
    # Defaults match the Caddy public hostname; override per-environment
    # via the ``NIKO_PUBLIC_URL`` env var (also used by other niko code).
    niko_public_url: str = "https://niko.galapagos.tech"

    model_config = {"env_prefix": "", "case_sensitive": False}

    def model_post_init(self, __context: object) -> None:
        # Default ``rag_rest_url`` to ``supabase_url`` when ``RAG_REST_URL`` is
        # not set, so RAG calls keep hitting the same Supabase endpoint as
        # before the split (NO-OP until the operator sets ``RAG_REST_URL``).
        if not self.rag_rest_url:
            self.rag_rest_url = self.supabase_url


settings = Settings()
