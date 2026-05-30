"""Signature queue (COLA_DOCS_FIRMAR) — status, error listing and reset.

The COLA_DOCS_FIRMAR table is Theos' electronic-signature queue: every
invoice/credit-note/retention that has to be sent to SRI lands here
first, with ``ESTADO_FEAP`` tracking its position in the SRI pipeline.

ESTADO_FEAP values (single char, see Velneo static table):

  '0' Puesto en Cola
  '1' Comprobante enviado Servidor
  '2' Re-Enviar
  'A' Autorizado
  'C' Consultando
  'E' Enviado
  'F' Firmado
  'I' Error            <-- target of the reset operation
  'M' Enviando Correo
  'N' Por Enviar Correo
  'O' Procesando
  'P' Pendiente
  'Q' No procesar
  'R' Rechazado
  'S' Error Creando
  'U' No autorizado SRI
  'X' Error en Identificacion
  'Z' Verificando Estado

When SRI authorizes a comprobante but Datil/FEAP didn't sync the
state in time, Theos may retry sending it. SRI then refuses with
``"No es posible modificar un comprobante autorizado"`` (code
``INVALID_RECEIPT``). The row gets stuck on ``ESTADO_FEAP='I'``
forever. Putting it back to ``'1'`` makes the next verification
cycle pick it up, query SRI, find AUTORIZADO and close it
correctly. No re-send, no duplication.
"""
from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient


# Human-readable labels for each ESTADO_FEAP value. Keeping them in
# Spanish — they're shown to the operator in the chat.
ESTADO_FEAP_LABELS: dict[str, str] = {
    "0": "Puesto en Cola",
    "1": "Comprobante enviado Servidor",
    "2": "Re-Enviar",
    "A": "Autorizado",
    "C": "Consultando",
    "E": "Enviado",
    "F": "Firmado",
    "I": "Error",
    "M": "Enviando Correo",
    "N": "Por Enviar Correo",
    "O": "Procesando",
    "P": "Pendiente",
    "Q": "No procesar",
    "R": "Rechazado",
    "S": "Error Creando",
    "U": "No autorizado SRI",
    "X": "Error en Identificacion",
    "Z": "Verificando Estado",
}

# Default error-message patterns that the reset operation handles
# safely. Anything outside this list is a real data problem and a
# reset would just hide it — the tool flags them but doesn't touch.
DEFAULT_SAFE_RESET_PATTERNS: tuple[str, ...] = (
    "No es posible modificar un comprobante autorizado",
    "INVALID_RECEIPT",  # SRI code that often pairs with the above
)


async def _fetch_all_pages(
    client: VelneoClient,
    table: str,
    *,
    fields: list[str] | None = None,
    page_size: int = 500,
    max_pages: int = 40,
) -> list[dict[str, Any]]:
    """Walk every page of a table, returning the combined rows.

    We can't filter ESTADO_FEAP on the server (Velneo treats single-char
    fields as exact-match on the indexed list and the static-table
    enum makes URL encoding fragile). Page everything and filter in
    memory — typical Mepriga has <5k queue rows total.
    """
    out: list[dict[str, Any]] = []
    params: dict[str, Any] = {"page[size]": page_size}
    if fields:
        params["fields"] = ",".join(fields)
    for page in range(1, max_pages + 1):
        params["page[number]"] = page
        resp = await client._client.get(table, params=params)  # noqa: SLF001
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("cola_docs_firmar") or data.get("COLA_DOCS_FIRMAR") or []
        if not isinstance(rows, list):
            rows = [rows]
        out.extend(rows)
        if len(rows) < page_size:
            break
    return out


async def signature_queue_status(
    client: VelneoClient,
    *,
    include_examples: bool = True,
    max_examples_per_state: int = 3,
) -> dict[str, Any]:
    """Count COLA_DOCS_FIRMAR rows grouped by ESTADO_FEAP.

    Returns the full breakdown (all states present, including
    Autorizado/Enviado/etc. that the operator was missing in the
    previous answer). For each state with errors, optionally surfaces
    a few example records so the operator sees what's stuck.
    """
    from collections import defaultdict
    rows = await _fetch_all_pages(
        client, "COLA_DOCS_FIRMAR",
        fields=[
            "ID", "ESTADO_FEAP", "OBSER_DOC_SRI",
            "SRI_ESTABLECIMIENTO", "SRI_PUNTO_EMISION", "SRI_SECUENCIAL",
            "NAME", "VALOR", "FECHA_DOC", "FECHA",
        ],
    )
    if not rows:
        return {
            "success": True, "total": 0,
            "by_state": [],
            "note": "La cola de firma está vacía.",
        }

    by_state: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        st = (r.get("estado_feap") or "").strip() or "(vacío)"
        by_state[st].append(r)

    breakdown = []
    for st in sorted(by_state.keys()):
        items = by_state[st]
        entry = {
            "code": st,
            "label": ESTADO_FEAP_LABELS.get(st, f"(desconocido: {st})"),
            "count": len(items),
        }
        if include_examples and items:
            # Sample by most recent FECHA_DOC if present.
            sample = sorted(
                items,
                key=lambda x: str(x.get("fecha_doc") or ""),
                reverse=True,
            )[:max_examples_per_state]
            entry["examples"] = [
                {
                    "id": int(x.get("id") or 0),
                    "ref": (
                        f"{(x.get('sri_establecimiento') or '').strip()}-"
                        f"{(x.get('sri_punto_emision') or '').strip()}-"
                        f"{(x.get('sri_secuencial') or '').strip()}"
                    ).strip("-"),
                    "valor": float(x.get("valor") or 0),
                    "detalle": (x.get("name") or "").strip()[:80],
                    "fecha_doc": str(x.get("fecha_doc") or "")[:10],
                    "obser": (x.get("obser_doc_sri") or "").strip()[:200] or None,
                }
                for x in sample
            ]
        breakdown.append(entry)

    return {
        "success": True,
        "total": len(rows),
        "by_state": breakdown,
        "labels_reference": ESTADO_FEAP_LABELS,
    }


