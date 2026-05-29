"""Operational read-only tools for internal support agents.

These tools serve the *internal* team running on the same MCP as the
customer-facing tools. They are deliberately read-only — no writes, no
SRI side-effects (SRI is handled by Datil / iFactura externally, not
Theos). Tool gating is the agent's responsibility (``enabled_tools``);
this module simply makes the surface available.

Naming convention: every tool here is purely diagnostic / inspection-
oriented and operates against the Velneo REST API only. Tables touched
follow the whitelist agreed for ``search_velneo``:

* Entry points: PRODUCTOS, VENT_FACT_VENT, VENT_ORDEN_VENTA,
  VENT_DEUD_CLIE, VENT_COBR_DEUD.
* Related (FK-followed only): ENT, ENT_ERP_CLI, VENT_ORDEN_MOVIMIENTOS,
  INV_MOVIMIENTOS, DETALLE_COBROS, DETALLE_COBROS_FORMAS, EXISTENCIAS,
  INV_BODEGA, and assorted catalogs.
"""

from __future__ import annotations

from typing import Any

from mcp_theos.tools.invoices import build_sri_number, parse_sri_number
from mcp_theos.velneo_http import VelneoClient, VelneoError, call_proceso_or_message

# ---------------------------------------------------------------------------
# Field projections — kept short to keep payloads small. Each tool adds
# whichever extra columns it needs on top of these via ``fields=[...]``.
# ---------------------------------------------------------------------------

_ENT_FIELDS = [
    "ID", "NAME", "NOM_COM", "CIF",
    "MAIL_PRINCIPAL", "TFN_PRI", "DIR_PRI",
    "SIN_CREDITO", "SIN_CREDITO_RAZON",
    "OFF",
]

_ENT_ERP_CLI_FIELDS = [
    "ID", "NAME", "CIF",
    "SALDO", "DEUDASC", "CUPOC", "DISPONIBLE_CUPOC",
    "DIAS_VENCIDOS", "FACTVENCIDAS", "NO_VENDER",
    "TIPO_CONTRIBUYENTE", "SRI_TIPO_IDENTIFICACION",
    "TIPO_CLIENTE", "DESCUENTOC",
    "OFF",
]

# See tools/invoices.py for the full allowlist rationale (this set
# mirrors the one used by get_customer_invoices). Key fields:
#   * SERIE + SECUENCIA → SRI number (NRO_FAC is blocked)
#   * RAZONSOCIALCOMPRADOR + SRI_IDENTIFICACION → customer (ENT_ERP_CLI blocked)
#   * LAST_STATUS → SRI state string (ESTADO_FEAP blocked)
_FACT_FIELDS = [
    "ID", "NAME", "FECHA",
    "SERIE", "SECUENCIA",
    "ESTABLECIMIENTO", "PUNTOEMISION",
    "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION",
    "SUBTOTAL", "BASE_IVA", "BASE0", "IVA", "TOTAL",
    "PAGADO", "SALDO",
    "VENDEDOR", "VTA_TIPO_ENT", "SUC", "EMP", "INV_BODEGA",
    "OFF", "OFF_MOTIVO", "REF", "REF2",
    "TIENE_ELECTRONICA", "SRI_TIPO_FEAP",
    "LAST_STATUS", "VCACCESOSRI", "AUTORIZACION", "KEY",
    "TIPO_AMBIENTE", "VENTA_CREDITO",
]

_ORDEN_FIELDS = [
    "ID", "NAME", "FECHA", "ENT_ERP_CLI", "VENDEDOR",
    "TOTAL", "PAGADO", "SALDO",
    "ESTADO", "FACTURADO", "OFF",
]

_DEUD_FIELDS = [
    "ID", "NAME", "FECHA", "VENCIMIENTO",
    "ENT_ERP_CLI", "EGRESOS",
    "TOTAL_DEUDA", "PAGADO", "SALDO",
    "DIAS", "DIAS_VENCIDOS",
    "CON_SALDO", "POR_VENCER", "COBRADO", "OFF",
    "REFERENCIA", "TIPO",
]

_COBR_FIELDS = [
    "ID", "NAME", "FECHA", "FECHA_CONTA",
    "ENT_ERP_CLI", "CAJERO",
    "VALOR", "VALOR_USADO", "SALDO",
    "SERIE", "SECUENCIA", "REFERENCIA",
    "OFF", "OFF_MOTIVO",
]

_INV_MOV_FIELDS = [
    "ID", "NUM_LINEA",
    "VENT_FACT_VENT", "VENT_ORDEN_VENTA",
    "PRODUCTOS", "NAME",
    "CAN", "FACTOR",
    "PVP", "PVP_LINEA", "PRECIO_NETO_LINEA",
    "INV_BODEGA",
]


def _short_date(value: Any) -> str:
    """``2026-05-16T00:00:00.000Z`` → ``2026-05-16`` (matches invoices.py)."""
    if not value or value == "Invalid Date":
        return ""
    s = str(value)
    return s[:10] if "T" in s else s


def _err(exc: VelneoError) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"velneo {exc.status}: {exc.message}",
        "velneo_status": exc.status,
    }


# ---------------------------------------------------------------------------
# inspect_partner — 360° view of a client for support diagnosis.
# ---------------------------------------------------------------------------


async def inspect_partner(
    client: VelneoClient,
    *,
    partner_id: int | None = None,
    cif: str | None = None,
) -> dict[str, Any]:
    """Pull ENT + ENT_ERP_CLI + recent activity snapshot for diagnosis.

    Use this when the user asks "qué pasa con el cliente X" or similar —
    it returns enough context to spot SIN_CREDITO, NO_VENDER, overdue
    debt, recent invoices and recent payments without forcing the agent
    to chain five tool calls.

    Identify the client by ``partner_id`` (ENT.ID == ENT_ERP_CLI.ID) or
    by ``cif`` (RUC / cédula). When both are passed ``partner_id`` wins.
    """
    if not partner_id and not cif:
        return {"success": False, "error": "partner_id or cif required"}

    # Resolve ENT row.
    if partner_id:
        try:
            ent = await client.get("ENT", record_id=partner_id, fields=_ENT_FIELDS)
        except VelneoError as exc:
            return _err(exc)
        ent_rows = ent.rows
    else:
        try:
            ent = await client.get(
                "ENT",
                params={"CIF": cif.strip(), "pagesize": 5},
                fields=_ENT_FIELDS,
            )
        except VelneoError as exc:
            return _err(exc)
        ent_rows = ent.rows

    if not ent_rows:
        return {
            "success": False,
            "error_code": "not_found",
            "error": f"partner not found (partner_id={partner_id} cif={cif!r})",
        }
    ent_row = ent_rows[0]
    pid = int(ent_row.get("ID") or 0)
    if not pid:
        return {"success": False, "error": "ENT row missing ID"}

    # ENT_ERP_CLI extension.
    erp_ext: dict[str, Any] | None = None
    try:
        ext = await client.get(
            "ENT_ERP_CLI", record_id=pid, fields=_ENT_ERP_CLI_FIELDS,
        )
        erp_ext = ext.rows[0] if ext.rows else None
    except VelneoError:
        erp_ext = None

    # Recent activity snapshot (small pages — diagnostic-grade, not for
    # full listing).
    snapshot: dict[str, Any] = {}
    for label, table, fields in (
        ("recent_invoices", "VENT_FACT_VENT", _FACT_FIELDS),
        ("recent_orders",   "VENT_ORDEN_VENTA", _ORDEN_FIELDS),
        ("open_debts",      "VENT_DEUD_CLIE", _DEUD_FIELDS),
        ("recent_payments", "VENT_COBR_DEUD", _COBR_FIELDS),
    ):
        try:
            resp = await client.get(
                table,
                params={"ENT_ERP_CLI": pid, "pagesize": 10},
                fields=fields,
            )
            rows = resp.rows
            if label == "open_debts":
                rows = [r for r in rows if not r.get("OFF")
                        and float(r.get("SALDO") or 0) > 0.01]
            snapshot[label] = rows
            snapshot[f"{label}_count"] = len(rows)
        except VelneoError as exc:
            snapshot[f"{label}_error"] = f"velneo {exc.status}: {exc.message}"

    flags: list[str] = []
    if ent_row.get("SIN_CREDITO"):
        flags.append("sin_credito")
    if ent_row.get("OFF"):
        flags.append("ent_off")
    if erp_ext:
        if erp_ext.get("NO_VENDER"):
            flags.append("no_vender")
        if erp_ext.get("OFF"):
            flags.append("erp_cli_off")
        if (erp_ext.get("DIAS_VENCIDOS") or 0):
            flags.append("dias_vencidos")
        if (erp_ext.get("FACTVENCIDAS") or 0):
            flags.append("facturas_vencidas")

    return {
        "success": True,
        "partner_id": pid,
        "ent": ent_row,
        "ent_erp_cli": erp_ext,
        "has_erp_cli": erp_ext is not None,
        "flags": flags,
        "snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# Invoice listings — shared engine, two public tools on top of it.
#
# Primary path: invoke ``ERP_APP/VENT_FACT_BUSQ_3P`` (Velneo proceso that
# runs the native VENT_FACT_VENT_BUSQ@erp Búsqueda on the server in 3rd
# plane). The proceso already orders by ID desc (newest first) and
# accepts NOM as a text token for the WORDS+PARTS index on NAME (which
# carries the customer NAME + RUC denormalized).
#
# Fallback path (used while the niko_saas API key lacks execute permit
# on caja ERP_APP): direct REST with ``sort=-FECHA`` for ordering and
# ``filter[words]=<token>`` for the same denormalized text search. Both
# paths return the same row shape so the LLM does not care which one
# resolved the call.
# ---------------------------------------------------------------------------


def _enrich_fact_row(r: dict[str, Any]) -> dict[str, Any]:
    """Add the derived ``NRO_FAC`` column to a row (SERIE + SECUENCIA)."""
    if isinstance(r, dict):
        r["NRO_FAC"] = build_sri_number(r.get("SERIE"), r.get("SECUENCIA"))
    return r


# Keys we keep from the 165-field proceso response. Everything else
# is internal Velneo bookkeeping (cesta refs, indexes, sub-relations)
# that just inflates the token bill without helping the LLM.
_PROCESO_FACT_KEEP = frozenset({
    "ID", "NAME", "FECHA",
    "SERIE", "SECUENCIA", "ESTABLECIMIENTO", "PUNTOEMISION",
    "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION",
    "CLIENTE",  # the proceso DOES return the ENT_ERP_CLI fk (REST blocks it)
    "SUBTOTAL", "BASE_IVA", "BASE0", "IVA", "TOTAL", "PAGADO", "SALDO",
    "VENDEDOR", "VTA_TIPO_ENT", "SUC", "EMP", "INV_BODEGA",
    "OFF", "OFF_MOTIVO", "REF", "REF2",
    "TIENE_ELECTRONICA", "SRI_TIPO_FEAP", "LAST_STATUS",
    "VCACCESOSRI", "AUTORIZACION", "KEY",
    "TIPO_AMBIENTE", "VENTA_CREDITO",
    "FECHA_FACT", "FECHA_CONTA",  # may be available via proceso
})


def _summarize_proceso_fact(r: dict[str, Any]) -> dict[str, Any]:
    """Project the 165-key proceso row down to ~30 useful keys + NRO_FAC."""
    if not isinstance(r, dict):
        return r
    out = {k: v for k, v in r.items() if k.upper() in _PROCESO_FACT_KEEP}
    out["NRO_FAC"] = build_sri_number(r.get("SERIE"), r.get("SECUENCIA"))
    return out


def _tenant_sucursal(client: VelneoClient) -> str:
    """Resolve the SUCURSAL value required by VENT_FACT_BUSQ_3P from the
    tenant's ``erp_api_extra`` config. Defaults to "001" (Mepriga value).
    """
    extra = getattr(client.cfg, "extra", None) or {}
    return str(extra.get("velneo_sucursal") or "001")


def _norm_velneo_date(d: str | None) -> str | None:
    """Caller passes ISO ``YYYY-MM-DD``; Velneo procesos consume that
    same shape (verified with visor_datos's date params). Returned
    unchanged so we can swap if a tenant requires ``DD/MM/YYYY``.
    """
    d = (d or "").strip()
    return d or None


async def _list_via_proceso(
    client: VelneoClient,
    *,
    nom: str | None,
    date_from: str | None,
    date_to: str | None,
    branch_id: int | None,
    include_off: bool,
    date_basis: str = "fact",
    mostrar_por_despachar: bool = False,
    mostrar_solo_pendientes_despachos: bool = False,
) -> dict[str, Any]:
    """Call ``VENT_FACT_BUSQ_3P``. Returns ``{ok, rows, ...}`` shape.

    Empirically verified against Mepriga (2026-05-28):

    * ``SUCURSAL`` is REQUIRED — without it the Búsqueda returns 0
      rows. The proper value is the EMP code string ("001"), not the
      numeric SUC. Pulled from ``cfg.extra.velneo_sucursal`` (default
      "001"). ``branch_id`` overrides per call.
    * ``NOM`` works via WORDS+PARTS index on NAME (carries customer
      RUC + razón social denormalized). "KLEINTURS" → 199 facturas.
    * Date range is activated by a SEPARATE FLAG variable, not by
      simply setting FCH_DES / FCH_HST. Without the flag the date
      vars are silently ignored. The two flags are mutually exclusive:
        - ``FCH_FACT=1`` → filter by fecha de emisión (FECHA)
        - ``FCH_CONTA=1`` → filter by fecha contable (FECHA_CONTA)
      We pick by the ``date_basis`` arg ("fact" default, "conta" for
      the contable view). Verified: range 2026-05-27..28 narrows
      KLEINTURS facturas 199 → 7 once FCH_FACT=1 is set.
    """
    params: dict[str, Any] = {
        # SUCURSAL is the gate that lets the Búsqueda return ANYTHING.
        "SUCURSAL": (str(branch_id) if branch_id is not None
                     else _tenant_sucursal(client)),
    }
    if nom:
        params["NOM"] = nom
    params["OFF"] = "1" if include_off else "0"
    # Date range — variables are ignored unless the corresponding flag
    # is set. Only activate the flag when at least one bound was given,
    # otherwise we pin everything to a default date with no upper bound
    # and lose results.
    if date_from or date_to:
        if date_basis == "conta":
            params["FCH_CONTA"] = "1"
        else:
            params["FCH_FACT"] = "1"
        if date_from:
            params["FCH_DES"] = date_from
        if date_to:
            params["FCH_HST"] = date_to
    if mostrar_por_despachar:
        params["MOSTRAR_POR_DESPACHAR"] = "1"
    if mostrar_solo_pendientes_despachos:
        params["MOSTRAR_SOLO_PENDIENTES_DESPACHOS"] = "1"
    resp = await call_proceso_or_message(
        client, "VENT_FACT_BUSQ_3P",
        params=params,
        row_keys=("vent_fact_vent",),
    )
    if resp.get("ok") and resp.get("rows"):
        resp["rows"] = [_summarize_proceso_fact(r) for r in resp["rows"]]
    return resp


async def _list_via_rest(
    client: VelneoClient,
    *,
    client_id: int | None,
    salesperson_id: int | None,
    nom_words: str | None,
    date_from: str | None,
    date_to: str | None,
    include_off: bool,
    limit: int,
) -> dict[str, Any]:
    """REST fallback when the proceso is unavailable.

    Pushes what Velneo REST supports: ``sort=-FECHA``,
    ``filter[ENT_ERP_CLI]``, ``filter[VENDEDOR]``, ``filter[words]``,
    exact ``filter[FECHA]``. Date ranges and SALDO > 0 still need to
    happen in-memory (Velneo REST has no range operators).
    """
    # Pull a bigger window than ``limit`` so the in-memory date /
    # SALDO filters have headroom before truncation. Cap at 500.
    pull = min(max(limit * 3, 20), 500)
    params: dict[str, Any] = {"sort": "-FECHA"}
    if client_id is not None:
        params["ENT_ERP_CLI"] = client_id
    if salesperson_id is not None:
        params["VENDEDOR"] = salesperson_id
    if nom_words:
        params["words"] = nom_words
    # Velneo REST exact-match only; if only one date is set we can map
    # it to ``filter[FECHA]=YYYY-MM-DD`` for that single day.
    if date_from and date_to and date_from == date_to:
        params["FECHA"] = date_from
    try:
        resp = await client.get(
            "VENT_FACT_VENT", params={**params, "pagesize": pull},
            fields=_FACT_FIELDS,
        )
    except VelneoError as exc:
        return {"ok": False, "transport_error": _err(exc)["error"]}

    rows = resp.rows
    if not include_off:
        rows = [r for r in rows if not r.get("OFF")]
    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "total_count": resp.total_count,
    }


