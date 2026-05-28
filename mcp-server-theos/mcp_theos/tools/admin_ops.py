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

from mcp_theos.tools.invoices import build_sri_number
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