async def list_signature_queue_errors(
    client: VelneoClient,
    *,
    patterns: list[str] | None = None,
    include_all_errors: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List COLA_DOCS_FIRMAR rows with ESTADO_FEAP='I'.

    ``patterns`` filters by substrings in OBSER_DOC_SRI (case-insensitive).
    Defaults to :data:`DEFAULT_SAFE_RESET_PATTERNS` — the only error
    family known to be safe to reset.

    ``include_all_errors=True`` returns every error row (also the ones
    that should NOT be reset, like INVALID_KEY or duplicate cert). The
    caller can show them as informational.
    """
    rows = await _fetch_all_pages(
        client, "COLA_DOCS_FIRMAR",
        fields=[
            "ID", "ESTADO_FEAP", "OBSER_DOC_SRI",
            "SRI_ESTABLECIMIENTO", "SRI_PUNTO_EMISION", "SRI_SECUENCIAL",
            "NAME", "VALOR", "FECHA_DOC", "FECHA",
            "VENT_FACT_VENT", "VENT_NOTA_CRED", "COMP_RETENCIONES",
        ],
    )
    error_rows = [r for r in rows if (r.get("estado_feap") or "").strip() == "I"]

    pats = [p.lower() for p in (patterns or DEFAULT_SAFE_RESET_PATTERNS)]
    safe = []
    other = []
    for r in error_rows:
        obser = (r.get("obser_doc_sri") or "").lower()
        matches = any(p in obser for p in pats)
        item = {
            "id": int(r.get("id") or 0),
            "ref": (
                f"{(r.get('SRI_ESTABLECIMIENTO') or '').strip()}-"
                f"{(r.get('SRI_PUNTO_EMISION') or '').strip()}-"
                f"{(r.get('SRI_SECUENCIAL') or '').strip()}"
            ).strip("-"),
            "valor": float(r.get("valor") or 0),
            "detalle": (r.get("name") or "").strip()[:80],
            "fecha_doc": str(r.get("fecha_doc") or "")[:10],
            "obser": (r.get("obser_doc_sri") or "").strip()[:300],
            "vent_fact_vent": int(r.get("vent_fact_vent") or 0) or None,
            "vent_nota_cred": int(r.get("vent_nota_cred") or 0) or None,
            "comp_retenciones": int(r.get("comp_retenciones") or 0) or None,
        }
        if matches:
            safe.append(item)
        else:
            other.append(item)

    return {
        "success": True,
        "n_errors_total": len(error_rows),
        "n_safe_to_reset": len(safe),
        "n_other_errors": len(other),
        "patterns_applied": list(pats),
        "safe_to_reset": safe[:limit],
        "other_errors": (other[:limit] if include_all_errors else []),
    }


async def reset_signature_queue_record(
    client: VelneoClient,
    *,
    record_id: int,
    reason: str = "",
) -> dict[str, Any]:
    """Set ESTADO_FEAP='1' on a single COLA_DOCS_FIRMAR row.

    Velneo's REST API uses **POST /TABLE/{id}** to modify an existing
    record (NOT PATCH). The Swagger documents the endpoint literally as
    "Modify existing document":

      POST  /cola_docs_firmar          → create new (full body)
      POST  /cola_docs_firmar/{id}     → modify existing (partial body)

    Confirmed empirically on 2026-05-29 with id=204 (factura García
    Reyes Patricia 001-002-000644228): POST with body {"ESTADO_FEAP":
    "1"} executed the update and the re-read confirmed the change. No
    PATCH method needed — and no fallback either; this is the canonical
    Velneo update pattern.

    Always re-reads the row after the write to confirm the new state.
    """
    rid = int(record_id)
    if not rid:
        return {"success": False, "error": "record_id required"}

    # Pre-flight: read current state.
    try:
        pre = await client._client.get(  # noqa: SLF001
            f"COLA_DOCS_FIRMAR/{rid}",
            params={"fields": "ID,ESTADO_FEAP,OBSER_DOC_SRI,NAME,VALOR"},
        )
        pre.raise_for_status()
        pre_data = pre.json()
        pre_rows = (
            pre_data.get("cola_docs_firmar")
            or pre_data.get("COLA_DOCS_FIRMAR")
            or []
        )
        if isinstance(pre_rows, dict):
            pre_rows = [pre_rows]
        pre_state = (
            (pre_rows[0].get("estado_feap") if pre_rows else None) or ""
        ).strip()
    except Exception as exc:  # noqa: BLE001
        return {"success": False,
                "error": f"failed to read record {rid}: {exc}"}

    if not pre_rows:
        return {"success": False,
                "error": f"record {rid} not found"}

    if pre_state != "I":
        return {
            "success": False,
            "error_code": "not_in_error_state",
            "error": (
                f"El registro {rid} está en estado '{pre_state}' "
                f"({ESTADO_FEAP_LABELS.get(pre_state, '?')}), no en "
                f"error 'I'. Reset NO aplicado para evitar tocar un "
                f"comprobante ya en flujo normal."
            ),
            "current_state": pre_state,
        }

    # Try POST-with-id (Velneo upsert pattern).
    try:
        resp = await client._client.post(  # noqa: SLF001
            f"COLA_DOCS_FIRMAR/{rid}",
            json={"ESTADO_FEAP": "1"},
        )
        body = resp.json() if resp.content else {}
        errors = body.get("errors") or []
        first = errors[0] if isinstance(errors, list) and errors else None
        status = (
            first.get("status") if isinstance(first, dict)
            else (str(first) if first else "")
        )
        if status == "405" or "no es válido" in str(first or "").lower():
            # Esta rama no debería dispararse: el swagger documenta
            # POST /COLA_DOCS_FIRMAR/{id} como "Modify existing
            # document" y ya está habilitado para niko_saas. Si llega
            # acá es porque el operador deshabilitó POST en el panel
            # de Seguridad → API key. La solución es re-habilitar
            # POST sobre la tabla, NO buscar habilitar PATCH (que es
            # un método que Velneo no usa para esta semántica).
            return {
                "success": False,
                "error_code": "post_disabled",
                "error": (
                    "Velneo rechazó el POST con id (405). El API key "
                    "niko_saas necesita POST habilitado sobre "
                    "COLA_DOCS_FIRMAR. Habilítalo en Velneo Server → "
                    "Seguridad → API key niko_saas → COLA_DOCS_FIRMAR "
                    "→ marcar POST. Velneo usa POST /TABLE/{id} para "
                    "modificar registros (no PATCH)."
                ),
                "velneo_status": status,
                "velneo_error": str(first),
            }
        if errors:
            return {
                "success": False,
                "error": f"velneo {status}: {first}",
            }
    except Exception as exc:  # noqa: BLE001
        return {"success": False,
                "error": f"velneo write failed: {exc}"}

    # Confirm with a fresh GET.
    try:
        post = await client._client.get(  # noqa: SLF001
            f"COLA_DOCS_FIRMAR/{rid}",
            params={"fields": "ID,ESTADO_FEAP,OBSER_DOC_SRI"},
        )
        post.raise_for_status()
        post_data = post.json()
        post_rows = (
            post_data.get("cola_docs_firmar")
            or post_data.get("COLA_DOCS_FIRMAR")
            or []
        )
        if isinstance(post_rows, dict):
            post_rows = [post_rows]
        post_state = (
            (post_rows[0].get("estado_feap") if post_rows else None) or ""
        ).strip()
    except Exception as exc:  # noqa: BLE001
        # The write may have succeeded but we can't confirm — still
        # report success but flag it.
        return {
            "success": True,
            "confirmed": False,
            "warning": f"write OK but confirm-read failed: {exc}",
            "record_id": rid, "previous_state": pre_state,
            "intended_state": "1",
            "reason": reason or None,
        }

    confirmed = post_state == "1"
    return {
        "success": confirmed,
        "confirmed": confirmed,
        "record_id": rid,
        "previous_state": pre_state,
        "previous_label": ESTADO_FEAP_LABELS.get(pre_state, "?"),
        "current_state": post_state,
        "current_label": ESTADO_FEAP_LABELS.get(post_state, "?"),
        "reason": reason or None,
        "note": (
            "Próximo ciclo de verificación lo reprocesa: consulta al "
            "SRI, recibe AUTORIZADO y cierra. Sin re-envío ni "
            "duplicación."
            if confirmed else
            "El POST se ejecutó sin error pero el re-read sigue mostrando "
            f"el estado anterior ('{post_state}'). Verifica manualmente."
        ),
    }
