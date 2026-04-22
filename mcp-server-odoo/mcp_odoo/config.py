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

    # Single-tenant fallback (when no JWT)
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_user: str = ""
    odoo_password: str = ""

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
