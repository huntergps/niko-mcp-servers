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

    host: str = "0.0.0.0"
    port: int = 8086

    velneo_http_timeout: float = 20.0
    velneo_default_page_size: int = 200
    velneo_max_pages: int = 50

    niko_api_url: str = "http://niko:8080"
    niko_public_url: str = "https://niko.galapagos.tech"

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
