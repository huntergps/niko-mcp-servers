"""Ecuadorian cedula / RUC validator.

Implements the official SRI Ecuador algorithms for the four document kinds:

* ``cedula``       — 10 digits, modulo-10 checksum, province 01-24, third < 6.
* ``ruc_natural``  — 13 digits, valid cedula + ``001`` suffix.
* ``ruc_privada``  — 13 digits, third digit = 9, modulo-11 checksum, ``001``.
* ``ruc_publica``  — 13 digits, third digit = 6, modulo-11 checksum (8 digits),
                     ``0001`` suffix.

Returns a structured dict so callers can rely on a stable contract:

    {
        "valid":      bool,
        "type":       "cedula" | "ruc_natural" | "ruc_privada" | "ruc_publica" | None,
        "normalized": str,
        "reason":     str | None,   # human-friendly Spanish reason on failure
    }

Two thin tuple wrappers are kept for backwards-compatibility with the
existing call sites in ``mcp_transport.py``:

* ``validate_cedula_ecuador(s) -> (bool, msg)``
* ``validate_ruc_ecuador(s) -> (bool, msg)``
* ``validate_cedula_or_ruc(s) -> (bool, msg, kind)``  where ``kind`` is the
  legacy two-class label ``"cedula" | "ruc" | "unknown"``.

Pesos / coefficients are SRI-defined constants (matemática estándar Ecuador,
not business policy) so they live as module-level tuples here.
"""

from __future__ import annotations

import re
from typing import Optional, TypedDict

# ---------------------------------------------------------------------------
# SRI coefficients (constants defined by Ecuadorian tax law — not business
# constants, never configurable). Documented:
#  https://www.sri.gob.ec/  (Resolución NAC-DGERCGC*)
# ---------------------------------------------------------------------------

_CEDULA_COEFS = (2, 1, 2, 1, 2, 1, 2, 1, 2)
_RUC_PRIV_COEFS = (4, 3, 2, 7, 6, 5, 4, 3, 2)
_RUC_PUB_COEFS = (3, 2, 7, 6, 5, 4, 3, 2)


class IdValidationResult(TypedDict):
    valid: bool
    type: Optional[str]
    normalized: str
    reason: Optional[str]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_NON_DIGIT = re.compile(r"\D+")


def normalize(value: str | None) -> str:
    """Strip any non-digit character (dashes, dots, spaces, letters)."""
    if not value:
        return ""
    return _NON_DIGIT.sub("", str(value))


# ---------------------------------------------------------------------------
# Low-level checksum helpers
# ---------------------------------------------------------------------------

def _province_ok(digits: str) -> bool:
    """Province code 01..24 (and 30 for ecuadorians abroad)."""
    try:
        prov = int(digits[:2])
    except ValueError:
        return False
    return 1 <= prov <= 24 or prov == 30


def _cedula_dv(nine_digits: str) -> int:
    """Module-10 checksum used by cedula and natural-person RUC."""
    total = 0
    for i, c in enumerate(nine_digits):
        v = int(c) * _CEDULA_COEFS[i]
        if v >= 10:
            v -= 9
        total += v
    return (10 - (total % 10)) % 10


def _ruc_priv_dv(nine_digits: str) -> int | None:
    """Module-11 checksum for private-society RUC. Returns ``None`` if invalid."""
    total = sum(int(c) * _RUC_PRIV_COEFS[i] for i, c in enumerate(nine_digits))
    mod = total % 11
    dv = 11 - mod
    if dv == 11:
        return 0
    if dv == 10:
        return None  # By SRI rule, mod=1 → invalid RUC sequence
    return dv


def _ruc_pub_dv(eight_digits: str) -> int | None:
    """Module-11 checksum for public-entity RUC (operates on 8 digits)."""
    total = sum(int(c) * _RUC_PUB_COEFS[i] for i, c in enumerate(eight_digits))
    mod = total % 11
    dv = 11 - mod
    if dv == 11:
        return 0
    if dv == 10:
        return None
    return dv


# ---------------------------------------------------------------------------
# Structured public API
# ---------------------------------------------------------------------------

def _result(valid: bool, type_: Optional[str], normalized: str,
            reason: Optional[str]) -> IdValidationResult:
    return {"valid": valid, "type": type_, "normalized": normalized,
            "reason": reason}


