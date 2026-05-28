"""AES-256-GCM helper compatible with niko.api.crypto.

Tenant secrets (Velneo ``X-API-Key``, etc.) are stored in
``public.tenants.erp_api_key_encrypted`` as ``base64(nonce||ciphertext)``
encrypted with the 32-byte key in :env:`ODOO_ENCRYPTION_KEY`. The env
var name is shared with mcp-server-odoo for continuity — the same key
ring decrypts both Odoo and Velneo credentials.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_key() -> bytes:
    key_hex = os.environ.get("ODOO_ENCRYPTION_KEY", "")
    if not key_hex:
        raise RuntimeError("ODOO_ENCRYPTION_KEY not set")
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise RuntimeError(
            f"ODOO_ENCRYPTION_KEY must decode to 32 bytes (got {len(key)})"
        )
    return key


def decrypt_secret(token: str) -> str:
    """Inverse of ``niko.api.crypto.encrypt_secret``."""
    if not token:
        return ""
    aesgcm = AESGCM(_get_key())
    raw = base64.b64decode(token.encode())
    if len(raw) < 13:
        raise ValueError("ciphertext too short to contain 12-byte nonce")
    nonce, ciphertext = raw[:12], raw[12:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode()
