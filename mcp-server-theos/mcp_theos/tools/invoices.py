"""Velneo invoices (VENT_FACT_VENT) + balance (ENT_ERP_CLI / VENT_DEUD_CLIE) +
customer statement (VENT_DEUD_CLIE list + per-row PDF rendering).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError

# Field allowlist verified empirically against Mepriga (2026-05-28).
# The API key niko_saas BLOCKS projection on these columns:
#
#   FECHA_FACT, IVA12, ENT_ERP_CLI, ESTADO, NRO_FAC, DIVISIONES,
#   EMAIL, CIF, MAIL_PRINCIPAL, ESTADO_FEAP, VERSION_SRI, UID_DATIL,
#   RIDE_IMPORT_ESTADO, WEB_DATIL, ERROR, FECHA_RET, FORMATO_FECHA
#
# Velneo drops the WHOLE response when any projected field is blocked
# (silent zero rows). So we project ONLY allowed columns. Substitutes
# for the most important blocked ones:
#
#   * NRO_FAC → reconstruct from SERIE + SECUENCIA (see build_sri_number)
#   * ENT_ERP_CLI client name → RAZONSOCIALCOMPRADOR + SRI_IDENTIFICACION
#     (the SRI-side denormalized customer block, always populated for
#     facturas con factura electrónica)
#   * ESTADO / ESTADO_FEAP → LAST_STATUS (the human-readable SRI status
#     string Datil returns: "AUTORIZADO", "DEVUELTA", "NO AUTORIZADO",
#     "PENDIENTE", etc.)
_FACT_FIELDS = [
    # Identifiers and dates
    "ID", "NAME", "FECHA",
    "SERIE", "SECUENCIA",
    "ESTABLECIMIENTO", "PUNTOEMISION",  # alt path to SRI number components
    # Customer (denormalized — substitutes the blocked ENT_ERP_CLI join)
    "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION",
    # Amounts
    "SUBTOTAL", "BASE_IVA", "BASE0", "IVA", "TOTAL",
    "PAGADO", "SALDO",
    # Op metadata
    "VENDEDOR", "VTA_TIPO_ENT", "SUC", "EMP", "INV_BODEGA",
    "OFF", "OFF_MOTIVO", "REF", "REF2",
    # SRI / Datil status (substitutes for blocked ESTADO / ESTADO_FEAP)
    "TIENE_ELECTRONICA", "SRI_TIPO_FEAP",
    "LAST_STATUS",         # human-readable SRI state: "AUTORIZADO" etc.
    "VCACCESOSRI",         # 49-digit clave de acceso
    "AUTORIZACION",        # autorización number SRI
    "KEY",                 # Datil internal UUID
    "TIPO_AMBIENTE",       # "1"=pruebas, "2"=producción
    "VENTA_CREDITO",
]


def build_sri_number(serie: Any, secuencia: Any, *, pad_secuencia: int = 9) -> str:
    """Compose SRI invoice number "001-001-565825" from SERIE + SECUENCIA.

    Velneo's ``NRO_FAC`` column is projection-blocked under the
    niko_saas API key, but the two parts that make it up (``SERIE``
    like "001-001" and ``SECUENCIA`` like 565825) come back fine. We
    join them with a dash and pad the secuencia to 9 digits (SRI's
    canonical width for invoice numbers in Ecuador).
    """
    s = str(serie or "").strip()
    try:
        n = int(secuencia)
    except (TypeError, ValueError):
        return s
    return f"{s}-{n:0{pad_secuencia}d}" if s else f"{n:0{pad_secuencia}d}"


def parse_sri_number(value: str) -> dict[str, Any] | None:
    """Decompose an SRI document number ("001-001-565825") into parts.

    Accepts the canonical Ecuadorian SRI format:

        establecimiento-puntoemision-secuencia
        (3 digits)-(3 digits)-(N digits, usually 9)

    Returns ``None`` if the input does not match. Successful return:

        {
            "establecimiento": "001",
            "puntoemision":    "001",
            "secuencia":       "565825",
            "secuencia_int":   565825,
            "serie":           "001-001",
            "padded":          "001-001-000565825",  # SRI canonical 9-digit
        }
    """
    if not value:
        return None
    s = str(value).strip()
    # Tolerate whitespace and accidental extra dashes/spaces.
    parts = [p for p in re.split(r"[-\s]+", s) if p]
    if len(parts) != 3:
        return None
    est, pe, seq = parts
    if not (est.isdigit() and pe.isdigit() and seq.isdigit()):
        return None
    if len(est) > 3 or len(pe) > 3:
        return None
    try:
        seq_int = int(seq)
    except ValueError:
        return None
    return {
        "establecimiento": est.zfill(3),
        "puntoemision":    pe.zfill(3),
        "secuencia":       seq,
        "secuencia_int":   seq_int,
        "serie":           f"{est.zfill(3)}-{pe.zfill(3)}",
        "padded":          f"{est.zfill(3)}-{pe.zfill(3)}-{seq_int:09d}",
    }

_DEUD_FIELDS = [
    "ID", "NAME", "FECHA", "VENCIMIENTO",
    "ENT_ERP_CLI", "EGRESOS",
    "TOTAL_DEUDA", "PAGADO", "SALDO",
    "DIAS", "DIAS_VENCIDOS",
    "CON_SALDO", "POR_VENCER", "COBRADO", "OFF",
    "NRO_DEUDA", "NRO_TOTAL_DEUDAS",
]


async def get_customer_invoices(
    client: VelneoClient,
    *,
    client_id: int,
    limit: int = 30,
    include_lines: bool = False,
) -> dict[str, Any]:
    try:
        resp = await client.get(
            "VENT_FACT_VENT",
            params={"ENT_ERP_CLI": client_id, "pagesize": limit},
            fields=_FACT_FIELDS,
        )
    except VelneoError as exc:
        return {"success": False, "error": f"velneo {exc.status} {exc.message}"}

    invoices = resp.rows[:limit]
    # Derive nro_fac in every row so callers see the SRI number even
    # though NRO_FAC itself is projection-blocked.
    for inv in invoices:
        inv["NRO_FAC"] = build_sri_number(inv.get("SERIE"), inv.get("SECUENCIA"))
    if include_lines:
        for inv in invoices:
            inv_id = inv.get("ID")
            if inv_id is None:
                continue
            try:
                ln = await client.get(
                    "INV_MOVIMIENTOS",
                    params={"VENT_FACT_VENT": inv_id, "pagesize": 200},
                    fields=[
                        "ID", "VENT_FACT_VENT", "NUM_LINEA", "PRODUCTOS",
                        "NAME", "CAN", "PVP", "PVP_LINEA",
                        "PRECIO_NETO_LINEA", "IVA_LINEA",
                    ],
                )
                inv["lines"] = ln.rows
            except VelneoError as exc:
                inv["_lines_error"] = f"velneo {exc.status} {exc.message}"

    return {
        "success": True,
        "client_id": client_id,
        "count": len(invoices),
        "total_count": resp.total_count,
        "invoices": invoices,
    }


async def check_balance(
    client: VelneoClient,
    *,
    client_id: int,
    detailed: bool = False,
) -> dict[str, Any]:
    """Fast path: ENT_ERP_CLI.SALDO / DEUDASC / CUPOC.

    Detailed path: pull VENT_DEUD_CLIE rows with CON_SALDO=1 so the
    caller can list per-invoice ageing.
    """
    try:
        ext = await client.get(
            "ENT_ERP_CLI",
            record_id=client_id,
            fields=[
                "ID", "NAME", "CIF",
                "SALDO", "SALDOP",
                "DEUDASC", "DEUDASCP",
                "CUPOC", "DISPONIBLE_CUPOC",
                # ``DEUDAS_VENCIDAS`` is rejected by Mepriga's Velneo API
                # key projection — see same note in
                # mcp_theos.tools.partners._ENT_ERP_CLI_FIELDS.
                "DIAS_VENCIDOS",
                "FACTVENCIDAS", "NO_VENDER",
                "ANTICIPOS_VTAS", "ANTICIPOS_DISPO_VTAS",
            ],
        )
    except VelneoError as exc:
        return {"success": False, "error": f"velneo {exc.status} {exc.message}"}
    if not ext.rows:
        return {"success": False, "error": f"client {client_id} not found in ENT_ERP_CLI"}

    summary = ext.rows[0]
    out: dict[str, Any] = {
        "success": True,
        "client_id": client_id,
        "summary": summary,
    }

    if detailed:
        try:
            # ``filter[CON_SALDO]`` does not actually filter on this
            # tenant (returns count=0 even for clients with open
            # debts). Pull a page and filter in-memory by saldo > 0.
            deuds = await client.get(
                "VENT_DEUD_CLIE",
                params={"ENT_ERP_CLI": client_id, "pagesize": 200},
                fields=_DEUD_FIELDS,
            )
            open_only = [r for r in deuds.rows if _saldo_positive(r)]
            out["debts"] = open_only
            out["debt_count"] = len(open_only)
        except VelneoError as exc:
            out["debts_error"] = f"velneo {exc.status} {exc.message}"

    return out


# ---------------------------------------------------------------------------
# Customer statement (estado de cuenta)
# ---------------------------------------------------------------------------


# Same projection as _DEUD_FIELDS but includes REFERENCIA (SRI invoice
# number like "001-001-581914") + CONTABILI source. CAJERO is the
# user_id of the cashier; resolving it to a name would need access
# to the USR table which is not exposed via the niko_saas API key,
# so we leave the numeric id and let the PDF renderer decide.
_STMT_FIELDS = [
    "ID", "NAME", "FECHA", "VENCIMIENTO",
    "ENT_ERP_CLI", "EGRESOS",
    "TOTAL_DEUDA", "PAGADO", "CRUZADO", "SALDO", "RETENIDO",
    "DIAS", "DIAS_VENCIDOS",
    "CON_SALDO", "POR_VENCER", "COBRADO", "OFF",
    "REFERENCIA",
    "NRO_DEUDA", "NRO_TOTAL_DEUDAS",
    "TIPO",
]


def _saldo_positive(row: dict[str, Any]) -> bool:
    try:
        return float(row.get("SALDO") or 0) > 0.01
    except (TypeError, ValueError):
        return False


def _parse_days(value: Any) -> int | None:
    """``DIAS`` comes back as a signed string ("-146", "2"). Returns int or None."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _short_date(value: Any) -> str:
    """``2026-05-16T00:00:00.000Z`` → ``2026-05-16``."""
    if not value or value == "Invalid Date":
        return ""
    s = str(value)
    return s[:10] if "T" in s else s