def _apply_inmemory_filters(
    rows: list[dict[str, Any]],
    *,
    date_from: str | None,
    date_to: str | None,
    saldo_positive: bool,
    sri_status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    df = (date_from or "").strip()
    dt = (date_to or "").strip()
    want = (sri_status or "").strip().upper() or None
    out: list[dict[str, Any]] = []
    for r in rows:
        if saldo_positive and float(r.get("SALDO") or 0) <= 0.01:
            continue
        f = _short_date(r.get("FECHA"))
        if df and f and f < df:
            continue
        if dt and f and f > dt:
            continue
        if want:
            ls = str(r.get("LAST_STATUS") or "").strip().upper()
            if want not in ls:
                continue
        _enrich_fact_row(r)
        out.append(r)
        if len(out) >= limit:
            break
    return out


async def list_pending_invoices(
    client: VelneoClient,
    *,
    customer_query: str | None = None,
    client_id: int | None = None,
    salesperson_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_id: int | None = None,
    sri_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Active invoices with SALDO > 0 — collections-oriented.

    Pass ``customer_query`` (free-text token like "KLEINTURS") or
    ``client_id``. The free-text route goes through the Velneo Búsqueda
    via proceso (NOM variable) or, when the proceso isn't enabled, via
    the same WORDS index over REST.
    """
    nom = (customer_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    # Primary path — proceso.
    p = await _list_via_proceso(
        client, nom=nom, date_from=df, date_to=dt,
        branch_id=branch_id, include_off=False,
    )

    used_path = "proceso"
    rows: list[dict[str, Any]] = []
    permission_msg: str | None = None
    total_count = 0
    if p.get("ok"):
        rows = p["rows"]
        total_count = p.get("total_count") or len(rows)
    elif p.get("permission_denied"):
        permission_msg = p.get("message")
        # Fallback — REST.
        r = await _list_via_rest(
            client,
            client_id=client_id, salesperson_id=salesperson_id,
            nom_words=nom, date_from=df, date_to=dt,
            include_off=False, limit=limit,
        )
        used_path = "rest_fallback"
        if not r.get("ok"):
            return {"success": False, "error": r.get("transport_error") or "rest fallback failed"}
        rows = r["rows"]
        total_count = r.get("total_count") or len(rows)
    else:
        return {"success": False, "error": p.get("transport_error") or "proceso call failed"}

    # SALDO > 0 + date window + SRI status in-memory.
    out = _apply_inmemory_filters(
        rows,
        date_from=df, date_to=dt,
        saldo_positive=True, sri_status=sri_status, limit=limit,
    )

    return {
        "success": True,
        "path": used_path,
        "count": len(out),
        "total_scanned": len(rows),
        "filter": {
            "customer_query": nom,
            "client_id": client_id,
            "salesperson_id": salesperson_id,
            "date_from": df,
            "date_to": dt,
            "branch_id": branch_id,
            "sri_status": sri_status,
        },
        "invoices": out,
        **({"permission_denied_message": permission_msg} if permission_msg else {}),
    }


async def list_recent_invoices(
    client: VelneoClient,
    *,
    customer_query: str | None = None,
    client_id: int | None = None,
    salesperson_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_id: int | None = None,
    sri_status: str | None = None,
    include_off: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Recent invoices, newest first.

    Same engine as :func:`list_pending_invoices` but without the
    SALDO > 0 filter — useful for "qué pasó hoy con KLEINTURS" or
    "muéstrame las últimas facturas del vendedor X".

    SRI ``ESTADO`` filtering is currently unavailable: the ``ESTADO``
    column is projection-blocked under the niko_saas API key, so we
    cannot read or filter on it from the MCP. When the Velneo admin
    unblocks ``ESTADO`` projection, the agent can add an ``estado``
    argument here and we'll wire it through.
    """
    nom = (customer_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    p = await _list_via_proceso(
        client, nom=nom, date_from=df, date_to=dt,
        branch_id=branch_id, include_off=include_off,
    )

    used_path = "proceso"
    rows: list[dict[str, Any]] = []
    permission_msg: str | None = None
    total_count = 0
    if p.get("ok"):
        rows = p["rows"]
        total_count = p.get("total_count") or len(rows)
    elif p.get("permission_denied"):
        permission_msg = p.get("message")
        r = await _list_via_rest(
            client,
            client_id=client_id, salesperson_id=salesperson_id,
            nom_words=nom, date_from=df, date_to=dt,
            include_off=include_off, limit=limit,
        )
        used_path = "rest_fallback"
        if not r.get("ok"):
            return {"success": False, "error": r.get("transport_error") or "rest fallback failed"}
        rows = r["rows"]
        total_count = r.get("total_count") or len(rows)
    else:
        return {"success": False, "error": p.get("transport_error") or "proceso call failed"}

    out = _apply_inmemory_filters(
        rows,
        date_from=df, date_to=dt,
        saldo_positive=False, sri_status=sri_status, limit=limit,
    )

    return {
        "success": True,
        "path": used_path,
        "count": len(out),
        "total_scanned": len(rows),
        "filter": {
            "customer_query": nom,
            "client_id": client_id,
            "salesperson_id": salesperson_id,
            "date_from": df,
            "date_to": dt,
            "branch_id": branch_id,
            "sri_status": sri_status,
            "include_off": include_off,
        },
        "invoices": out,
        **({"permission_denied_message": permission_msg} if permission_msg else {}),
    }


async def list_invoices_pending_dispatch(
    client: VelneoClient,
    *,
    customer_query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_id: int | None = None,
    strict: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Facturas con líneas sin despachar (CAN_NO_DESP != 0).

    Uses ``MOSTRAR_POR_DESPACHAR=1`` + (optional)
    ``MOSTRAR_SOLO_PENDIENTES_DESPACHOS=1`` on the proceso. There is no
    REST equivalent (the despacho semantics live inside the proceso's
    nested plural-load over INV_MOVIMIENTOS); when the proceso is not
    enabled this tool returns the permission-denied message and the
    agent should ask the operator to grant ERP_APP execution.
    """
    nom = (customer_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    p = await _list_via_proceso(
        client, nom=nom, date_from=df, date_to=dt,
        branch_id=branch_id, include_off=False,
        mostrar_por_despachar=True,
        mostrar_solo_pendientes_despachos=strict,
    )
    if not p.get("ok"):
        if p.get("permission_denied"):
            return {
                "success": False,
                "error_code": "proceso_permission_denied",
                "error": p.get("message"),
                "hint": (
                    "Pídele al admin Velneo de este tenant que habilite "
                    "ejecución del proceso ERP_APP/VENT_FACT_BUSQ_3P para "
                    "el API key. Sin eso no hay forma de listar "
                    "pendientes de despacho desde el MCP."
                ),
            }
        return {"success": False, "error": p.get("transport_error") or "proceso call failed"}

    out = [_enrich_fact_row(r) for r in p["rows"][:limit]]
    return {
        "success": True,
        "path": "proceso",
        "count": len(out),
        "total_scanned": len(p["rows"]),
        "filter": {
            "customer_query": nom,
            "date_from": df, "date_to": dt,
            "branch_id": branch_id, "strict": strict,
        },
        "invoices": out,
    }


# ---------------------------------------------------------------------------
# get_invoice_detail — header + lines + linked debts + linked payments.
# ---------------------------------------------------------------------------


async def get_invoice_detail(
    client: VelneoClient,
    *,
    invoice_id: int | None = None,
    nro_fac: str | None = None,
) -> dict[str, Any]:
    """Single invoice with movement lines, generated debt rows, and the
    payment detail rows that have been applied against those debts.

    Identify by ``invoice_id`` (VENT_FACT_VENT.ID) or by ``nro_fac``
    (SRI invoice number string).
    """
    if not invoice_id and not nro_fac:
        return {"success": False, "error": "invoice_id or nro_fac required"}

    # Resolve header.
    if invoice_id:
        try:
            head = await client.get(
                "VENT_FACT_VENT", record_id=invoice_id, fields=_FACT_FIELDS,
            )
        except VelneoError as exc:
            return _err(exc)
    else:
        # NRO_FAC projection is blocked, but SERIE+SECUENCIA aren't —
        # decompose "001-001-565825" → SERIE="001-001", SECUENCIA=565825
        # and filter on those instead. Falls back to a NOM token search
        # if the decomposition doesn't match the expected shape.
        s = (nro_fac or "").strip()
        parts = s.split("-")
        params: dict[str, Any] = {"pagesize": 1}
        if len(parts) >= 3 and parts[-1].isdigit():
            params["SERIE"] = "-".join(parts[:-1])
            params["SECUENCIA"] = int(parts[-1])
        else:
            params["words"] = s  # last-resort token search via WORDS
        try:
            head = await client.get(
                "VENT_FACT_VENT", params=params, fields=_FACT_FIELDS,
            )
        except VelneoError as exc:
            return _err(exc)

    if not head.rows:
        return {
            "success": False,
            "error_code": "not_found",
            "error": f"invoice not found (id={invoice_id} nro_fac={nro_fac!r})",
        }
    header = _enrich_fact_row(head.rows[0])
    inv_id = int(header.get("ID") or 0)

    # Lines — primary path is the proceso ``VENT_FACT_MOV_BUSQ_3P``
    # with ID = invoice_id. Returns the INV_MOVIMIENTOS rows that
    # belong to this factura (the proceso handles the join). Fallback:
    # direct REST filter[VENT_FACT_VENT]=<inv_id> on INV_MOVIMIENTOS,
    # which works without the ERP_APP permit but loses the proceso's
    # native ordering / despacho-status enrichment.
    lines: list[dict[str, Any]] = []
    lines_error: str | None = None
    lines_path = "proceso"
    # VENT_FACT_MOV_BUSQ_3P requires SUCURSAL (same gate as the parent
    # Búsqueda VENT_FACT_VENT_BUSQ). Without it the proceso returns 0
    # rows. The header we already loaded has EMP, but the proceso uses
    # the tenant-default establecimiento code from cfg.extra.
    mov_resp = await call_proceso_or_message(
        client, "VENT_FACT_MOV_BUSQ_3P",
        params={"ID": inv_id, "SUCURSAL": _tenant_sucursal(client)},
        row_keys=("inv_movimientos",),
    )
    if mov_resp.get("ok"):
        lines = mov_resp["rows"]
    elif mov_resp.get("permission_denied"):
        lines_path = "rest_fallback"
        try:
            ln = await client.get(
                "INV_MOVIMIENTOS",
                params={"VENT_FACT_VENT": inv_id, "pagesize": 500},
                fields=_INV_MOV_FIELDS,
            )
            lines = ln.rows
        except VelneoError as exc:
            lines_error = f"velneo {exc.status}: {exc.message}"
    else:
        lines_error = mov_resp.get("transport_error") or "proceso call failed"

    # Debts generated by this invoice (VENT_DEUD_CLIE where EGRESOS = inv_id).
    debts: list[dict[str, Any]] = []
    debts_error: str | None = None
    try:
        d = await client.get(
            "VENT_DEUD_CLIE",
            params={"VENT_FACT_VENT": inv_id, "pagesize": 100},
            fields=_DEUD_FIELDS,
        )
        debts = d.rows
    except VelneoError as exc:
        debts_error = f"velneo {exc.status}: {exc.message}"

    # Payments applied to the debts generated by this invoice. We pull
    # DETALLE_COBROS rows for each debt id, plus the parent COBROS
    # header. Capped at 200 detail rows to keep the response sane.
    payment_lines: list[dict[str, Any]] = []
    payment_error: str | None = None
    if debts:
        debt_ids = [d.get("ID") for d in debts if d.get("ID") is not None]
        try:
            for did in debt_ids[:20]:  # sane cap
                resp = await client.get(
                    "DETALLE_COBROS",
                    params={"VENT_DEUD_CLIE": did, "pagesize": 50},
                    fields=[
                        "ID", "NAME", "COBROS", "VENT_DEUD_CLIE",
                        "VALOR", "SALDO_ACTUAL", "NUEVO_SALDO",
                        "FECHA_CONTA",
                    ],
                )
                payment_lines.extend(resp.rows)
                if len(payment_lines) >= 200:
                    break
        except VelneoError as exc:
            payment_error = f"velneo {exc.status}: {exc.message}"

    out: dict[str, Any] = {
        "success": True,
        "invoice_id": inv_id,
        "header": header,
        "lines": lines,
        "line_count": len(lines),
        "lines_path": lines_path,
        "debts": debts,
        "debt_count": len(debts),
        "payment_applications": payment_lines,
        "payment_application_count": len(payment_lines),
    }
    if lines_error:
        out["lines_error"] = lines_error
    if debts_error:
        out["debts_error"] = debts_error
    if payment_error:
        out["payment_error"] = payment_error
    return out


# ---------------------------------------------------------------------------
# list_recent_stock_movements — INV_MOVIMIENTOS for a product/bodega.
# ---------------------------------------------------------------------------


async def list_recent_stock_movements(
    client: VelneoClient,
    *,
    product_id: int | None = None,
    bodega_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Most recent ``INV_MOVIMIENTOS`` rows, optionally narrowed by
    product or bodega. Used for "por qué el stock dice X" forensics.

    At least one of ``product_id`` / ``bodega_id`` is required —
    pulling every movement in the ERP without a filter would walk
    millions of rows.
    """
    if not product_id and not bodega_id:
        return {
            "success": False,
            "error": "at least one of product_id, bodega_id is required",
        }
    params: dict[str, Any] = {"pagesize": min(max(limit, 1), 500)}
    if product_id:
        params["PRODUCTOS"] = product_id
    if bodega_id:
        params["INV_BODEGA"] = bodega_id

    try:
        resp = await client.get(
            "INV_MOVIMIENTOS", params=params, fields=_INV_MOV_FIELDS,
        )
    except VelneoError as exc:
        return _err(exc)

    return {
        "success": True,
        "filter": {"product_id": product_id, "bodega_id": bodega_id},
        "count": len(resp.rows),
        "total_count": resp.total_count,
        "movements": resp.rows[:limit],
    }


# Stock-by-bodega lives in ``products.check_stock`` (it reads the
# denormalized EXS_BOD1..12 / INV_BODEGA1..12 columns on the PRODUCTOS
# master row — the Theos-native pattern). Audit-trail forensics on
# stock movements stays here under ``list_recent_stock_movements``.


# ---------------------------------------------------------------------------
# generate_sales_report — XLSX deliverable for the daily ops report
# ---------------------------------------------------------------------------


async def generate_sales_report(
    client: VelneoClient,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    sucursal: str | None = None,
    max_rows: int = 5000,
) -> dict[str, Any]:
    """Generate the "Informe de ventas diarias" XLSX for a date range.

    Mirrors the layout the Mepriga operator generates manually from the
    vClient (Facturas Ventas → Exportar → Detalle de ventas + pivot):

    * ``INFORME`` sheet: pivot Fecha × (Familia Principal × Bodega) of
      ``Suma de PVP Linea``, with daily and grand totals.
    * ``VENTAS_DETALLE`` sheet: the 29-column raw line-level data.

    Default range is today (Quito timezone) so "lila, dame el informe
    de ventas" without a date returns the report as of right now.

    Returns ``{xlsx_base64, xlsx_filename, totals, message}``. Lila's
    channel layer attaches the XLSX to Telegram automatically when it
    sees ``xlsx_base64`` + ``xlsx_filename`` in the tool response.
    """
    import base64
    from datetime import date, datetime, timezone, timedelta

    # Default to today in Ecuador (UTC-5).
    if not date_from and not date_to:
        ec_now = datetime.now(timezone.utc) - timedelta(hours=5)
        today_iso = ec_now.date().isoformat()
        date_from = today_iso
        date_to = today_iso
    if not date_from:
        date_from = date_to
    if not date_to:
        date_to = date_from

    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)
    if not df or not dt:
        return {"success": False, "error": "date_from / date_to must be ISO YYYY-MM-DD"}

    from mcp_theos.sales_report import generate as _gen
    result = await _gen(
        client, date_from=df, date_to=dt,
        sucursal=sucursal, max_rows=max_rows,
    )
    if not result.get("success"):
        return result

    xlsx_bytes = result.pop("xlsx_bytes", None)
    if not xlsx_bytes:
        return {
            "success": True,
            "no_data": True,
            "message": result.get("message"),
            "totals": result.get("totals"),
        }

    fname_range = df if df == dt else f"{df}_a_{dt}"
    return {
        **{k: v for k, v in result.items() if k != "xlsx_bytes"},
        "xlsx_base64": base64.b64encode(xlsx_bytes).decode("ascii"),
        "xlsx_filename": f"informe_ventas_mepriga_{fname_range}.xlsx",
        "mime_type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    }


# ---------------------------------------------------------------------------
# B1 find_invoice — flexible lookup by ID, SRI number, or text token.
# ---------------------------------------------------------------------------


async def find_invoice(
    client: VelneoClient,
    *,
    query: str | None = None,
    invoice_id: int | None = None,
    nro_fac: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Locate one or more invoices from whatever the user typed.

    Resolution paths (first non-empty wins):

    * ``invoice_id``      → direct ``GET VENT_FACT_VENT/{id}``
    * ``nro_fac`` (SRI)   → parse "001-001-565825" → SERIE + SECUENCIA
                            filter (canonical SRI lookup)
    * ``query``           → tries in order:
        - looks like SRI ("001-001-565825") → SRI parse path
        - all-digits, ≤ 9 chars → treat as SECUENCIA
        - otherwise → ``filter[words]=<query>`` (Velneo WORDS index
          over NAME — catches "kleinturs", "klein", an SRI access
          key fragment, or a partial REFERENCIA)

    Returns ``{success, count, invoices: [...]}``. Always sets
    ``NRO_FAC`` per row via :func:`build_sri_number` so the caller
    sees the SRI document number regardless of which path matched.
    """
    if not (query or invoice_id or nro_fac):
        return {"success": False, "error": "query, invoice_id or nro_fac required"}

    rows: list[dict[str, Any]] = []
    used: dict[str, Any] = {}

    # Path 1 — direct by ID.
    if invoice_id:
        used["invoice_id"] = invoice_id
        try:
            r = await client.get(
                "VENT_FACT_VENT", record_id=int(invoice_id),
                fields=_FACT_FIELDS,
            )
            rows = r.rows
        except VelneoError as exc:
            return _err(exc)

    # Path 2 — explicit SRI number.
    if not rows and nro_fac:
        parsed = parse_sri_number(nro_fac)
        if parsed:
            used["nro_fac"] = parsed["padded"]
            try:
                r = await client.get(
                    "VENT_FACT_VENT",
                    params={"SERIE": parsed["serie"],
                            "SECUENCIA": parsed["secuencia_int"],
                            "pagesize": limit},
                    fields=_FACT_FIELDS,
                )
                rows = r.rows
            except VelneoError as exc:
                return _err(exc)
        else:
            used["nro_fac_raw"] = nro_fac

    # Path 3 — free-text query.
    if not rows and query:
        q = query.strip()
        parsed = parse_sri_number(q)
        if parsed:
            used["query_parsed_as_sri"] = parsed["padded"]
            try:
                r = await client.get(
                    "VENT_FACT_VENT",
                    params={"SERIE": parsed["serie"],
                            "SECUENCIA": parsed["secuencia_int"],
                            "pagesize": limit},
                    fields=_FACT_FIELDS,
                )
                rows = r.rows
            except VelneoError as exc:
                return _err(exc)
        elif q.isdigit() and len(q) <= 9:
            used["query_as_secuencia"] = int(q)
            try:
                r = await client.get(
                    "VENT_FACT_VENT",
                    params={"SECUENCIA": int(q), "pagesize": limit},
                    fields=_FACT_FIELDS,
                )
                rows = r.rows
            except VelneoError as exc:
                return _err(exc)
        else:
            used["query_as_words"] = q
            try:
                r = await client.get(
                    "VENT_FACT_VENT",
                    params={"words": q, "pagesize": limit, "sort": "-FECHA"},
                    fields=_FACT_FIELDS,
                )
                rows = r.rows
            except VelneoError as exc:
                return _err(exc)

    invoices = [_enrich_fact_row(r) for r in rows[:limit]]

    return {
        "success": True,
        "lookup": used,
        "count": len(invoices),
        "invoices": invoices,
    }


# ---------------------------------------------------------------------------
# B3 list_documents_window — multi-doc-type window for a customer.
# ---------------------------------------------------------------------------


# Stable projection per document table — verified against Mepriga
# 2026-05-28. Each list is the allowed subset under niko_saas.
_NC_FIELDS = [
    # Identifiers / dates
    "ID", "NAME", "FECHA", "FECHA_FACT",
    "SERIE", "SECUENCIA", "ESTABLECIMIENTO", "PUNTOEMISION",
    # Customer (NC ventas use ENT_ERP_CLI — this column IS allowed on
    # VENT_NOTA_CRED unlike its sibling on VENT_FACT_VENT)
    "ENT_ERP_CLI",
    "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION",
    # Amounts (PAGADO blocked here — NC tracks SALDO only)
    "TOTAL", "SUBTOTAL", "BASE_IVA", "BASE0", "IVA", "SALDO",
    # Op metadata + SRI block
    "OFF", "OFF_MOTIVO",
    "LAST_STATUS", "VCACCESOSRI", "AUTORIZACION", "KEY",
    "TIENE_ELECTRONICA", "SRI_TIPO_FEAP", "TIPO_AMBIENTE",
    "EMP", "SUC", "INV_BODEGA",
    # FK to parent invoice (NCs are issued AGAINST a factura)
    "VENT_FACT_VENT",
]

_COMP_RET_FIELDS = [
    # SERIE / SECUENCIA are blocked here — NC has its own NRO_RET
    # but it lives in NAME. ENT_ERP_PROV is the proveedor.
    "ID", "NAME", "FECHA", "FECHA_RET",
    "ESTABLECIMIENTO", "PUNTOEMISION",
    "ENT_ERP_PROV",
    "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION",
    "TOTAL", "BASE_IVA", "BASE0", "IVA",
    # Withholding amounts (these are the actual retention values)
    "RET_FUE1", "RET_FUE2", "RET_FUE3",
    "RET_IVA1", "RET_IVA2",
    "CODE_RET_FUE1", "CODE_RET_IVA1",
    # SRI block (AUTORIZACION blocked on this table — only KEY +
    # LAST_STATUS + VCACCESOSRI carry the SRI signal)
    "OFF", "OFF_MOTIVO",
    "LAST_STATUS", "VCACCESOSRI", "KEY",
    "TIENE_ELECTRONICA", "TIPO_AMBIENTE",
    "EMP", "SUC",
]

_CONT_COMPRAS_FIELDS = [
    "ID", "NAME", "FECHA", "FECHA_FACT",
    "SERIE", "SECUENCIA", "ESTABLECIMIENTO", "PUNTOEMISION",
    "ENT_ERP_PROV",
    "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION",
    "TOTAL", "BASE_IVA", "BASE0", "IVA",
    "PAGADO", "SALDO",
    "OFF", "OFF_MOTIVO",
    "LAST_STATUS", "VCACCESOSRI", "AUTORIZACION", "KEY",
    "TIENE_ELECTRONICA", "TIPO_AMBIENTE",
    "EMP", "SUC",
]

# COMP_DEUD_PROV (cuentas por pagar). Allowlist verified 2026-05-28
# against Mepriga. Blocked: ENT, CIF (read via WORDS on NAME instead),
# EGRESOS, VENT_FACT_VENT, CRUZADO, RETENIDO, TIPO, NRO_DEUDA,
# NRO_TOTAL_DEUDAS. The supplier's RUC + razón social are embedded in
# NAME ("0993405590001 DISPROINCO S.A.S. Ingreso 3803 Ref 001-001-17")
# so we can identify the proveedor without joining ENT_ERP_PROV (which
# is GET-blocked under this API key).
_COMP_DEUD_FIELDS = [
    "ID", "NAME", "FECHA", "VENCIMIENTO",
    "ALT_TIM", "MOD_TIM",
    "ENT_ERP_PROV", "CONT_COMPRAS",
    "TOTAL_DEUDA", "PAGADO", "SALDO",
    "DIAS", "DIAS_VENCIDOS",
    "CON_SALDO", "POR_VENCER", "COBRADO",
    "OFF", "OFF_MOTIVO",
    "REFERENCIA",
    "EMP", "SUC", "DIVISIONES",
    "BANDERA", "FECHA_CONTA",
]


def _doc_summary(doc_type: str) -> dict[str, Any]:
    """Empty summary scaffold per document type."""
    return {"type": doc_type, "count": 0, "items": []}


async def list_documents_window(
    client: VelneoClient,
    *,
    customer_query: str | None = None,
    client_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    types: list[str] | None = None,
    limit_per_type: int = 20,
) -> dict[str, Any]:
    """Cross-document listing in a date window.

    Pulls (in parallel) the recent activity of a customer across all
    document types they can have. Defaults to all known types; pass
    ``types`` to narrow:

      ``"invoices"``     → VENT_FACT_VENT
      ``"orders"``       → VENT_ORDEN_VENTA
      ``"debts"``        → VENT_DEUD_CLIE
      ``"payments"``     → VENT_COBR_DEUD
      ``"credit_notes"`` → VENT_NOTA_CRED
      ``"withholdings"`` → COMP_RETENCIONES (compra)

    Either ``customer_query`` (free-text WORDS) or ``client_id``
    narrows by partner; without either, the window is global.
    """
    import asyncio

    nom = (customer_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)
    df_short = df or ""
    dt_short = dt or ""
    want = set(types or
               ["invoices", "orders", "debts", "payments",
                "credit_notes", "withholdings"])
    page = min(max(limit_per_type * 2, 20), 200)

    def _params(extra: dict[str, Any]) -> dict[str, Any]:
        p: dict[str, Any] = {"pagesize": page, "sort": "-FECHA"}
        if client_id is not None:
            p["ENT_ERP_CLI"] = client_id
        if nom:
            p["words"] = nom
        p.update(extra)
        return p

    async def _pull(label: str, table: str, fields: list[str]) -> tuple[str, list[dict[str, Any]]]:
        try:
            r = await client.get(table, params=_params({}), fields=fields)
        except VelneoError as exc:
            return label, [{"_error": f"velneo {exc.status}: {exc.message}"}]
        rows = r.rows
        # In-memory date narrowing — REST cannot do > / < operators.
        out: list[dict[str, Any]] = []
        for row in rows:
            f = _short_date(row.get("FECHA"))
            if df_short and f and f < df_short:
                continue
            if dt_short and f and f > dt_short:
                continue
            if label in ("invoices", "credit_notes"):
                _enrich_fact_row(row)
            out.append(row)
            if len(out) >= limit_per_type:
                break
        return label, out

    plan = []
    if "invoices" in want:
        plan.append(_pull("invoices", "VENT_FACT_VENT", _FACT_FIELDS))
    if "orders" in want:
        plan.append(_pull("orders", "VENT_ORDEN_VENTA", _ORDEN_FIELDS))
    if "debts" in want:
        plan.append(_pull("debts", "VENT_DEUD_CLIE", _DEUD_FIELDS))
    if "payments" in want:
        plan.append(_pull("payments", "VENT_COBR_DEUD", _COBR_FIELDS))
    if "credit_notes" in want:
        plan.append(_pull("credit_notes", "VENT_NOTA_CRED", _NC_FIELDS))
    if "withholdings" in want:
        plan.append(_pull("withholdings", "COMP_RETENCIONES", _COMP_RET_FIELDS))

    results = await asyncio.gather(*plan)

    by_type: dict[str, dict[str, Any]] = {}
    for label, rows in results:
        by_type[label] = {
            "type": label,
            "count": len(rows),
            "items": rows,
        }

    return {
        "success": True,
        "filter": {
            "customer_query": nom,
            "client_id": client_id,
            "date_from": df, "date_to": dt,
            "types": sorted(want),
        },
        "documents": by_type,
        "total_count": sum(v["count"] for v in by_type.values()),
    }


# ---------------------------------------------------------------------------
# B4 search_by_amount — filter invoices / payments by amount range.
# ---------------------------------------------------------------------------


async def search_by_amount(
    client: VelneoClient,
    *,
    doc_type: str = "invoices",
    amount_min: float | None = None,
    amount_max: float | None = None,
    customer_query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Find docs whose TOTAL (or VALOR, for cobros) is in ``[min, max]``.

    Velneo's REST has no range operator on numeric columns, so we pull
    a wide window (filter by NOM / fechas) and apply the amount filter
    in memory. Use a NOM filter or a narrow date window or both — a
    naked search without either pulls only the first page of the table
    and is unlikely to find what the operator is looking for.

    ``doc_type``: "invoices" | "payments" | "credit_notes".
    """
    nom = (customer_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    table, amt_field, fields = {
        "invoices":     ("VENT_FACT_VENT", "TOTAL",   _FACT_FIELDS),
        "payments":     ("VENT_COBR_DEUD", "VALOR",   _COBR_FIELDS),
        "credit_notes": ("VENT_NOTA_CRED", "TOTAL",   _NC_FIELDS),
    }.get(doc_type, (None, None, None))
    if table is None:
        return {
            "success": False,
            "error": f"doc_type {doc_type!r} not supported "
                     "(use invoices / payments / credit_notes)",
        }

    params: dict[str, Any] = {
        "pagesize": min(max(limit * 4, 50), 500),
        "sort": "-FECHA",
    }
    if nom:
        params["words"] = nom

    try:
        resp = await client.get(table, params=params, fields=fields)
    except VelneoError as exc:
        return _err(exc)

    lo = float(amount_min) if amount_min is not None else float("-inf")
    hi = float(amount_max) if amount_max is not None else float("inf")
    out: list[dict[str, Any]] = []
    for r in resp.rows:
        try:
            amt = float(r.get(amt_field) or 0)
        except (TypeError, ValueError):
            continue
        if amt < lo or amt > hi:
            continue
        f = _short_date(r.get("FECHA"))
        if df and f and f < df:
            continue
        if dt and f and f > dt:
            continue
        if table in ("VENT_FACT_VENT", "VENT_NOTA_CRED"):
            _enrich_fact_row(r)
        out.append(r)
        if len(out) >= limit:
            break

    return {
        "success": True,
        "doc_type": doc_type,
        "table": table,
        "amount_field": amt_field,
        "count": len(out),
        "total_scanned": len(resp.rows),
        "filter": {
            "amount_min": amount_min, "amount_max": amount_max,
            "customer_query": nom,
            "date_from": df, "date_to": dt,
        },
        "documents": out,
    }


# ---------------------------------------------------------------------------
# B5 search_invoice_lines_by_product — INV_MOVIMIENTOS where the line
# belongs to a sales invoice (VENT_FACT_VENT != 0) AND PRODUCTOS = id.
# ---------------------------------------------------------------------------


async def search_invoice_lines_by_product(
    client: VelneoClient,
    *,
    product_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Movement lines for a product that sit on sales invoices.

    Useful for returns / claims ("¿en cuáles facturas vendimos este
    producto el mes pasado?"). Pulls INV_MOVIMIENTOS with
    ``filter[PRODUCTOS]=<id>`` and keeps only rows where
    ``VENT_FACT_VENT`` is set — that excludes purchase / transfer /
    adjustment movements while reusing the same indexed filter.
    """
    if not product_id:
        return {"success": False, "error": "product_id required"}

    try:
        resp = await client.get(
            "INV_MOVIMIENTOS",
            params={"PRODUCTOS": int(product_id),
                    "pagesize": min(max(limit * 3, 50), 500),
                    "sort": "-ID"},
            fields=_INV_MOV_FIELDS,
        )
    except VelneoError as exc:
        return _err(exc)

    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)
    out: list[dict[str, Any]] = []
    for r in resp.rows:
        if not r.get("VENT_FACT_VENT"):
            continue  # not a sale line
        # No FECHA on the movement; date narrowing requires resolving
        # the parent invoice, which would explode the call. Caller can
        # pull a fresh invoice header per line with ``get_invoice_detail``
        # if the date matters.
        out.append(r)
        if len(out) >= limit:
            break

    return {
        "success": True,
        "product_id": int(product_id),
        "count": len(out),
        "total_scanned": len(resp.rows),
        "filter": {
            "product_id": int(product_id),
            "date_from": df, "date_to": dt,
        },
        "lines": out,
    }


# ---------------------------------------------------------------------------
# list_invoice_lines_window — INV_MOVIMIENTOS rows of sales invoices
# inside a date window. Uses the proceso VENT_FACT_MOV_BUSQ_3P with the
# FCH_FACT=1 flag so the date range is honored server-side.
# ---------------------------------------------------------------------------


# Keys we keep from the 130-key proceso movement row. Same project-down
# discipline as _summarize_proceso_fact.
_PROCESO_MOV_KEEP = frozenset({
    "ID", "NAME", "NUM_LINEA",
    "VENT_FACT_VENT", "VENT_NOTA_CRED", "CONT_COMPRAS",
    "PRODUCTOS", "INV_PRESENT_PRODUCTO",
    "CAN", "FACTOR",
    "PVP", "PVP_LINEA",
    "PRECIO_BRUTO_LINEA", "PRECIO_NETO_LINEA",
    "DCTO_VTAS_LINEA", "IVA_LINEA",
    "INV_BODEGA",
    "EMP", "SUC",
    "CONTADO", "FECHA_CONTA",
    "MOV_TIP", "ENTRADA",
    "CLI_ENT", "PRV_ENT",
})


def _summarize_proceso_mov(r: dict[str, Any]) -> dict[str, Any]:
    """Project the 130-key proceso movement row down to ~25 useful keys."""
    if not isinstance(r, dict):
        return r
    return {k: v for k, v in r.items() if k.upper() in _PROCESO_MOV_KEEP}


async def list_invoice_lines_window(
    client: VelneoClient,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    customer_query: str | None = None,
    branch_id: int | None = None,
    date_basis: str = "fact",
    include_off: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """All sales-invoice line items in a date window.

    Pattern for "detalle de ventas del día / semana / mes". Uses
    ``VENT_FACT_MOV_BUSQ_3P`` (the Búsqueda that traverses parent
    VENT_FACT_VENT → child INV_MOVIMIENTOS via plural relation) with:

      * ``SUCURSAL`` — required gate (from cfg.extra.velneo_sucursal
        or branch_id override).
      * ``FCH_FACT=1`` — activates the date filter on FECHA de emisión.
        Without this flag FCH_DES / FCH_HST are silently ignored and
        you get the whole 102k-fact dataset (∼1.86k KLEINTURS lines or
        the equivalent global cap). ``date_basis="conta"`` switches
        to FCH_CONTA for fecha contable.
      * ``FCH_DES`` / ``FCH_HST`` — the actual window.
      * ``NOM`` — optional customer WORDS filter.

    Empirically verified: ``date_range 27..28 May 2026 + FCH_FACT=1``
    narrowed KLEINTURS movs from 1860 → 92.

    Returns the proceso rows summarized to ~25 useful keys. The
    proceso caps at 1000 rows; if ``total_count >= 1000`` the response
    sets ``truncated=true`` so the caller knows to narrow the window.
    There is no page[number] equivalent — pagination = narrower date
    range or per-invoice fetch via ``get_invoice_detail``.
    """
    nom = (customer_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)
    params: dict[str, Any] = {
        "SUCURSAL": (str(branch_id) if branch_id is not None
                     else _tenant_sucursal(client)),
        "OFF": "1" if include_off else "0",
    }
    if nom:
        params["NOM"] = nom
    if df or dt:
        if date_basis == "conta":
            params["FCH_CONTA"] = "1"
        else:
            params["FCH_FACT"] = "1"
        if df:
            params["FCH_DES"] = df
        if dt:
            params["FCH_HST"] = dt

    resp = await call_proceso_or_message(
        client, "VENT_FACT_MOV_BUSQ_3P",
        params=params,
        row_keys=("inv_movimientos",),
    )
    if not resp.get("ok"):
        if resp.get("permission_denied"):
            return {
                "success": False,
                "error_code": "proceso_permission_denied",
                "error": resp.get("message"),
            }
        return {
            "success": False,
            "error": resp.get("transport_error") or "proceso call failed",
        }

    rows = [_summarize_proceso_mov(r) for r in resp["rows"]]
    total = resp.get("total_count") or len(rows)
    truncated = total >= 1000

    return {
        "success": True,
        "path": "proceso",
        "count": min(len(rows), limit),
        "returned": len(rows),
        "total_count": total,
        "truncated": truncated,
        "filter": {
            "customer_query": nom,
            "date_from": df, "date_to": dt,
            "date_basis": date_basis,
            "branch_id": branch_id,
            "include_off": include_off,
        },
        "lines": rows[:limit],
        **({"truncated_hint": (
            "El proceso devolvió >= 1000 líneas; narrow date_from/"
            "date_to o agrega customer_query para ver el detalle "
            "completo."
        )} if truncated else {}),
    }


# ---------------------------------------------------------------------------
# list_credit_notes (VENT_NOTA_CRED) — sales credit notes
# ---------------------------------------------------------------------------


async def list_credit_notes(
    client: VelneoClient,
    *,
    customer_query: str | None = None,
    client_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sri_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Notas de crédito de ventas (VENT_NOTA_CRED), newest first.

    Each NC carries a link to the parent invoice in
    ``VENT_FACT_VENT`` so the agent can chain ``get_invoice_detail``
    to see what was being credited. SRI block (LAST_STATUS,
    VCACCESOSRI, AUTORIZACION, KEY) lives directly on the row — NCs
    have their own electronic signature lifecycle.
    """
    nom = (customer_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    params: dict[str, Any] = {
        "sort": "-FECHA",
        "pagesize": min(max(limit * 2, 20), 500),
    }
    if client_id is not None:
        params["ENT_ERP_CLI"] = client_id
    if nom:
        params["words"] = nom

    try:
        resp = await client.get("VENT_NOTA_CRED", params=params, fields=_NC_FIELDS)
    except VelneoError as exc:
        return _err(exc)

    out = _apply_inmemory_filters(
        resp.rows,
        date_from=df, date_to=dt,
        saldo_positive=False, sri_status=sri_status, limit=limit,
    )

    return {
        "success": True,
        "count": len(out),
        "total_scanned": len(resp.rows),
        "filter": {
            "customer_query": nom, "client_id": client_id,
            "date_from": df, "date_to": dt, "sri_status": sri_status,
        },
        "credit_notes": out,
    }


# ---------------------------------------------------------------------------
# list_withholdings (COMP_RETENCIONES) — retenciones en compras
# ---------------------------------------------------------------------------


async def list_withholdings(
    client: VelneoClient,
    *,
    supplier_query: str | None = None,
    supplier_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sri_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Comprobantes de retención emitidos a proveedores (COMP_RETENCIONES).

    Returns the retention amounts split per category: RET_FUE1..3
    (fuente) + RET_IVA1..2 (IVA). CODE_RET_FUE1 / CODE_RET_IVA1
    carry the SRI tax code. NAME contains the SRI number (SERIE +
    SECUENCIA are projection-blocked on this table). Filter by
    supplier via the NOM WORDS index (``supplier_query``) or by
    explicit ``supplier_id`` (ENT_ERP_PROV).
    """
    nom = (supplier_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    params: dict[str, Any] = {
        "sort": "-FECHA",
        "pagesize": min(max(limit * 2, 20), 500),
    }
    if supplier_id is not None:
        params["ENT_ERP_PROV"] = supplier_id
    if nom:
        params["words"] = nom

    try:
        resp = await client.get("COMP_RETENCIONES", params=params, fields=_COMP_RET_FIELDS)
    except VelneoError as exc:
        return _err(exc)

    out = _apply_inmemory_filters(
        resp.rows,
        date_from=df, date_to=dt,
        saldo_positive=False, sri_status=sri_status, limit=limit,
    )

    return {
        "success": True,
        "count": len(out),
        "total_scanned": len(resp.rows),
        "filter": {
            "supplier_query": nom, "supplier_id": supplier_id,
            "date_from": df, "date_to": dt, "sri_status": sri_status,
        },
        "withholdings": out,
    }


# ---------------------------------------------------------------------------
# list_purchase_invoices (CONT_COMPRAS) — facturas de compras
# ---------------------------------------------------------------------------


async def list_purchase_invoices(
    client: VelneoClient,
    *,
    supplier_query: str | None = None,
    supplier_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sri_status: str | None = None,
    only_unpaid: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Facturas de compras (CONT_COMPRAS), newest first.

    Mirror of ``list_recent_invoices`` for the procurement side.
    Filter by supplier via NOM WORDS (``supplier_query``) or
    explicit ENT_ERP_PROV id. ``only_unpaid=true`` keeps rows with
    SALDO > 0 (accounts-payable view).
    """
    nom = (supplier_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    params: dict[str, Any] = {
        "sort": "-FECHA",
        "pagesize": min(max(limit * 2, 20), 500),
    }
    if supplier_id is not None:
        params["ENT_ERP_PROV"] = supplier_id
    if nom:
        params["words"] = nom

    try:
        resp = await client.get("CONT_COMPRAS", params=params,
                                  fields=_CONT_COMPRAS_FIELDS)
    except VelneoError as exc:
        return _err(exc)

    out = _apply_inmemory_filters(
        resp.rows,
        date_from=df, date_to=dt,
        saldo_positive=only_unpaid, sri_status=sri_status, limit=limit,
    )
    # Same NRO_FAC composition as sales invoices.
    for r in out:
        _enrich_fact_row(r)

    return {
        "success": True,
        "count": len(out),
        "total_scanned": len(resp.rows),
        "filter": {
            "supplier_query": nom, "supplier_id": supplier_id,
            "date_from": df, "date_to": dt,
            "sri_status": sri_status, "only_unpaid": only_unpaid,
        },
        "purchase_invoices": out,
    }


# ---------------------------------------------------------------------------
# identify_supplier — proveedor lookup (ENT_ERP_PROV is GET-blocked, so
# we go through COMP_DEUD_PROV / CONT_COMPRAS which carry the supplier
# name + RUC denormalized in NAME)
# ---------------------------------------------------------------------------


_SUPPLIER_PROBE_DEBT_FIELDS = [
    "ID", "NAME", "ENT_ERP_PROV", "TOTAL_DEUDA", "SALDO",
    "FECHA", "VENCIMIENTO", "DIAS_VENCIDOS",
]
_SUPPLIER_PROBE_INV_FIELDS = [
    "ID", "NAME", "FECHA", "ENT_ERP_PROV",
    "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION",
    "TOTAL", "PAGADO", "SALDO",
]


async def identify_supplier(
    client: VelneoClient,
    *,
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Resolve a proveedor from a free-text query.

    ``ENT_ERP_PROV/{id}`` direct GET is **405** under the niko_saas
    API key, so we cannot read provider master rows directly. Instead
    we use COMP_DEUD_PROV.NAME (carries "RUC + Razón Social + Ingreso"
    inline) via the WORDS index, then fall back to CONT_COMPRAS for
    suppliers without open debts.

    Returns up to ``limit`` distinct suppliers, each with:

        { supplier_id (ENT_ERP_PROV),
          display_name (parsed from NAME),
          ruc (parsed from NAME / SRI_IDENTIFICACION when available),
          open_debt_count, open_debt_saldo,
          recent_invoice_count, sample_rows: [...]  }

    DOES NOT delegate to identify_customer — proveedores live in
    ENT_ERP_PROV (different from ENT_ERP_CLI) and the partner_embeddings
    RAG only indexes customers. Calling identify_customer for a
    supplier query would silently return wrong rows.
    """
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "query required"}

    # Stage 1 — Velneo's native supplier debt search via swagger spec
    # (lowercase params). saldados=1 includes paid debts so we can
    # surface suppliers without open debts via this same path.
    try:
        body = await client.query(
            "comp_deud_prov_busq",
            params={
                "sucursal": _tenant_sucursal(client),
                "nom": q,
                "saldados": "1",
            },
            page_size=50,
        )
    except VelneoError as exc:
        return _err(exc)
    rows_raw = body.get("comp_deud_prov") or []
    from mcp_theos.velneo_http import _upper_keys
    debt_rows = [_upper_keys(r) for r in rows_raw]

    # Group by ENT_ERP_PROV. The NAME field of each debt row carries
    # ("0993405590001 DISPROINCO S.A.S. Ingreso ...") so we lift the
    # first numeric token = RUC and the next words up to "Ingreso" as
    # the display name.
    import re
    by_pid: dict[int, dict[str, Any]] = {}
    for r in debt_rows:
        if r.get("OFF"):
            continue
        pid = r.get("ENT_ERP_PROV")
        if not pid:
            continue
        pid_i = int(pid)
        name = (r.get("NAME") or "").strip()
        ruc_match = re.match(r"^\s*(\d{10,13})\s+", name)
        ruc = ruc_match.group(1) if ruc_match else None
        display = name
        if ruc_match:
            tail = name[ruc_match.end():]
            display = re.split(r"\bIngreso\b|\bRef\b", tail, maxsplit=1)[0].strip(" -")
        slot = by_pid.setdefault(pid_i, {
            "supplier_id": pid_i,
            "display_name": display,
            "ruc": ruc,
            "open_debt_count": 0,
            "open_debt_saldo": 0.0,
            "recent_invoice_count": 0,
            "sample_rows": [],
        })
        if float(r.get("SALDO") or 0) > 0.01:
            slot["open_debt_count"] += 1
            slot["open_debt_saldo"] += float(r.get("SALDO") or 0)
        if len(slot["sample_rows"]) < 3:
            slot["sample_rows"].append({
                "id": r.get("ID"),
                "fecha": _short_date(r.get("FECHA")),
                "saldo": float(r.get("SALDO") or 0),
            })

    # Stage 2 — if nothing came back from debts, try CONT_COMPRAS so
    # suppliers without open debts are still findable.
    if not by_pid:
        try:
            inv = await client.get(
                "CONT_COMPRAS",
                params={"words": q, "pagesize": 20, "sort": "-FECHA"},
                fields=_SUPPLIER_PROBE_INV_FIELDS,
            )
        except VelneoError as exc:
            return _err(exc)
        for r in inv.rows:
            pid = r.get("ENT_ERP_PROV")
            if not pid:
                continue
            pid_i = int(pid)
            ruc = (r.get("SRI_IDENTIFICACION") or "").strip() or None
            display = (r.get("RAZONSOCIALCOMPRADOR") or "").strip() or \
                      (r.get("NAME") or "").strip()
            slot = by_pid.setdefault(pid_i, {
                "supplier_id": pid_i,
                "display_name": display,
                "ruc": ruc,
                "open_debt_count": 0, "open_debt_saldo": 0.0,
                "recent_invoice_count": 0,
                "sample_rows": [],
            })
            slot["recent_invoice_count"] += 1

    matches = sorted(
        ({**v, "open_debt_saldo": round(v["open_debt_saldo"], 2)}
         for v in by_pid.values()),
        key=lambda x: (-x["open_debt_saldo"], -x["recent_invoice_count"]),
    )[:limit]

    out: dict[str, Any] = {
        "success": True,
        "found": bool(matches),
        "count": len(matches),
        "matches": matches,
    }
    # Auto-pick supplier_id at top level if there is exactly one
    # unambiguous match (saldo > 0 or invoices > 0).
    if len(matches) == 1:
        m = matches[0]
        out["supplier_id"] = m["supplier_id"]
        out["id"] = m["supplier_id"]
        out["name"] = m["display_name"]
        out["vat"] = m["ruc"]
    return out


# ---------------------------------------------------------------------------
# list_supplier_debts (COMP_DEUD_PROV) — cuentas por pagar
# ---------------------------------------------------------------------------


async def list_supplier_debts(
    client: VelneoClient,
    *,
    supplier_query: str | None = None,
    supplier_id: int | None = None,
    cont_compras_id: int | None = None,
    only_with_balance: bool = True,
    only_overdue: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Deudas a proveedores — Mepriga "Cuentas por Pagar".

    Empirically (2026-05-28 incident DISPROINCO) the WORDS index on
    COMP_DEUD_PROV is incomplete — ``filter[words]=DISPROINCO`` returns
    only 3 stale rows and misses the active debt rows. The Velneo
    native query ``_query/deudas_prov_con_saldo`` is the reliable
    path: returns the global ~390 supplier debts WITH SALDO > 0 in
    a single page, which we then filter in-memory by supplier name or
    ENT_ERP_PROV id. That gives the same answer the operator sees in
    the vClient "Deudas a Proveedores" UI.

    Filtering:

    * ``supplier_query`` — case-insensitive substring over NAME
      (which carries "RUC + Razón Social + Ingreso ..." inline). Use
      this for free-text queries like "DISPROINCO".
    * ``supplier_id`` — exact ENT_ERP_PROV match. Cheaper if you
      already know the supplier id.
    * ``cont_compras_id`` — narrow to the debts generated by one
      specific purchase invoice. Falls back to the direct
      COMP_DEUD_PROV table query because ``_query`` does not honor
      this filter.

    Returns aggregated totals + per-supplier breakdown to mirror the
    "TOTAL: PROVEEDOR X.XX" footer pattern of the Velneo PDF report.
    """
    nom = (supplier_query or "").strip() or None
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)
    df_s = df or ""
    dt_s = dt or ""

    from mcp_theos.velneo_http import _upper_keys

    if cont_compras_id is not None:
        # Specific-invoice lookup — direct table filter.
        try:
            resp = await client.get(
                "COMP_DEUD_PROV",
                params={"CONT_COMPRAS": cont_compras_id, "pagesize": 200},
                fields=_COMP_DEUD_FIELDS,
            )
            rows = resp.rows
        except VelneoError as exc:
            return _err(exc)
    elif nom:
        # Primary path — Velneo's native supplier debt search.
        # Verified vs swagger (lowercase params): param[nom] over the
        # WORDS+PARTS index that the vClient UI uses; saldados=0 (default)
        # excludes already-paid debts. This is server-side and accurate
        # for "qué le debo a X" queries.
        proc_params = {
            "sucursal": _tenant_sucursal(client),
            "nom": nom,
        }
        if not only_with_balance:
            proc_params["saldados"] = "1"
        try:
            body = await client.query(
                "comp_deud_prov_busq",
                params=proc_params,
                page_size=min(max(limit * 3, 50), 200),
            )
        except VelneoError as exc:
            return _err(exc)
        rows_raw = body.get("comp_deud_prov") or []
        rows = [_upper_keys(r) for r in rows_raw]
    else:
        # No supplier filter — fall back to "all suppliers with saldo".
        try:
            body = await client.query(
                "deudas_prov_con_saldo",
                page_size=500,
            )
        except VelneoError as exc:
            return _err(exc)
        rows_raw = body.get("comp_deud_prov") or []
        rows = [_upper_keys(r) for r in rows_raw]

    # In-memory narrow.
    items: list[dict[str, Any]] = []
    total_deuda = pagado = saldo = 0.0
    by_supplier: dict[int, dict[str, Any]] = {}
    matched_any = False
    for r in rows:
        if r.get("OFF"):
            continue
        if only_with_balance and float(r.get("SALDO") or 0) <= 0.01:
            continue
        if only_overdue:
            try:
                dv = int(r.get("DIAS_VENCIDOS") or 0)
            except (TypeError, ValueError):
                dv = 0
            if dv <= 0:
                continue
        if supplier_id is not None and int(r.get("ENT_ERP_PROV") or 0) != int(supplier_id):
            continue
        if nom and nom.upper() not in (r.get("NAME") or "").upper():
            continue
        f = _short_date(r.get("FECHA"))
        if df_s and f and f < df_s:
            continue
        if dt_s and f and f > dt_s:
            continue
        matched_any = True
        items.append(r)
        total_deuda += float(r.get("TOTAL_DEUDA") or 0)
        pagado += float(r.get("PAGADO") or 0)
        saldo += float(r.get("SALDO") or 0)
        pv = r.get("ENT_ERP_PROV")
        if pv is not None:
            slot = by_supplier.setdefault(int(pv), {
                "supplier_id": int(pv),
                "debt_count": 0, "saldo": 0.0,
                "display_name": _supplier_display(r),
            })
            slot["debt_count"] += 1
            slot["saldo"] += float(r.get("SALDO") or 0)
        if len(items) >= limit:
            break

    suppliers = sorted(
        ({**v, "saldo": round(v["saldo"], 2)} for v in by_supplier.values()),
        key=lambda x: -x["saldo"],
    )

    return {
        "success": True,
        "count": len(items),
        "total_scanned": len(rows),
        "path": "_query/deudas_prov_con_saldo" if cont_compras_id is None else "direct_table",
        "filter": {
            "supplier_query": nom, "supplier_id": supplier_id,
            "cont_compras_id": cont_compras_id,
            "only_with_balance": only_with_balance,
            "only_overdue": only_overdue,
            "date_from": df, "date_to": dt,
        },
        "items": items,
        "totals": {
            "total_deuda": round(total_deuda, 2),
            "pagado": round(pagado, 2),
            "saldo": round(saldo, 2),
        },
        "by_supplier": suppliers,
    }


def _supplier_display(row: dict[str, Any]) -> str:
    """Parse "RUC NOMBRE Ingreso XXX Ref YYY" → "NOMBRE"."""
    import re
    name = (row.get("NAME") or "").strip()
    m = re.match(r"^\s*(\d{10,13})\s+(.+?)(?:\s+Ingreso\b|\s+Ref\b|$)", name)
    if m:
        return m.group(2).strip()
    return name


# ---------------------------------------------------------------------------
# get_open_debts — wrapper around _query/deudas_cli_con_saldo
# ---------------------------------------------------------------------------


_STMT_DEUD_FIELDS = [
    "ID", "NAME", "FECHA", "VENCIMIENTO",
    "ENT_ERP_CLI", "EGRESOS",
    "TOTAL_DEUDA", "PAGADO", "CRUZADO", "SALDO", "RETENIDO",
    "DIAS", "DIAS_VENCIDOS",
    "CON_SALDO", "POR_VENCER", "COBRADO", "OFF",
    "REFERENCIA",
    "NRO_DEUDA", "NRO_TOTAL_DEUDAS", "TIPO",
]


async def get_open_debts(
    client: VelneoClient,
    *,
    client_id: int | None = None,
    customer_query: str | None = None,
    only_overdue: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Customer debts with SALDO > 0 via Velneo's native query.

    Wraps ``_query/deudas_cli_con_saldo`` — the Búsqueda the ERP UI
    uses for its own "deudas con saldo" view. Pre-filtered server-side
    (SALDO > 0 + OFF != 1) so the agent does NOT need to page through
    closed debts to find the open ones.

    Identifying a customer via ``client_id`` is the cheapest path
    (single integer filter). ``customer_query`` falls back to the
    direct VENT_DEUD_CLIE WORDS path with the customer name embedded
    inline (since the query endpoint does not natively honor a NOM
    var on this Búsqueda — verified empirically).

    Returns aggregated totals (total_deuda, pagado, saldo) + per-debt
    items + a small ``by_age_bucket`` summary (0-30 / 31-60 / 61-90 /
    90+) to support aging reports.
    """
    df = _norm_velneo_date(date_from)
    dt = _norm_velneo_date(date_to)

    filters: dict[str, Any] = {}
    if client_id is not None:
        filters["ENT_ERP_CLI"] = client_id

    try:
        body = await client.query(
            "deudas_cli_con_saldo",
            filters=filters,
            page_size=min(max(limit * 2, 50), 500),
        )
    except VelneoError as exc:
        return _err(exc)

    rows_raw = body.get("vent_deud_clie") or []
    from mcp_theos.velneo_http import _upper_keys
    rows = [_upper_keys(r) for r in rows_raw]

    # Optional WORDS narrow when no client_id — happens here because
    # the _query endpoint does not honor a NOM param on this Búsqueda.
    if customer_query and client_id is None:
        try:
            extra = await client.get(
                "VENT_DEUD_CLIE",
                params={"words": customer_query.strip(),
                        "pagesize": min(max(limit * 2, 50), 500),
                        "sort": "-FECHA"},
                fields=_STMT_DEUD_FIELDS,
            )
            existing_ids = {r.get("ID") for r in rows}
            for r in extra.rows:
                if not r.get("OFF") and float(r.get("SALDO") or 0) > 0.01:
                    if r.get("ID") not in existing_ids:
                        rows.append(r)
        except VelneoError:
            pass  # words fallback is best-effort

    # In-memory narrow.
    df_s = df or ""
    dt_s = dt or ""
    items: list[dict[str, Any]] = []
    total_deuda = pagado = saldo = 0.0
    aging = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "90_plus": 0.0,
             "not_yet_due": 0.0}
    for r in rows:
        if r.get("OFF"):
            continue
        if float(r.get("SALDO") or 0) <= 0.01:
            continue
        try:
            dv = int(r.get("DIAS_VENCIDOS") or 0)
        except (TypeError, ValueError):
            dv = 0
        if only_overdue and dv <= 0:
            continue
        f = _short_date(r.get("FECHA"))
        if df_s and f and f < df_s:
            continue
        if dt_s and f and f > dt_s:
            continue
        items.append(r)
        sal = float(r.get("SALDO") or 0)
        total_deuda += float(r.get("TOTAL_DEUDA") or 0)
        pagado += float(r.get("PAGADO") or 0)
        saldo += sal
        if dv <= 0:
            aging["not_yet_due"] += sal
        elif dv <= 30:
            aging["0_30"] += sal
        elif dv <= 60:
            aging["31_60"] += sal
        elif dv <= 90:
            aging["61_90"] += sal
        else:
            aging["90_plus"] += sal
        if len(items) >= limit:
            break

    items.sort(key=lambda x: (x.get("FECHA") or "", x.get("VENCIMIENTO") or ""))

    return {
        "success": True,
        "path": "_query/deudas_cli_con_saldo",
        "count": len(items),
        "total_scanned": len(rows),
        "filter": {
            "client_id": client_id,
            "customer_query": (customer_query or "").strip() or None,
            "only_overdue": only_overdue,
            "date_from": df, "date_to": dt,
        },
        "items": items,
        "totals": {
            "total_deuda": round(total_deuda, 2),
            "pagado": round(pagado, 2),
            "saldo": round(saldo, 2),
        },
        "by_age_bucket": {k: round(v, 2) for k, v in aging.items()},
    }


# ---------------------------------------------------------------------------
# velneo_query — generic Velneo Búsqueda invocation
# ---------------------------------------------------------------------------


# Whitelist of Búsquedas the agent is allowed to invoke directly. Some
# (corte_*) are documented in the swagger but returned 0 in every probe
# — they need specific tenant-side params we have not yet discovered.
# We expose them so an operator can experiment, but mark which ones are
# verified vs untested.
_QUERY_WHITELIST = {
    "deudas_cli_con_saldo":              "verified",
    "vent_deud_clie_busq":               "verified",
    "vent_deud_clie_busq1":              "verified",
    "vent_fact_vent_busq":                "verified — accepts param[SUCURSAL] + param[NOM]",
    "vent_fact_vent_busq_ats":           "untested — ATS report filter",
    "vent_fact_vent_code_list":          "untested",
    "productos_busq":                     "untested",
    "productos_cod_bar":                  "untested",
    "presen_busq":                        "untested",
    "buscar_cod_bar":                     "untested",
    "cod_bar_parts":                      "untested",
    # The corte_* family — Velneo documents them but they require
    # specific params (likely cut date + EMP + maybe SUCURSAL). Keep
    # them in the allow list so the agent can experiment.
    "corte_deudas_clientes":             "untested — needs unknown params",
    "corte_deudas_vs_pagos_clientes":    "untested — needs unknown params",
    "vent_deud_clie_para_fecha":          "untested — needs unknown params",
}


async def velneo_query(
    client: VelneoClient,
    *,
    name: str,
    filters: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    page_size: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """Generic invocation of a Velneo ``_query/<name>`` Búsqueda.

    Used when none of the dedicated tools fits. Restricted to the
    whitelist (see ``_QUERY_WHITELIST``) to keep the surface bounded.
    Filters use ``filter[FIELD]=value`` syntax (REST-style). Params
    use ``param[VAR]=value`` (proceso-style) — some Búsquedas accept
    both shapes and combine them.

    Returns the raw envelope ``{count, total_count, <table>: [...]}``
    plus the resolved row list under ``rows`` for convenience.
    """
    key = (name or "").strip().lower()
    if key not in _QUERY_WHITELIST:
        return {
            "success": False,
            "error_code": "query_not_whitelisted",
            "error": f"query {name!r} not in whitelist",
            "allowed_queries": sorted(_QUERY_WHITELIST),
        }
    try:
        body = await client.query(
            key, filters=filters, params=params,
            page_size=page_size, page=page,
        )
    except VelneoError as exc:
        return _err(exc)

    rows: list[dict[str, Any]] = []
    from mcp_theos.velneo_http import _upper_keys
    if isinstance(body, dict):
        for k, v in body.items():
            if k in {"errors", "count", "total_count"}:
                continue
            if isinstance(v, list):
                rows = [_upper_keys(r) for r in v if isinstance(r, dict)]
                break

    return {
        "success": True,
        "name": key,
        "status": _QUERY_WHITELIST[key],
        "count": body.get("count") if isinstance(body, dict) else len(rows),
        "total_count": body.get("total_count") if isinstance(body, dict) else len(rows),
        "rows": rows,
        "raw": body,
    }


# ---------------------------------------------------------------------------
# F4 partner_360 — inspect_partner + aging buckets + recent payments
# ---------------------------------------------------------------------------


async def partner_360(
    client: VelneoClient,
    *,
    partner_id: int | None = None,
    cif: str | None = None,
    customer_query: str | None = None,
) -> dict[str, Any]:
    """Audit-grade panoramic of a customer.

    Like ``inspect_partner`` but richer:

    * pulls open debts via ``_query/deudas_cli_con_saldo`` (server-side
      filter — accurate, doesn't risk missing rows past the first
      page)
    * computes a saldo aging bucket breakdown (0-30 / 31-60 / 61-90 /
      90+ / not_yet_due) from DIAS_VENCIDOS
    * adds last 10 payments via VENT_COBR_DEUD
    * surfaces SIN_CREDITO + NO_VENDER + total bucket signals as flags

    Use this for auditoría or "cuéntame todo de KLEINTURS"; for a
    quick "saldo de X" prefer the lighter ``inspect_partner`` or
    ``check_balance``.
    """
    # Identify partner (prefer fastest path).
    pid: int | None = None
    ent_row: dict[str, Any] = {}
    erp_ext: dict[str, Any] = {}
    if partner_id:
        try:
            ent = await client.get("ENT", record_id=partner_id, fields=_ENT_FIELDS)
            if ent.rows:
                ent_row = ent.rows[0]
                pid = int(ent_row.get("ID") or 0)
        except VelneoError as exc:
            return _err(exc)
    elif cif or customer_query:
        from mcp_theos.tools.partners import identify_customer
        ident = await identify_customer(
            client, cif=cif, name=customer_query,
        )
        if not ident.get("found"):
            return {
                "success": False,
                "error_code": "partner_not_found",
                "error": "No identifiqué al cliente.",
                "lookup": ident,
            }
        first = ident["matches"][0]
        if first.get("_match_via") in ("words", "rag"):
            return {
                "success": False,
                "error_code": "needs_disambiguation",
                "error": "Encontré coincidencias por aproximación; pide confirmación.",
                "matches": ident["matches"][:5],
            }
        ent_row = first
        pid = int(first.get("ID") or 0)
    else:
        return {"success": False, "error": "partner_id, cif, or customer_query required"}

    if not pid:
        return {"success": False, "error": "could not resolve partner"}

    # ERP_CLI ext.
    try:
        ext = await client.get(
            "ENT_ERP_CLI", record_id=pid, fields=_ENT_ERP_CLI_FIELDS,
        )
        erp_ext = ext.rows[0] if ext.rows else {}
    except VelneoError:
        erp_ext = {}

    # Open debts via _query (server-side SALDO > 0).
    open_debts = await get_open_debts(client, client_id=pid, limit=200)

    # Recent payments via direct table.
    payments: list[dict[str, Any]] = []
    try:
        resp = await client.get(
            "VENT_COBR_DEUD",
            params={"ENT_ERP_CLI": pid, "pagesize": 10, "sort": "-FECHA"},
            fields=_COBR_FIELDS,
        )
        payments = resp.rows
    except VelneoError:
        pass

    flags: list[str] = []
    if ent_row.get("SIN_CREDITO"):
        flags.append("sin_credito")
    if ent_row.get("OFF"):
        flags.append("ent_off")
    if erp_ext.get("NO_VENDER"):
        flags.append("no_vender")
    if erp_ext.get("OFF"):
        flags.append("erp_cli_off")
    try:
        if int(erp_ext.get("DIAS_VENCIDOS") or 0) > 0:
            flags.append("dias_vencidos")
    except (TypeError, ValueError):
        pass
    try:
        if int(erp_ext.get("FACTVENCIDAS") or 0) > 0:
            flags.append("facturas_vencidas")
    except (TypeError, ValueError):
        pass
    buckets = open_debts.get("by_age_bucket") or {}
    if (buckets.get("90_plus") or 0) > 0:
        flags.append("debt_90_plus")
    if (buckets.get("61_90") or 0) > 0:
        flags.append("debt_61_90")

    return {
        "success": True,
        "partner_id": pid,
        "ent": ent_row,
        "ent_erp_cli": erp_ext,
        "has_erp_cli": bool(erp_ext),
        "flags": flags,
        "open_debts": open_debts.get("items") or [],
        "open_debt_count": open_debts.get("count") or 0,
        "open_debt_totals": open_debts.get("totals") or {},
        "by_age_bucket": buckets,
        "recent_payments": payments,
        "recent_payment_count": len(payments),
    }


# ---------------------------------------------------------------------------
# F1 customer_full_view — composite tool: identify + activity sweep
# ---------------------------------------------------------------------------


async def customer_full_view(
    client: VelneoClient,
    *,
    customer_query: str | None = None,
    client_id: int | None = None,
    cif: str | None = None,
    days: int = 90,
) -> dict[str, Any]:
    """One-call panoramic of a customer.

    Resolves the customer (delegates to identify_customer's path
    order — exact / WORDS / RAG via the same partner module) and
    then pulls their last ``days`` of activity across all document
    types (invoices, orders, debts, payments, credit notes) in
    PARALLEL. Use this whenever the operator asks "qué pasa con
    KLEINTURS" or "muéstrame todo de este cliente": it cuts the
    typical 4-5 follow-up tool calls down to one.

    Returns ``{ partner, snapshot: {invoices: {...}, debts: {...},
    payments: {...}, ...}, totals: {...} }``. The agent should then
    summarize in natural language; do NOT dump the raw payload
    verbatim to the user.
    """
    from mcp_theos.tools.partners import identify_customer
    from datetime import datetime, timedelta, timezone

    # ── Resolve the partner ──────────────────────────────────────────
    partner_lookup: dict[str, Any] = {}
    if client_id is not None:
        # Need to confirm the partner exists + get NAME for the windowed
        # search. identify_customer accepts ruc/cif so go via the cif
        # path when we already know the id by reading ENT directly.
        try:
            ent = await client.get("ENT", record_id=client_id, fields=[
                "ID", "NAME", "NOM_COM", "CIF", "MAIL_PRINCIPAL", "TFN_PRI",
                "SIN_CREDITO", "OFF",
            ])
            if not ent.rows:
                return {"success": False, "error": f"client_id {client_id} not found"}
            partner_lookup = {
                "matches": [ent.rows[0]],
                "found": True, "count": 1,
                "partner_id": ent.rows[0].get("ID"),
                "id": ent.rows[0].get("ID"),
                "name": ent.rows[0].get("NAME") or "",
                "vat": ent.rows[0].get("CIF") or "",
            }
        except VelneoError as exc:
            return _err(exc)
    else:
        partner_lookup = await identify_customer(
            client, cif=cif, name=customer_query,
        )
        if not partner_lookup.get("found"):
            return {
                "success": False,
                "error_code": "partner_not_found",
                "error": "No pude identificar al cliente con esos datos.",
                "lookup": partner_lookup,
            }
        # When the partner came back as a fuzzy match (WORDS / RAG),
        # ask the LLM to disambiguate BEFORE pulling activity — the
        # last thing we want is the agent confidently showing the
        # wrong customer's account because of a typo.
        first = partner_lookup["matches"][0]
        if first.get("_match_via") in ("words", "rag"):
            return {
                "success": False,
                "error_code": "needs_disambiguation",
                "error": (
                    "Encontré coincidencias por aproximación. Por favor "
                    "confirma con el usuario cuál cliente es antes de "
                    "consultar su actividad."
                ),
                "matches": partner_lookup.get("matches", [])[:5],
            }

    pid = partner_lookup.get("partner_id") or partner_lookup.get("id")
    if not pid:
        return {
            "success": False,
            "error_code": "needs_disambiguation",
            "error": "Múltiples clientes coinciden. Confirma con el usuario.",
            "matches": partner_lookup.get("matches", [])[:5],
        }

    # ── Compute the date window ──────────────────────────────────────
    today = datetime.now(timezone.utc).date()
    from_d = (today - timedelta(days=days)).isoformat()
    to_d = today.isoformat()

    # ── Pull activity in parallel ────────────────────────────────────
    docs = await list_documents_window(
        client,
        client_id=int(pid),
        date_from=from_d, date_to=to_d,
        types=["invoices", "orders", "debts", "payments",
               "credit_notes"],
        limit_per_type=15,
    )

    # ── Totals from open_debts (everything the customer still owes) ──
    debts = docs["documents"].get("debts", {}).get("items", [])
    open_debts = [d for d in debts
                  if not d.get("OFF") and float(d.get("SALDO") or 0) > 0.01]
    total_saldo = sum(float(d.get("SALDO") or 0) for d in open_debts)
    overdue = [d for d in open_debts if int(d.get("DIAS") or 0) > 0]

    partner_summary = partner_lookup.get("matches", [{}])[0]

    return {
        "success": True,
        "partner": {
            "id": partner_summary.get("ID"),
            "name": partner_summary.get("NAME"),
            "commercial_name": partner_summary.get("NOM_COM"),
            "cif": partner_summary.get("CIF"),
            "email": partner_summary.get("MAIL_PRINCIPAL"),
            "phone": partner_summary.get("TFN_PRI"),
            "saldo": partner_summary.get("SALDO"),
            "cupo": partner_summary.get("CUPOC"),
            "disponible_cupo": partner_summary.get("DISPONIBLE_CUPOC"),
            "dias_vencidos": partner_summary.get("DIAS_VENCIDOS"),
            "flags": partner_summary.get("_flags") or [],
        },
        "window_days": days,
        "window": {"from": from_d, "to": to_d},
        "activity": docs["documents"],
        "totals": {
            "total_count": docs.get("total_count"),
            "open_debt_count": len(open_debts),
            "open_debt_saldo": round(total_saldo, 2),
            "overdue_count": len(overdue),
        },
    }
