"""XLSX sales report generator for Mepriga / Velneo.

Two sheets:

* ``INFORME`` — pivot ``Familia Principal × Bodega`` per FECHA, with daily
  and grand totals. Same layout as the operator's manual report.
* ``VENTAS_DETALLE`` — line-level data, one row per INV_MOVIMIENTOS entry
  (the same 25-column layout the Velneo UI exports via "Detalle de ventas").

Data source
-----------

The proceso ``VENT_FACT_MOV_BUSQ_3P`` returns INV_MOVIMIENTOS rows in the
requested date window with ~130 fields per row. We summarize down to the
13 useful ones, then resolve FKs once (cached) for:

* INV_BODEGA → bodega name
* INV_FAMI → familia name
* INV_SUBFAMI (via PRODUCTOS) → subfamilia name
* PRODUCTOS → codigo + INV_FAMI/SUBFAMI

The XLSX is built with ``openpyxl`` directly (no pandas). Aggregations
are computed with plain ``collections.defaultdict`` — fast enough for
the 100k row caps we hit in practice.
"""

from __future__ import annotations

import io
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from mcp_theos.velneo_http import VelneoClient, VelneoError, call_proceso_or_message

logger = logging.getLogger(__name__)


# Fields we keep from the 130-key proceso row.
_KEEP_KEYS = frozenset({
    "id", "vent_fact_vent",
    "productos", "inv_present_producto",
    "nombre", "abrev", "factor", "can",
    "fecha",
    "inv_bodega",
    "inv_fami",
    "cod_bar",
    "precio_bruto_empaque", "iva_empaque", "costo_empaque",
    "precio_neto_empaque",
    "porcentaje_dscto_vta", "porcentaje_utilidad2",
    "pvp", "pvp_linea",
    "costo_linea", "precio_neto_linea", "iva_linea",
    "dcto_vtas_linea",
    "emp", "suc",
    "clt_ent",
})


def _short_date(value: Any) -> str:
    if not value or value == "Invalid Date":
        return ""
    s = str(value)
    return s[:10] if "T" in s else s


def _fmt_date_es(d: str) -> str:
    """``2026-05-26`` → ``26/05/2026``."""
    if not d or len(d) < 10:
        return d
    return f"{d[8:10]}/{d[5:7]}/{d[0:4]}"


async def _resolve_lookup(
    client: VelneoClient, table: str, name_field: str = "NAME",
    pagesize: int = 500,
) -> dict[int, str]:
    """Pull all rows of a small lookup table and return ``{id: NAME}``."""
    try:
        resp = await client.get(
            table, params={"pagesize": pagesize},
            fields=["ID", name_field],
            use_cache=True,
        )
    except VelneoError as exc:
        logger.warning("lookup %s failed: %s", table, exc)
        return {}
    out: dict[int, str] = {}
    for r in resp.rows:
        rid = r.get("ID")
        if rid is None:
            continue
        out[int(rid)] = str(r.get(name_field) or "").strip()
    return out


async def _resolve_products(
    client: VelneoClient, product_ids: set[int], chunk: int = 100,
) -> dict[int, dict[str, Any]]:
    """For each PRODUCTOS.ID, return {CODIGO, INV_FAMI, INV_SUBFAMI, NAME}.

    Pulls by record_id in chunks since Velneo REST has no IN operator
    (each chunk is N sequential GETs internally — cheap because product
    rows are cached in the response_cache).
    """
    out: dict[int, dict[str, Any]] = {}
    for pid in product_ids:
        try:
            resp = await client.get(
                "PRODUCTOS", record_id=pid,
                fields=["ID", "CODIGO", "NAME", "INV_FAMI", "INV_SUBFAMI"],
                use_cache=True,
            )
            if resp.rows:
                out[pid] = resp.rows[0]
        except VelneoError:
            continue
    return out


async def _resolve_facturas(
    client: VelneoClient, invoice_ids: set[int],
) -> dict[int, dict[str, Any]]:
    """For each VENT_FACT_VENT.ID, return the header fields we need on
    the report (ESTABLECIMIENTO, PUNTOEMISION, SECUENCIA, FECHA,
    RAZONSOCIALCOMPRADOR, SRI_IDENTIFICACION).
    """
    out: dict[int, dict[str, Any]] = {}
    for iid in invoice_ids:
        try:
            resp = await client.get(
                "VENT_FACT_VENT", record_id=iid,
                fields=["ID", "ESTABLECIMIENTO", "PUNTOEMISION", "SECUENCIA",
                        "FECHA", "RAZONSOCIALCOMPRADOR", "SRI_IDENTIFICACION"],
                use_cache=True,
            )
            if resp.rows:
                out[iid] = resp.rows[0]
        except VelneoError:
            continue
    return out


