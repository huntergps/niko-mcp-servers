"""Tests for AES-256-GCM credential encryption."""

import os
import pytest


@pytest.fixture(autouse=True)
def set_encryption_key(monkeypatch):
    """Set a test encryption key (32 bytes = 64 hex chars)."""
    test_key = os.urandom(32).hex()
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", test_key)


class TestEncryption:

    def test_encrypt_decrypt_roundtrip(self):
        from mcp_odoo.auth.encryption import encrypt_credentials, decrypt_credentials
        plaintext = "my_odoo_password_123!"
        encrypted = encrypt_credentials(plaintext)
        decrypted = decrypt_credentials(encrypted)
        assert decrypted == plaintext

    def test_encrypted_is_different_from_plaintext(self):
        from mcp_odoo.auth.encryption import encrypt_credentials
        plaintext = "secret_password"
        encrypted = encrypt_credentials(plaintext)
        assert encrypted != plaintext

    def test_different_encryptions_produce_different_ciphertext(self):
        from mcp_odoo.auth.encryption import encrypt_credentials
        plaintext = "same_password"
        enc1 = encrypt_credentials(plaintext)
        enc2 = encrypt_credentials(plaintext)
        assert enc1 != enc2

    def test_decrypt_with_wrong_key_fails(self, monkeypatch):
        from mcp_odoo.auth.encryption import encrypt_credentials, decrypt_credentials
        plaintext = "test_password"
        encrypted = encrypt_credentials(plaintext)

        # Change the key
        monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())

        with pytest.raises(Exception):
            decrypt_credentials(encrypted)

    def test_empty_string(self):
        from mcp_odoo.auth.encryption import encrypt_credentials, decrypt_credentials
        encrypted = encrypt_credentials("")
        decrypted = decrypt_credentials(encrypted)
        assert decrypted == ""

    def test_unicode_password(self):
        from mcp_odoo.auth.encryption import encrypt_credentials, decrypt_credentials
        plaintext = "contraseña_ñ_áéíóú_$€"
        encrypted = encrypt_credentials(plaintext)
        decrypted = decrypt_credentials(encrypted)
        assert decrypted == plaintext
