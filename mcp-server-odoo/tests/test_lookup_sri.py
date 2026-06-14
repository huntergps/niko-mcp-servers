"""Tests for ``_lookup_sri`` — the SRI (Ecuador) catastro lookup tool.

The real tool hits three public SRI REST endpoints. Here we patch
``httpx.AsyncClient`` with the verbatim payloads the SRI returns (captured
live 2026-06-13 for MEPRIGA, a SOCIEDAD with a legal representative) and
assert the parser surfaces ALL of the fields — regression guard for the
2026-06-13 fix that had been dropping representantes_legales, fraud flags,
categoria, motivo de cancelación and the cese/reinicio/actualizacion dates.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from mcp_odoo.transports.mcp_transport import _lookup_sri


# --- Verbatim SRI payloads (RUC 0992624310001 = MEPRIGA, SOCIEDAD) ----------

_PERSONA = {
    "identificacion": "0992624310001",
    "nombreCompleto": "MEGA PRIMAVERA GALAPAGOS S.A. (MEPRIGA)",
    "tipoPersona": "JUR",
    "codigoPersona": 2320992,
}

_CONSOLIDADO = [
    {
        "numeroRuc": "0992624310001",
        "razonSocial": "MEGA PRIMAVERA GALAPAGOS S.A. (MEPRIGA)",
        "estadoContribuyenteRuc": "ACTIVO",
        "actividadEconomicaPrincipal": "VENTA AL POR MENOR DE GRAN VARIEDAD DE PRODUCTOS EN TIENDAS...",
        "tipoContribuyente": "SOCIEDAD",
        "regimen": "GENERAL",
        "categoria": None,
        "obligadoLlevarContabilidad": "SI",
        "agenteRetencion": "NO",
        "contribuyenteEspecial": "SI",
        "informacionFechasContribuyente": {
            "fechaInicioActividades": "2009-06-04 00:00:00.0",
            "fechaCese": "",
            "fechaReinicioActividades": "",
            "fechaActualizacion": "2023-07-19 09:20:30.0",
        },
        "representantesLegales": [
            {"identificacion": "1801971613", "nombre": "BENITEZ LOZADA JOHNSON OLIVO"}
        ],
        "motivoCancelacionSuspension": None,
        "contribuyenteFantasma": "NO",
        "transaccionesInexistente": "NO",
    }
]

_ESTABLECIMIENTOS = [
    {
        "numeroEstablecimiento": "001",
        "tipoEstablecimiento": "MAT",
        "nombreFantasiaComercial": "MEPRIGA",
        "direccionCompleta": "GALAPAGOS / SANTA CRUZ / ...",
        "estado": "ABIERTO",
    },
    {
        "numeroEstablecimiento": "005",
        "tipoEstablecimiento": "OFI",
        "nombreFantasiaComercial": "BODEGA-MEPRIGA",
        "direccionCompleta": "GUAYAS / GUAYAQUIL / ...",
        "estado": "ABIERTO",
    },
]


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        if "Persona/obtenerPorTipoIdentificacion" in url:
            return _FakeResponse(_PERSONA)
        if "ConsolidadoContribuyente/obtenerPorNumerosRuc" in url:
            return _FakeResponse(_CONSOLIDADO)
        if "Establecimiento/consultarPorNumeroRuc" in url:
            return _FakeResponse(_ESTABLECIMIENTOS)
        return _FakeResponse({})


@pytest.mark.asyncio
async def test_lookup_sri_returns_all_consolidado_fields():
    with patch("httpx.AsyncClient", _FakeClient):
        out = json.loads(await _lookup_sri("0992624310001"))

    assert out["found"] is True
    # Persona
    assert out["nombre"] == "MEGA PRIMAVERA GALAPAGOS S.A. (MEPRIGA)"
    assert out["tipo_persona"] == "JUR"
    assert out["codigo_persona"] == 2320992
    # Consolidado — fiscal status
    assert out["razon_social"] == "MEGA PRIMAVERA GALAPAGOS S.A. (MEPRIGA)"
    assert out["estado"] == "ACTIVO"
    assert out["tipo_contribuyente"] == "SOCIEDAD"
    assert out["regimen"] == "GENERAL"
    assert out["obligado_contabilidad"] == "SI"
    assert out["agente_retencion"] == "NO"
    assert out["contribuyente_especial"] == "SI"
    # Previously-dropped fields (the regression this test guards)
    assert out["contribuyente_fantasma"] == "NO"
    assert out["transacciones_inexistente"] == "NO"
    assert out["categoria"] == ""
    assert out["motivo_cancelacion_suspension"] == ""
    assert out["representantes_legales"] == [
        {"identificacion": "1801971613", "nombre": "BENITEZ LOZADA JOHNSON OLIVO"}
    ]
    # Full date lifecycle
    assert out["fecha_inicio"] == "2009-06-04 00:00:00.0"
    assert out["fecha_cese"] == ""
    assert out["fecha_reinicio"] == ""
    assert out["fecha_actualizacion"] == "2023-07-19 09:20:30.0"
    # Establishments
    assert len(out["establecimientos"]) == 2
    assert out["direccion"]  # matriz address resolved


@pytest.mark.asyncio
async def test_lookup_sri_natural_person_has_empty_reps():
    """A persona natural still parses; representantes_legales is just empty."""
    consolidado_natural = [dict(_CONSOLIDADO[0], representantesLegales=None,
                                tipoContribuyente="PERSONA NATURAL",
                                obligadoLlevarContabilidad="NO")]

    class _NaturalClient(_FakeClient):
        async def get(self, url, params=None):
            if "ConsolidadoContribuyente" in url:
                return _FakeResponse(consolidado_natural)
            return await super().get(url, params=params)

    with patch("httpx.AsyncClient", _NaturalClient):
        out = json.loads(await _lookup_sri("1500501968001"))

    assert out["found"] is True
    assert out["representantes_legales"] == []
    assert out["obligado_contabilidad"] == "NO"