def _pivot(
    rows: list[dict[str, Any]],
    bodega_names: dict[int, str],
    familia_names: dict[int, str],
) -> tuple[list[str], list[str], dict[tuple[str, str], dict[str, float]]]:
    """Build the (fecha, familia) × bodega pivot table.

    Returns:
        bodegas — sorted unique bodega names actually used
        fechas — sorted unique date strings (YYYY-MM-DD)
        table — dict[(fecha, familia_name)][bodega_name] -> sum(PVP_LINEA)
    """
    table: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    bodegas: set[str] = set()
    fechas: set[str] = set()
    for r in rows:
        fecha = _short_date(r.get("fecha"))
        if not fecha:
            continue
        bod_id = r.get("inv_bodega")
        try:
            bod_id_i = int(bod_id) if bod_id else 0
        except (TypeError, ValueError):
            bod_id_i = 0
        bod_name = bodega_names.get(bod_id_i, f"BODEGA {bod_id_i}")
        fam_id = r.get("inv_fami")
        try:
            fam_id_i = int(fam_id) if fam_id else 0
        except (TypeError, ValueError):
            fam_id_i = 0
        fam_name = familia_names.get(fam_id_i, f"FAMILIA {fam_id_i}")
        try:
            pvp_linea = float(r.get("pvp_linea") or 0)
        except (TypeError, ValueError):
            pvp_linea = 0.0
        table[(fecha, fam_name)][bod_name] += pvp_linea
        bodegas.add(bod_name)
        fechas.add(fecha)
    return sorted(bodegas), sorted(fechas), table


def _write_informe(
    ws,
    bodegas: list[str],
    fechas: list[str],
    table: dict[tuple[str, str], dict[str, float]],
    company_name: str = "MEGA PRIMAVERA GALAPAGOS SA",
) -> None:
    """Write the INFORME sheet: Familia x Bodega pivot per Fecha."""
    bold = Font(bold=True)
    title_font = Font(bold=True, size=14)
    header_fill = PatternFill("solid", fgColor="D9E1F2")
    money_fmt = "#,##0.00"
    center = Alignment(horizontal="center")

    # Headers / title
    ws.cell(2, 2, company_name).font = title_font
    ws.cell(3, 2, "REPORTE DE VENTAS DIARIAS").font = title_font

    # Pivot header row
    HDR_ROW = 7
    ws.cell(HDR_ROW, 2, "Suma de PVP Linea").font = bold
    for j, bod in enumerate(bodegas, start=3):
        c = ws.cell(HDR_ROW, j, bod)
        c.font = bold; c.fill = header_fill; c.alignment = center
    total_col = 3 + len(bodegas)
    c = ws.cell(HDR_ROW, total_col, "Total general")
    c.font = bold; c.fill = header_fill; c.alignment = center

    # Body rows
    row = HDR_ROW + 1
    grand_total: dict[str, float] = defaultdict(float)
    grand_total_all = 0.0
    for fecha in fechas:
        # Date section header
        ws.cell(row, 2, _fmt_date_es(fecha)).font = bold
        row += 1
        # Familia rows for this fecha
        familias = sorted({fam for (f, fam) in table if f == fecha})
        date_total: dict[str, float] = defaultdict(float)
        date_total_all = 0.0
        for fam in familias:
            ws.cell(row, 2, fam)
            row_total = 0.0
            for j, bod in enumerate(bodegas, start=3):
                v = table[(fecha, fam)].get(bod, 0.0)
                if v:
                    c = ws.cell(row, j, v); c.number_format = money_fmt
                row_total += v
                date_total[bod] += v
                grand_total[bod] += v
            c = ws.cell(row, total_col, row_total); c.number_format = money_fmt; c.font = bold
            date_total_all += row_total
            row += 1
        # Date subtotal
        c = ws.cell(row, 2, f"Total {_fmt_date_es(fecha)}"); c.font = bold; c.fill = header_fill
        for j, bod in enumerate(bodegas, start=3):
            c = ws.cell(row, j, date_total[bod]); c.number_format = money_fmt; c.font = bold; c.fill = header_fill
        c = ws.cell(row, total_col, date_total_all); c.number_format = money_fmt; c.font = bold; c.fill = header_fill
        grand_total_all += date_total_all
        row += 1

    # Grand total row
    c = ws.cell(row, 2, "Total general"); c.font = bold; c.fill = header_fill
    for j, bod in enumerate(bodegas, start=3):
        c = ws.cell(row, j, grand_total[bod]); c.number_format = money_fmt; c.font = bold; c.fill = header_fill
    c = ws.cell(row, total_col, grand_total_all); c.number_format = money_fmt; c.font = bold; c.fill = header_fill

    # Column widths
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 28
    for j in range(3, total_col + 1):
        ws.column_dimensions[get_column_letter(j)].width = 22


