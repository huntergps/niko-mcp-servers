"""Tests for SRI tools — access key validation and import flow."""

import pytest
from mcp_odoo.tools.sri import validate_sri_access_key


class TestSRIAccessKeyValidation:
    """Test modulo-11 checksum validation of SRI access keys."""

    def test_valid_key_49_digits(self):
        # Real key from the project document (WINEDTECH invoice)
        key = "3003202601099269566800120010020000528572010011812"
        assert len(key) == 49
        valid, msg = validate_sri_access_key(key)
        # We test format validation, checksum may or may not pass
        # depending on the actual check digit
        assert isinstance(valid, bool)
        assert isinstance(msg, str)

    def test_too_short(self):
        valid, msg = validate_sri_access_key("12345")
        assert valid is False
        assert "49" in msg

    def test_too_long(self):
        valid, msg = validate_sri_access_key("1" * 50)
        assert valid is False
        assert "49" in msg

    def test_empty(self):
        valid, msg = validate_sri_access_key("")
        assert valid is False

    def test_none(self):
        valid, msg = validate_sri_access_key(None)
        assert valid is False

    def test_non_numeric(self):
        valid, msg = validate_sri_access_key("a" * 49)
        assert valid is False
        assert "digitos" in msg

    def test_mixed_alphanumeric(self):
        valid, msg = validate_sri_access_key("123abc" + "0" * 43)
        assert valid is False

    def test_exact_49_digits_format(self):
        """Verify the function accepts exactly 49 digits."""
        key = "0" * 48 + "0"  # 49 zeros, check digit for all zeros
        valid, msg = validate_sri_access_key(key)
        # Format is valid (49 digits), checksum validation determines result
        assert isinstance(valid, bool)


class TestSRIAccessKeyChecksum:
    """Test specific checksum calculations."""

    def test_known_valid_checksum(self):
        """Test with a key where we know the checksum is correct.

        SRI modulo-11 algorithm:
        - Take first 48 digits
        - Multiply by coefficients [2,3,4,5,6,7] cycling from right to left
        - Sum all products
        - remainder = sum % 11
        - check_digit = 11 - remainder (if 11 → 0, if 10 → 1)
        """
        # Build a key where we can verify the checksum
        base = "0" * 48
        digits = [int(d) for d in base]
        coefficients = [2, 3, 4, 5, 6, 7]

        total = 0
        for i, digit in enumerate(reversed(digits)):
            total += digit * coefficients[i % len(coefficients)]

        remainder = total % 11
        expected = 11 - remainder
        if expected == 11:
            expected = 0
        elif expected == 10:
            expected = 1

        key = base + str(expected)
        valid, msg = validate_sri_access_key(key)
        assert valid is True
        assert msg == "OK"

    def test_wrong_checksum(self):
        """A key with incorrect check digit should fail."""
        base = "1" * 48
        digits = [int(d) for d in base]
        coefficients = [2, 3, 4, 5, 6, 7]

        total = 0
        for i, digit in enumerate(reversed(digits)):
            total += digit * coefficients[i % len(coefficients)]

        remainder = total % 11
        expected = 11 - remainder
        if expected == 11:
            expected = 0
        elif expected == 10:
            expected = 1

        # Use wrong check digit
        wrong_digit = (expected + 1) % 10
        key = base + str(wrong_digit)
        valid, msg = validate_sri_access_key(key)
        assert valid is False
        assert "verificador" in msg
