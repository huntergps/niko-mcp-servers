"""Tests for the ``case_chassis`` intent filter (audit 2026-05-30).

A Tecnosmart PC-build "case/gabinete" search surfaced "CASE COMBO ...
TECLADO - MOUSE - PARLANTES" starter bundles (peripherals, not a PC
chassis) because their name starts with "CASE". The LLM then rewrote
those combos into fabricated gaming cases. The ``case_chassis`` rule in
``_INTENT_RULES`` drops the bundles so only real chassis reach the model.

These exercise ``_classify_product_intent`` + ``_apply_kind_filter``
directly with synthetic rows — no live Odoo needed.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


def _row(code: str, name: str, qty: int = 5) -> dict:
    return {
        "odoo_id": hash(code) & 0xFFFF,
        "_live": {
            "code": code,
            "name": name,
            "price": 50.0,
            "qty": qty,
            "category": "Componentes / Gabinetes",
        },
    }


# Real Tecnosmart-style candidate pool for a "gabinete" search: 4
# peripheral bundles + 5 genuine chassis (two of which legitimately
# include a power supply and must NOT be dropped).
_POOL = [
    _row("CAS0280", "CASE COMBO QUASAD QC-610 SLIM TECLADO - MOUSE - PARLANTES - FUENTE"),
    _row("CAS0281", "Case Combo TERRAX Slim T02 fuente,mouse,teclado,parlantes"),
    _row("CAS0282", "Case Combo Xtech Teclado / Mouse / Parlante / Fuente 500/ CS"),
    _row("CAS0498", "CASE HELTECH Z06 + FUENTE + TECLADO + MOUSE + PARLANTE - MICRO ATX"),
    _row("CAS0283", "CASE CORSAIR 7000X RGB ICUE - TEMPERED GLASS - ATX - FULL TOWER"),
    _row("CAS0001", "Case AZZA BASTION 120 BLACK 1xFan"),
    _row("CAS0005", "Case Corsair 4000D AirFlow Mesh White, Vidrio Templado"),
    _row("CAS0356", "CASE AGILER C007B- FUENTE 600W"),
    _row("CAS0777", "CASE AVANTI AV-501 - MATX - FUENTE 600W - BLACK"),
]

_BUNDLE_CODES = {"CAS0280", "CAS0281", "CAS0282", "CAS0498"}
_CHASSIS_CODES = {"CAS0283", "CAS0001", "CAS0005", "CAS0356", "CAS0777"}


@pytest.mark.parametrize("query", ["gabinete", "case", "gabinete atx", "chasis"])
def test_chassis_query_drops_combo_bundles_keeps_real_chassis(query):
    from mcp_odoo.transports.mcp_transport import (
        _apply_kind_filter,
        _classify_product_intent,
    )

    rule = _classify_product_intent(query)
    assert rule is not None and rule["kind"] == "case_chassis"

    kept, dropped = _apply_kind_filter(_POOL, rule)
    kept_codes = {r["_live"]["code"] for r in kept}

    assert kept_codes == _CHASSIS_CODES, f"query={query!r}"
    assert dropped == len(_BUNDLE_CODES)
    # Real chassis that include a PSU must survive (only teclado/mouse/
    # parlante/combo tokens disqualify — never "fuente").
    assert {"CAS0356", "CAS0777"} <= kept_codes


def test_explicit_combo_query_is_not_filtered():
    """When the customer explicitly wants the bundle, show it unfiltered."""
    from mcp_odoo.transports.mcp_transport import _classify_product_intent

    assert _classify_product_intent("case combo con teclado y mouse") is None
    assert _classify_product_intent("quiero un combo de case") is None