def _write_detalle(
    ws,
    rows: list[dict[str, Any]],
    bodega_names: dict[int, str],
    familia_names: dict[int, str],
    subfamilia_names: dict[int, str],
    product_info: dict[int, dict[str, Any]],
    factura_info: dict[int, dict[str, Any]],
) -> None:
    """Write VENTAS_DETALLE with the same column layout the Velneo UI exports."""
    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="305496")
    header_font = Font(bold=True, color="FFFFFF")

    headers = [
        "CodBar", "Codigo", "Nombre", "Empaque", "Factor", "Cantidad",
        "Precio Bruto Empaque", "Dscto Empaque", "IVA Empaque", "PVP Linea",
        "Costo Empaque", "Precio Neto Empaque", "Utilidad",
        "Costo Linea", "Precio Neto Linea", "IVA Linea",
        "Bodega", "Familia Principal", "SubFamilia",
        "Id Venta", "Establecimiento", "Pto Emision", "Secuencia",
        "Fecha", "Cliente", "MES", "DIA", "AÑO", "Columna1",
    ]
    for j, h in enumerate(headers, start=1):
        c = ws.cell(1, j, h); c.font = header_font; c.fill = header_fill

    money_fmt = "#,##0.00"
    money_cols = {7, 8, 9, 10, 11, 12, 14, 15, 16}  # 1-based

    row_i = 2
    for r in rows:
        pid_raw = r.get("productos")
        try:
            pid = int(pid_raw) if pid_raw else 0
        except (TypeError, ValueError):
            pid = 0
        prod = product_info.get(pid) or {}

        bod_raw = r.get("inv_bodega")
        try:
            bod_id = int(bod_raw) if bod_raw else 0
        except (TypeError, ValueError):
            bod_id = 0

        fam_raw = r.get("inv_fami") or prod.get("INV_FAMI")
        try:
            fam_id = int(fam_raw) if fam_raw else 0
        except (TypeError, ValueError):
            fam_id = 0

        sub_raw = prod.get("INV_SUBFAMI")
        try:
            sub_id = int(sub_raw) if sub_raw else 0
        except (TypeError, ValueError):
            sub_id = 0

        inv_raw = r.get("vent_fact_vent")
        try:
            inv_id = int(inv_raw) if inv_raw else 0
        except (TypeError, ValueError):
            inv_id = 0
        fact = factura_info.get(inv_id) or {}

        fecha_str = _short_date(r.get("fecha") or fact.get("FECHA"))
        yyyy, mm, dd = ("", "", "")
        if len(fecha_str) == 10:
            yyyy, mm, dd = fecha_str[0:4], fecha_str[5:7], fecha_str[8:10]

        familia_name = familia_names.get(fam_id, "")
        values = [
            r.get("cod_bar") or "",
            prod.get("CODIGO") or "",
            r.get("nombre") or prod.get("NAME") or "",
            r.get("abrev") or "",
            r.get("factor"),
            r.get("can"),
            r.get("precio_bruto_empaque"),
            r.get("porcentaje_dscto_vta"),
            r.get("iva_empaque"),
            r.get("pvp_linea"),
            r.get("costo_empaque"),
            r.get("precio_neto_empaque"),
            r.get("porcentaje_utilidad2"),
            r.get("costo_linea"),
            r.get("precio_neto_linea"),
            r.get("iva_linea"),
            bodega_names.get(bod_id, ""),
            familia_name,
            subfamilia_names.get(sub_id, ""),
            inv_id or "",
            fact.get("ESTABLECIMIENTO") or r.get("emp") or "",
            fact.get("PUNTOEMISION") or "",
            fact.get("SECUENCIA") or "",
            _fmt_date_es(fecha_str),
            fact.get("RAZONSOCIALCOMPRADOR") or "",
            int(mm) if mm else "",
            int(dd) if dd else "",
            int(yyyy) if yyyy else "",
            familia_name,
        ]
        for j, v in enumerate(values, start=1):
            c = ws.cell(row_i, j, v)
            if j in money_cols:
                c.number_format = money_fmt
        row_i += 1

    # Auto-ish widths
    widths = [16, 12, 34, 8, 8, 10,
              12, 10, 10, 12,
              12, 12, 10,
              12, 14, 10,
              22, 22, 22,
              10, 10, 10, 12,
              12, 28, 6, 6, 6, 16]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w