async def _resolve_partner_header(
    client: VelneoClient, partner_id: int,
) -> dict[str, Any]:
    """Pull ENT row + matching ENT_ERP_CLI row for the statement header."""
    out: dict[str, Any] = {
        "id": partner_id, "name": "", "cif": "",
        "email": "", "address": "",
    }
    try:
        ent = await client.get(
            "ENT", record_id=partner_id,
            fields=["ID", "NAME", "NOM_COM", "CIF", "MAIL_PRINCIPAL", "DIR_PRI"],
        )
        if ent.rows:
            row = ent.rows[0]
            out.update({
                "name": (row.get("NAME") or "").strip(),
                "commercial_name": (row.get("NOM_COM") or "").strip(),
                "cif": (row.get("CIF") or "").strip(),
                "email": (row.get("MAIL_PRINCIPAL") or "").strip(),
                "address": str(row.get("DIR_PRI") or "").strip(),
            })
    except VelneoError:
        pass
    return out


async def get_customer_statement(
    client: VelneoClient,
    *,
    client_id: int,
    only_with_balance: bool = True,
    only_overdue: bool = False,
    cutoff_date: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Pull the customer's debt list for an account-statement view.

    Equivalent to Theos' ``CARGAR_DEUDAS`` proceso which uses the
    ``CORTE_DEUDAS_CLIENTES`` Búsqueda. Velneo's REST cannot drive
    that Búsqueda directly, so we paginate VENT_DEUD_CLIE filtered by
    ``ENT_ERP_CLI`` and apply the cutoff / overdue / has-balance
    rules in memory.

    Returns a payload ready to render as either chat text or PDF:
    ``{ partner, items: [{fecha, vencimiento, dias, referencia, total,
    pagado, saldo, ...}], totals: {total, pagado, saldo, count} }``.
    """
    if not client_id:
        return {"success": False, "error": "client_id required"}

    # ``ENT_ERP_CLI_SALDO`` is a Velneo index on VENT_DEUD_CLIE that
    # transparently filters rows whose SALDO > 0 — the same index the
    # Theos ``CORTE_DEUDAS_CLIENTES`` Búsqueda walks for its print
    # statement. We use it whenever ``only_with_balance=True`` (default)
    # so the server does the filtering instead of pulling 210 rows
    # only to discard 191. When the caller asks for the full ledger
    # (paid + unpaid) we fall back to the regular ENT_ERP_CLI filter.
    filter_key = "ENT_ERP_CLI_SALDO" if only_with_balance else "ENT_ERP_CLI"
    try:
        resp = await client.get(
            "VENT_DEUD_CLIE",
            params={filter_key: client_id, "pagesize": limit},
            fields=_STMT_FIELDS,
        )
    except VelneoError as exc:
        return {
            "success": False,
            "error": f"velneo {exc.status}: {exc.message}",
        }

    cutoff_iso = (cutoff_date or "").strip()  # "YYYY-MM-DD" expected
    items: list[dict[str, Any]] = []
    total = pagado = saldo = 0.0
    for raw in resp.rows:
        if raw.get("OFF"):
            continue
        if only_with_balance and not _saldo_positive(raw):
            continue
        days = _parse_days(raw.get("DIAS"))
        if only_overdue and (days is None or days <= 0):
            continue
        if cutoff_iso:
            f = _short_date(raw.get("FECHA"))
            if f and f > cutoff_iso:
                continue
        item = {
            "id": raw.get("ID"),
            "fecha": _short_date(raw.get("FECHA")),
            "vencimiento": _short_date(raw.get("VENCIMIENTO")),
            "dias": days,
            "referencia": (raw.get("REFERENCIA") or "").strip(),
            "detalle": (raw.get("NAME") or "").strip(),
            "total_deuda": float(raw.get("TOTAL_DEUDA") or 0),
            "pagado": float(raw.get("PAGADO") or 0),
            "saldo": float(raw.get("SALDO") or 0),
            "egresos": raw.get("EGRESOS"),  # FK to VENT_FACT_VENT
            "tipo": (raw.get("TIPO") or "").strip(),
            "nro_deuda": raw.get("NRO_DEUDA"),
            "nro_total_deudas": raw.get("NRO_TOTAL_DEUDAS"),
        }
        items.append(item)
        total += item["total_deuda"]
        pagado += item["pagado"]
        saldo += item["saldo"]

    # Order matching the Theos PDF (FECHA, VENCIMIENTO ascending).
    items.sort(key=lambda r: (r.get("fecha") or "", r.get("vencimiento") or ""))

    partner = await _resolve_partner_header(client, int(client_id))

    return {
        "success": True,
        "partner": partner,
        "cutoff_date": cutoff_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "only_with_balance": only_with_balance,
        "only_overdue": only_overdue,
        "items": items,
        "totals": {
            "count": len(items),
            "total_deuda": round(total, 2),
            "pagado": round(pagado, 2),
            "saldo": round(saldo, 2),
        },
    }


async def get_customer_statement_pdf(
    client: VelneoClient,
    *,
    client_id: int,
    only_with_balance: bool = True,
    only_overdue: bool = False,
    cutoff_date: str | None = None,
) -> dict[str, Any]:
    """Render the customer statement as a PDF and return base64.

    The Velneo REST API has no PDF endpoint (the desktop form prints
    via Velneo's own report engine). We build the PDF here with
    reportlab so the bot can attach it directly to a Telegram /
    WhatsApp message.
    """
    data = await get_customer_statement(
        client,
        client_id=client_id,
        only_with_balance=only_with_balance,
        only_overdue=only_overdue,
        cutoff_date=cutoff_date,
    )
    if not data.get("success"):
        return data

    # Short-circuit: if the customer has zero open debts, don't render
    # a blank PDF — let the LLM tell the customer in plain text.
    if not data.get("items"):
        partner = data.get("partner") or {}
        return {
            "success": True,
            "no_debts": True,
            "partner": partner,
            "cutoff_date": data.get("cutoff_date"),
            "totals": data.get("totals") or {"count": 0, "saldo": 0,
                                              "pagado": 0, "total_deuda": 0},
            "item_count": 0,
            "message": (
                f"{partner.get('name') or 'El cliente'} no tiene deudas "
                f"pendientes a la fecha. No se genera PDF."
            ),
        }

    try:
        from mcp_theos.pdf import render_statement_pdf
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"PDF renderer unavailable: {type(exc).__name__}: {exc}",
        }

    import base64

    # Resolve brand from public.tenants — same pattern used by the OTP
    # path. Avoids hardcoding "Tecnosmart" / "Mepriga" in the renderer.
    from mcp_theos.otp import _get_tenant_commercial_name
    brand = await _get_tenant_commercial_name(client.cfg.tenant_id)

    try:
        pdf_bytes = render_statement_pdf(data, brand=brand)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"PDF render failed: {type(exc).__name__}: {exc}",
        }

    return {
        "success": True,
        "partner": data["partner"],
        "cutoff_date": data["cutoff_date"],
        "totals": data["totals"],
        "item_count": len(data["items"]),
        "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        "pdf_filename": (
            f"estado_cuenta_{data['partner'].get('cif') or client_id}"
            f"_{data['cutoff_date']}.pdf"
        ),
    }