def validate(value: str | None) -> IdValidationResult:
    """Validate ``value`` as Ecuadorian cedula or RUC.

    The input is normalized (any non-digit char is stripped) before the
    structural and checksum checks are applied. The result always exposes the
    normalized string so the caller can store it in a canonical form.
    """
    clean = normalize(value)
    if not clean:
        return _result(False, None, clean,
                       "El identificador esta vacio.")

    if len(clean) == 10:
        return _validate_cedula(clean)

    if len(clean) == 13:
        return _validate_ruc(clean)

    return _result(False, None, clean,
                   "Debe ser una cedula (10 digitos) o RUC (13 digitos).")


def _validate_cedula(clean: str) -> IdValidationResult:
    if not _province_ok(clean):
        return _result(False, None, clean,
                       f"Codigo de provincia invalido: {clean[:2]}.")
    third = int(clean[2])
    if third >= 6:
        return _result(False, None, clean,
                       "Tercer digito invalido para cedula de persona natural.")
    if _cedula_dv(clean[:9]) != int(clean[9]):
        return _result(False, None, clean,
                       "Digito verificador invalido.")
    return _result(True, "cedula", clean, None)


def _validate_ruc(clean: str) -> IdValidationResult:
    third = int(clean[2])

    # ── Natural-person RUC ────────────────────────────────────────────────
    if third < 6:
        if not _province_ok(clean):
            return _result(False, None, clean,
                           f"Codigo de provincia invalido: {clean[:2]}.")
        if not clean.endswith("001"):
            return _result(False, None, clean,
                           "El RUC de persona natural debe terminar en 001.")
        if _cedula_dv(clean[:9]) != int(clean[9]):
            return _result(False, None, clean,
                           "Digito verificador invalido para RUC de persona natural.")
        return _result(True, "ruc_natural", clean, None)

    # ── Public-entity RUC ─────────────────────────────────────────────────
    if third == 6:
        if not _province_ok(clean):
            return _result(False, None, clean,
                           f"Codigo de provincia invalido: {clean[:2]}.")
        if not clean.endswith("0001"):
            return _result(False, None, clean,
                           "El RUC de entidad publica debe terminar en 0001.")
        expected = _ruc_pub_dv(clean[:8])
        if expected is None or expected != int(clean[8]):
            return _result(False, None, clean,
                           "Digito verificador invalido para RUC publico.")
        return _result(True, "ruc_publica", clean, None)

    # ── Private-society RUC ──────────────────────────────────────────────
    if third == 9:
        if not _province_ok(clean):
            return _result(False, None, clean,
                           f"Codigo de provincia invalido: {clean[:2]}.")
        if not clean.endswith("001"):
            return _result(False, None, clean,
                           "El RUC de sociedad debe terminar en 001.")
        expected = _ruc_priv_dv(clean[:9])
        if expected is None or expected != int(clean[9]):
            return _result(False, None, clean,
                           "Digito verificador invalido para RUC de sociedad.")
        return _result(True, "ruc_privada", clean, None)

    return _result(False, None, clean, f"Tercer digito invalido: {third}.")


# ---------------------------------------------------------------------------
# Backwards-compatible tuple wrappers (existing call sites)
# ---------------------------------------------------------------------------

def validate_cedula_ecuador(cedula: str) -> tuple[bool, str]:
    """Legacy API. Validates cedula only (rejects RUCs)."""
    clean = normalize(cedula)
    if len(clean) != 10:
        return False, "La cedula debe tener exactamente 10 digitos numericos."
    res = _validate_cedula(clean)
    return res["valid"], res["reason"] or "OK"


def validate_ruc_ecuador(ruc: str) -> tuple[bool, str]:
    """Legacy API. Validates RUC only (rejects cedulas)."""
    clean = normalize(ruc)
    if len(clean) != 13:
        return False, "El RUC debe tener exactamente 13 digitos numericos."
    res = _validate_ruc(clean)
    return res["valid"], res["reason"] or "OK"


def validate_cedula_or_ruc(value: str) -> tuple[bool, str, str]:
    """Legacy API used by ``mcp_transport._create_partner`` /
    ``_identify_customer``.

    Returns ``(valid, message, kind)`` where ``kind`` is the coarse-grained
    label expected by the legacy callers: ``"cedula" | "ruc" | "unknown"``.
    Use :func:`validate` for the fine-grained subtype.
    """
    res = validate(value)
    if res["type"] in ("ruc_natural", "ruc_privada", "ruc_publica"):
        kind = "ruc"
    elif res["type"] == "cedula":
        kind = "cedula"
    else:
        kind = "unknown"
    return res["valid"], res["reason"] or "OK", kind