async def generate(
    client: VelneoClient,
    *,
    date_from: str,
    date_to: str,
    sucursal: str | None = None,
    max_rows: int = 5000,
) -> dict[str, Any]:
    """Pull data + build XLSX. Returns ``{xlsx_bytes, total_lines, totals}``.

    ``date_from``/``date_to`` are ISO YYYY-MM-DD (inclusive). Sucursal
    defaults to the tenant's cfg.extra.velneo_sucursal.
    """
    from mcp_theos.tools.admin_ops import _tenant_sucursal

    # 1. Pull lines via the proceso.
    suc = sucursal or _tenant_sucursal(client)
    proc_params = {
        "SUCURSAL": suc,
        "FCH_FACT": "1",
        "FCH_DES": date_from,
        "FCH_HST": date_to,
        "OFF": "0",
    }
    body = await call_proceso_or_message(
        client, "VENT_FACT_MOV_BUSQ_3P",
        params=proc_params,
        row_keys=("inv_movimientos",),
    )
    if not body.get("ok"):
        return {
            "success": False,
            "error": body.get("message") or body.get("transport_error")
                     or "proceso failed",
            "error_code": "proceso_denied" if body.get("permission_denied") else "transport",
        }
    rows_full = body.get("rows") or []
    total_count = body.get("total_count") or len(rows_full)
    truncated = total_count > max_rows
    # Trim each row to the keys we actually use (saves memory + the
    # underlying response_cache hash).
    rows: list[dict[str, Any]] = []
    for r in rows_full[:max_rows]:
        kept = {k: v for k, v in r.items() if k in _KEEP_KEYS}
        rows.append(kept)

    if not rows:
        return {
            "success": True, "total_lines": 0, "truncated": False,
            "xlsx_bytes": None,
            "message": (
                f"No hay ventas en el rango {date_from} a {date_to}."
            ),
        }

    # 2. Resolve lookups (cached per tenant 30s in response_cache).
    bodega_names = await _resolve_lookup(client, "INV_BODEGA", "NAME")
    familia_names = await _resolve_lookup(client, "INV_FAMI", "NAME")
    # SUBFAMI lookup — fallback to empty if endpoint not exposed.
    try:
        subfamilia_names = await _resolve_lookup(client, "INV_SUBFAMI", "NAME")
    except Exception:
        subfamilia_names = {}

    product_ids: set[int] = set()
    invoice_ids: set[int] = set()
    for r in rows:
        try:
            pid = int(r.get("productos") or 0)
            if pid: product_ids.add(pid)
        except (TypeError, ValueError):
            pass
        try:
            iid = int(r.get("vent_fact_vent") or 0)
            if iid: invoice_ids.add(iid)
        except (TypeError, ValueError):
            pass
    product_info = await _resolve_products(client, product_ids)
    factura_info = await _resolve_facturas(client, invoice_ids)

    # 3. Build XLSX.
    wb = Workbook()
    wb.remove(wb.active)

    informe_ws = wb.create_sheet("INFORME")
    bodegas, fechas, table = _pivot(rows, bodega_names, familia_names)
    company_name = getattr(client.cfg, "commercial_name", None) or "MEPRIGA"
    _write_informe(informe_ws, bodegas, fechas, table, company_name=company_name)

    detalle_ws = wb.create_sheet("VENTAS_DETALLE")
    _write_detalle(detalle_ws, rows, bodega_names, familia_names,
                   subfamilia_names, product_info, factura_info)

    # 4. Aggregate totals for the response payload.
    grand_total_pvp = sum(
        v for fb in table.values() for v in fb.values()
    )
    grand_total_neto = sum(
        float(r.get("precio_neto_linea") or 0) for r in rows
    )

    # 5. Serialize.
    buf = io.BytesIO()
    wb.save(buf)
    return {
        "success": True,
        "total_lines": len(rows),
        "total_lines_in_range": total_count,
        "truncated": truncated,
        "xlsx_bytes": buf.getvalue(),
        "totals": {
            "pvp_linea": round(grand_total_pvp, 2),
            "precio_neto_linea": round(grand_total_neto, 2),
            "date_from": date_from,
            "date_to": date_to,
            "fechas_distintas": len(fechas),
            "bodegas": bodegas,
        },
    }
