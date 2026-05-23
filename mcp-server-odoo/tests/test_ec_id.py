"""Tests for the Ecuadorian cedula / RUC validator.

Real-world test data was sourced from the production Tecnosmart Odoo database
(``res.partner.vat``) to make sure the validator accepts every RUC that the
SRI itself accepts. See test docstrings for partner IDs.
"""

import pytest

from mcp_odoo.tools import ec_id


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalize:
    @pytest.mark.parametrize("raw, clean", [
        ("0992624310001", "0992624310001"),
        (" 0992624310001 ", "0992624310001"),
        ("099-2624310-001", "0992624310001"),
        ("099.262.4310.001", "0992624310001"),
        ("0992 6243 1000 1", "0992624310001"),
        ("RUC: 0992624310001", "0992624310001"),
        ("ruc:0992624310001", "0992624310001"),
        ("", ""),
        (None, ""),
        ("abc", ""),
    ])
    def test_normalization(self, raw, clean):
        assert ec_id.normalize(raw) == clean


# ---------------------------------------------------------------------------
# Cedula (10 digits)
# ---------------------------------------------------------------------------

class TestCedula:
    # Real cedulas from Tecnosmart res.partner.vat
    VALID = [
        "1205296534",  # RODRIGUEZ LIMA GALO DAVID
        "0958678427",  # ESPINOZA VIVAR DIANA
        "0941884496",  # TORRES MERCHAN NIURKA
        "1206006486",  # LEDESMA NUÑEZ GALO
    ]

    @pytest.mark.parametrize("cedula", VALID)
    def test_valid_real_cedulas(self, cedula):
        res = ec_id.validate(cedula)
        assert res["valid"], res["reason"]
        assert res["type"] == "cedula"
        assert res["normalized"] == cedula
        assert res["reason"] is None

    def test_valid_with_dashes_and_spaces(self):
        res = ec_id.validate("120-529-6534")
        assert res["valid"]
        assert res["type"] == "cedula"
        assert res["normalized"] == "1205296534"

    def test_invalid_dv(self):
        # Flip last digit
        res = ec_id.validate("1205296533")
        assert not res["valid"]
        assert "verificador" in res["reason"].lower()

    def test_invalid_province(self):
        res = ec_id.validate("9905296534")
        assert not res["valid"]
        assert "provincia" in res["reason"].lower()

    def test_third_digit_too_high(self):
        # third = 7 → must be < 6 for cedula
        res = ec_id.validate("1275296534")
        assert not res["valid"]
        assert "tercer" in res["reason"].lower()

    def test_too_short(self):
        res = ec_id.validate("12345")
        assert not res["valid"]

    def test_letters_in_middle(self):
        # Normalizer strips letters → leaves 10 digits → must still validate
        res = ec_id.validate("12abc05296534")
        # After normalization → 1205296534 (valid)
        assert res["valid"]
        assert res["normalized"] == "1205296534"


# ---------------------------------------------------------------------------
# RUC Natural person (cedula + 001)
# ---------------------------------------------------------------------------

class TestRucNatural:
    # Real natural-person RUCs from Tecnosmart
    VALID = [
        "0951803386001",  # PILOSO INTRIAGO ADONIS
        "0941633521001",  # GUAJALA OSEGUERA CARLOS
        "0924935323001",  # PORTUGAL CORDOVA SARA
        "0919258160001",  # CEDEÑO TOLEDO MARIO
        "1802976017001",  # GALLARDO PAREDES EDWIN
    ]

    @pytest.mark.parametrize("ruc", VALID)
    def test_valid_real_rucs(self, ruc):
        res = ec_id.validate(ruc)
        assert res["valid"], res["reason"]
        assert res["type"] == "ruc_natural"
        assert res["normalized"] == ruc

    def test_missing_001_suffix(self):
        # Same cedula, wrong suffix
        res = ec_id.validate("0951803386002")
        assert not res["valid"]

    def test_invalid_dv_natural(self):
        # Flip DV inside the cedula portion
        res = ec_id.validate("0951803385001")
        assert not res["valid"]


# ---------------------------------------------------------------------------
# RUC Sociedad privada (third digit = 9, modulo-11)
# ---------------------------------------------------------------------------

class TestRucPrivada:
    # Real RUCs from Tecnosmart suppliers / customers
    VALID = [
        "0992624310001",  # MEPRIGA (partner_id=229)
        "0992889284001",  # TECNOSMARTEC S.A.
        "1791433025001",  # TECNOMEGA C.A.
        "1791743148001",  # INTCOMEX DEL ECUADOR
        "1791353897001",  # MEGAMICRO S.A.
        "0991243844001",  # SIGLO21
        "0991400427001",  # CARTIMEX S.A.
        "0992286571001",  # BRELDYNG S.A
        "1792009863001",  # HENTEL CIA.LTDA.
        "0992695668001",  # WINEDTECH S.A.
        "0992146818001",  # ZC MAYORISTAS S.A.
        "1791999150001",  # CORPOELYDO CIA. LTDA.
    ]

    @pytest.mark.parametrize("ruc", VALID)
    def test_valid_real_rucs(self, ruc):
        res = ec_id.validate(ruc)
        assert res["valid"], f"{ruc}: {res['reason']}"
        assert res["type"] == "ruc_privada"
        assert res["normalized"] == ruc

    def test_invalid_dv_priv(self):
        # Flip DV (position 9) of MEPRIGA
        res = ec_id.validate("0992624311001")
        assert not res["valid"]
        assert "verificador" in res["reason"].lower()

    def test_third_digit_8_invalid(self):
        # third digit must be 6 or 9 for non-natural — 8 is invalid
        res = ec_id.validate("0982624310001")
        assert not res["valid"]
        assert "tercer" in res["reason"].lower()

    def test_with_dashes(self):
        res = ec_id.validate("0992-624310-001")
        assert res["valid"]
        assert res["type"] == "ruc_privada"
        assert res["normalized"] == "0992624310001"

    def test_consumidor_final_fails(self):
        # 9999999999999 is a special SRI sentinel that does NOT pass real
        # checksum validation. Odoo stores it but real validators reject it.
        res = ec_id.validate("9999999999999")
        assert not res["valid"]


