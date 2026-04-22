import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import os as _os


def _get_key() -> bytes:
    """Get encryption key from environment (32 bytes hex)."""
    key_hex = _os.environ.get("ODOO_ENCRYPTION_KEY", "")
    if not key_hex:
        raise ValueError("ODOO_ENCRYPTION_KEY not set")
    return bytes.fromhex(key_hex)


def encrypt_credentials(plaintext: str) -> str:
    """Encrypt Odoo credentials with AES-256-GCM.

    Returns: base64(nonce + ciphertext + tag)
    """
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def decrypt_credentials(encrypted: str) -> str:
    """Decrypt Odoo credentials from AES-256-GCM.

    Input: base64(nonce + ciphertext + tag)
    """
    key = _get_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