# ---------------------------------------------------------------------------
# RUC Entidad pública (third digit = 6, modulo-11, 8-digit base, ends 0001)
# ---------------------------------------------------------------------------

class TestRucPublica:
    VALID = [
        "1760004650001",  # IESS - Instituto Ecuatoriano de Seguridad Social
        "1760013210001",  # SRI - Servicio de Rentas Internas
    ]

    @pytest.mark.parametrize("ruc", VALID)
    def test_valid_real_rucs(self, ruc):
        res = ec_id.validate(ruc)
        assert res["valid"], f"{ruc}: {res['reason']}"
        assert res["type"] == "ruc_publica"
        assert res["normalized"] == ruc

    def test_invalid_dv_pub(self):
        # Flip DV at position 8 for IESS
        res = ec_id.validate("1760004660001")
        assert not res["valid"]

    def test_missing_0001_suffix(self):
        # SRI rule: public RUC must end in 0001 (not just 001)
        res = ec_id.validate("1760004651001")
        assert not res["valid"]


# ---------------------------------------------------------------------------
# Generic dispatcher & edge cases
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_empty_string(self):
        res = ec_id.validate("")
        assert not res["valid"]
        assert res["type"] is None
        assert "vacio" in res["reason"].lower()

    def test_none_input(self):
        res = ec_id.validate(None)
        assert not res["valid"]
        assert res["type"] is None

    def test_wrong_length(self):
        res = ec_id.validate("12345678")
        assert not res["valid"]
        assert "10 digitos" in res["reason"]

    def test_normalized_field_always_present(self):
        # Even for invalid input we still return the normalized form
        res = ec_id.validate(" 099-99 ")
        assert res["normalized"] == "09999"
        assert not res["valid"]

    def test_thirteen_random_digits_rejected(self):
        # Random 13-digit string — should not pass any branch
        res = ec_id.validate("1234567890123")
        assert not res["valid"]


# ---------------------------------------------------------------------------
# Legacy tuple wrappers (used by mcp_transport.py call sites)
# ---------------------------------------------------------------------------

class TestLegacyWrappers:
    def test_validate_cedula_ecuador_ok(self):
        valid, msg = ec_id.validate_cedula_ecuador("1205296534")
        assert valid
        assert msg == "OK"

    def test_validate_cedula_ecuador_strips_dashes(self):
        valid, msg = ec_id.validate_cedula_ecuador("120-529-6534")
        assert valid

    def test_validate_cedula_ecuador_rejects_ruc(self):
        valid, msg = ec_id.validate_cedula_ecuador("0992624310001")
        assert not valid

    def test_validate_ruc_ecuador_ok_privada(self):
        valid, msg = ec_id.validate_ruc_ecuador("0992624310001")
        assert valid
        assert msg == "OK"

    def test_validate_ruc_ecuador_ok_publica(self):
        valid, msg = ec_id.validate_ruc_ecuador("1760004650001")
        assert valid

    def test_validate_ruc_ecuador_bad_dv_no_longer_passes(self):
        # ── Regression test for the production bug ──────────────────────
        # Before the refactor, the old validator accepted ANY 13-digit
        # string ending in 001 with third digit 6 or 9 (no checksum). Now
        # the DV is properly checked and this MUST fail.
        valid, msg = ec_id.validate_ruc_ecuador("0992624311001")  # flipped DV
        assert not valid

    def test_validate_cedula_or_ruc_classifies(self):
        ok, _, kind = ec_id.validate_cedula_or_ruc("1205296534")
        assert ok and kind == "cedula"
        ok, _, kind = ec_id.validate_cedula_or_ruc("0992624310001")
        assert ok and kind == "ruc"
        ok, _, kind = ec_id.validate_cedula_or_ruc("abc")
        assert not ok and kind == "unknown"


# ---------------------------------------------------------------------------
# Idempotency / determinism (regression test for goal4 "rejected then
# accepted" bug — same input MUST always produce the same result)
# ---------------------------------------------------------------------------

class TestDeterminism:
    @pytest.mark.parametrize("value", [
        "0992624310001",
        "0992624310001 ",
        " 0992624310001",
        "0992-624310-001",
        "0992624310001",
    ])
    def test_same_ruc_always_validates_same(self, value):
        # Repeat several times to guard against any hidden state / random behaviour
        results = [ec_id.validate(value) for _ in range(20)]
        assert all(r["valid"] for r in results)
        assert all(r["type"] == "ruc_privada" for r in results)
        assert all(r["normalized"] == "0992624310001" for r in results)
