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
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from mcp_theos.velneo_http import VelneoClient, VelneoError, call_proceso_or_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mepriga corporate palette — extracted from the operator-approved
# "informe mejorado" template (see commit message). Keep names symbolic so
# any future tweak (logo refresh, brand reboot) touches one constant only.
# ---------------------------------------------------------------------------

PALETTE_PRIMARY = "1F4E78"        # MEPRIGA banner / grand-total row
PALETTE_SECONDARY = "2E75B6"      # KPI header for ALMACÉN 01 / column heads
PALETTE_GREEN = "70AD47"          # KPI header for BODEGA 15 (visual contrast)
PALETTE_TABLE_HEADER = "305496"   # VENTAS_DETALLE / pivot table headers
PALETTE_ZEBRA = "F8F9FB"          # alternating row fill
PALETTE_TOTAL = "F2F2F2"          # right-most TOTAL column / subtotal rows
PALETTE_SUBTLE = "595959"         # secondary text (dates, captions)
PALETTE_WHITE = "FFFFFF"

# Bodega code (3-digit suffix of name when present) → KPI fill color.
# Anything not in the map falls back to PALETTE_SECONDARY.
BODEGA_FILL_MAP = {
    "BODEGA 15": PALETTE_GREEN,
    "ALMACEN 01": PALETTE_SECONDARY,
}


def _bodega_color(name: str) -> str:
    """Best-match color for a bodega name (case-insensitive prefix)."""
    if not name:
        return PALETTE_SECONDARY
    n = name.upper()
    for prefix, color in BODEGA_FILL_MAP.items():
        if n.startswith(prefix):
            return color
    return PALETTE_SECONDARY


MONEY_FORMAT = '"$"#,##0.00'


# Fields we keep from the 130-key proceso row. call_proceso_or_message
# upper-cases all keys (matches the convention used by table list
# responses) so we filter on UPPERCASE here.
_KEEP_KEYS = frozenset({
    "ID", "VENT_FACT_VENT",
    "PRODUCTOS", "INV_PRESENT_PRODUCTO",
    "NOMBRE", "ABREV", "FACTOR", "CAN",
    "FECHA",
    "INV_BODEGA",
    "INV_FAMI",
    "COD_BAR",
    "PRECIO_BRUTO_EMPAQUE", "IVA_EMPAQUE", "COSTO_EMPAQUE",
    "PRECIO_NETO_EMPAQUE",
    "PORCENTAJE_DSCTO_VTA", "PORCENTAJE_UTILIDAD2",
    "PVP", "PVP_LINEA",
    "COSTO_LINEA", "PRECIO_NETO_LINEA", "IVA_LINEA",
    "DCTO_VTAS_LINEA",
    "EMP", "SUC",
    "CLT_ENT",
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
        try:
            rid_i = int(rid)
        except (TypeError, ValueError):
            # Some catalog tables have alphanumeric IDs (e.g. INV_BODEGA
            # may carry codes like "00000000U"). Skip — the row data
            # already carries the bodega/familia NAME inline elsewhere
            # if it matters.
            continue
        out[rid_i] = str(r.get(name_field) or "").strip()
    return out


import re as _re

_FAMILY_PREFIX_RE = _re.compile(r"^\s*\d+(?:\.\d+)*\s+")


def _clean_family_name(name: str) -> str:
    """Strip the leading "N " or "N.N " classification prefix.

    Mepriga's INV_FAMI names are like "1 VIVERES" (parent) and
    "1.3 ABARROTES" (child). The manual report shows just "VIVERES" /
    "ABARROTES". We strip the leading digits + dots + spaces here.
    """
    return _FAMILY_PREFIX_RE.sub("", str(name or "").strip())


async def _resolve_family_hierarchy(
    client: VelneoClient, pagesize: int = 500,
) -> tuple[dict[int, str], dict[int, str]]:
    """Pull INV_FAMI and walk the id_padre chain.

    Returns two dicts:

    * ``leaf_to_parent_name`` — ``{leaf_inv_fami_id: parent_name}`` —
      what the line's ``INV_FAMI`` field resolves to as the "Familia
      Principal" column of the manual report.
    * ``id_to_self_name`` — ``{inv_fami_id: own_name}`` — used for the
      SubFamilia column of VENTAS_DETALLE.

    Names are cleaned (prefix "1 ", "1.3 " etc. stripped).
    """
    try:
        resp = await client.get(
            "INV_FAMI", params={"pagesize": pagesize},
            fields=["ID", "NAME", "ID_PADRE"],
            use_cache=True,
        )
    except VelneoError as exc:
        logger.warning("INV_FAMI hierarchy failed: %s", exc)
        return {}, {}

    raw: dict[int, dict[str, Any]] = {}
    for r in resp.rows:
        rid = r.get("ID")
        if rid is None:
            continue
        try:
            rid_i = int(rid)
        except (TypeError, ValueError):
            continue  # alphanum sentinel rows
        pid_raw = r.get("ID_PADRE")
        try:
            pid_i = int(pid_raw) if pid_raw not in (None, "", "0") else 0
        except (TypeError, ValueError):
            pid_i = 0
        raw[rid_i] = {"name": str(r.get("NAME") or "").strip(),
                      "parent": pid_i}

    # Walk parent chain to find the TOP of each leaf (id == id_padre
    # OR id_padre==0 marks the root). Cap at 5 hops to prevent loops.
    id_to_self_name: dict[int, str] = {
        i: _clean_family_name(v["name"]) for i, v in raw.items()
    }
    leaf_to_parent_name: dict[int, str] = {}
    for leaf_id, info in raw.items():
        cur = leaf_id
        for _ in range(5):
            parent = raw.get(cur, {}).get("parent", 0)
            if parent == 0 or parent == cur or parent not in raw:
                break
            cur = parent
        leaf_to_parent_name[leaf_id] = _clean_family_name(raw[cur]["name"])
    return leaf_to_parent_name, id_to_self_name


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
        fecha = _short_date(r.get("FECHA"))
        if not fecha:
            continue
        bod_id = r.get("INV_BODEGA")
        try:
            bod_id_i = int(bod_id) if bod_id else 0
        except (TypeError, ValueError):
            bod_id_i = 0
        bod_name = bodega_names.get(bod_id_i, f"BODEGA {bod_id_i}")
        fam_id = r.get("INV_FAMI")
        try:
            fam_id_i = int(fam_id) if fam_id else 0
        except (TypeError, ValueError):
            fam_id_i = 0
        fam_name = familia_names.get(fam_id_i, f"FAMILIA {fam_id_i}")
        try:
            pvp_linea = float(r.get("PVP_LINEA") or 0)
        except (TypeError, ValueError):
            pvp_linea = 0.0
        table[(fecha, fam_name)][bod_name] += pvp_linea
        bodegas.add(bod_name)
        fechas.add(fecha)
    return sorted(bodegas), sorted(fechas), table


def _aggregate_for_charts(
    bodegas: list[str],
    table: dict[tuple[str, str], dict[str, float]],
) -> tuple[list[str], dict[str, dict[str, float]], dict[str, float]]:
    """Collapse the per-date pivot into the totals each chart needs.

    The 4 charts that mirror the operator's "informe mejorado" template
    never break down by fecha — they show one number per familia (or per
    bodega) for the whole reporting window. So we sum across all dates
    here and return:

      familias — list of family names, sorted alphabetically (matches
                 the visual order in the reference XLSX: BAZAR,
                 PAPELERIA, PRODUCTOS DE HOGAR, VESTIMENTA, VIVERES)
      fam_x_bod — {familia: {bodega: total_pvp}}
      bod_totals — {bodega: total_pvp_across_familias}
    """
    fam_x_bod: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    bod_totals: dict[str, float] = defaultdict(float)
    familias_set: set[str] = set()
    for (_fecha, fam), bod_vals in table.items():
        familias_set.add(fam)
        for bod, v in bod_vals.items():
            fam_x_bod[fam][bod] += v
            bod_totals[bod] += v
    return sorted(familias_set), dict(fam_x_bod), dict(bod_totals)


def _write_charts_data(
    ws,
    bodegas: list[str],
    familias: list[str],
    fam_x_bod: dict[str, dict[str, float]],
    bod_totals: dict[str, float],
) -> None:
    """Populate the hidden ``_datos_graficos`` sheet — three side-by-side
    blocks that the 4 charts reference. Mirrors the layout in the
    reference XLSX (analysed empirically).

      Block 1 (cols A..B+n_bodegas) — Familia × Bodega matrix.
      Block 2 (cols E..F)           — Familia × Total.
      Block 3 (cols H..I)           — Bodega × Venta.
    """
    # Block 1: Familia × Bodega
    ws.cell(1, 1, "Familia")
    for j, bod in enumerate(bodegas, start=2):
        ws.cell(1, j, bod)
    for i, fam in enumerate(familias, start=2):
        ws.cell(i, 1, fam)
        for j, bod in enumerate(bodegas, start=2):
            ws.cell(i, j, fam_x_bod.get(fam, {}).get(bod, 0))

    # Block 2: Familia × Total (cols E:F)
    ws.cell(1, 5, "Familia")
    ws.cell(1, 6, "Total")
    for i, fam in enumerate(familias, start=2):
        ws.cell(i, 5, fam)
        ws.cell(i, 6, sum(fam_x_bod.get(fam, {}).get(b, 0) for b in bodegas))

    # Block 3: Bodega × Venta (cols H:I)
    ws.cell(1, 8, "Bodega")
    ws.cell(1, 9, "Venta")
    for i, bod in enumerate(bodegas, start=2):
        ws.cell(i, 8, bod)
        ws.cell(i, 9, bod_totals.get(bod, 0))

    ws.sheet_state = "hidden"


def _add_dashboard_charts(
    ws_informe,
    ws_data,
    bodegas: list[str],
    familias: list[str],
    anchor_row: int,
) -> None:
    """Place 4 charts on the INFORME sheet, anchored below the table.

    Two rows × two columns layout:
      [bar horizontal: Familia × Bodega]  [donut: Participación por Familia]
      [bar vertical: Total por Bodega]    [donut: Participación por Bodega]

    All series read from ``ws_data`` (the hidden _datos_graficos sheet).
    """
    from openpyxl.chart import BarChart, DoughnutChart, Reference
    from openpyxl.chart.label import DataLabelList
    from openpyxl.chart.marker import DataPoint
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.colors import ColorChoice as ColorChoiceClass
    from openpyxl.drawing.fill import ColorChoice
    from openpyxl.drawing.line import LineProperties

    n_fam = len(familias)
    n_bod = len(bodegas)
    if n_fam == 0 or n_bod == 0:
        return

    def _solid_no_line(hex_color: str) -> GraphicalProperties:
        gp = GraphicalProperties(solidFill=hex_color)
        gp.line = LineProperties(noFill=True)
        return gp

    # Map first two bodegas to brand colors. Anything beyond falls back
    # to the secondary blue so reports with extra bodegas still render.
    bodega_chart_colors = [PALETTE_SECONDARY, PALETTE_GREEN] + [PALETTE_SECONDARY] * max(0, n_bod - 2)

    # ------------------------------------------------------------------
    # Helper: build a DataLabelList that ONLY shows the field we want.
    # Without explicit False on the others Excel falls back to defaults
    # (which is "show series name + category name + value" for donuts
    # and bars). That's the source of the verbose labels we want gone.
    # ------------------------------------------------------------------
    def _quiet_labels(show_percent=False, show_val=False) -> DataLabelList:
        return DataLabelList(
            showPercent=show_percent,
            showVal=show_val,
            showCatName=False,
            showSerName=False,
            showLegendKey=False,
            showBubbleSize=False,
        )

    # Each chart roughly portrait-shaped (matches the reference layout):
    # 11 cm wide × 9 cm tall — about 2 chart columns fit horizontally in
    # a standard widescreen viewer.
    CHART_W_CM = 11.0
    CHART_H_CM = 9.0

    # ------------------------------------------------------------------
    # Chart 0 — BarChart horizontal: Ventas por Familia y Bodega
    # ------------------------------------------------------------------
    c0 = BarChart()
    c0.type = "bar"
    c0.grouping = "clustered"
    c0.style = 2
    c0.title = "Ventas por Familia y Bodega"
    c0.gapWidth = 182
    c0.varyColors = False
    c0.width = CHART_W_CM
    c0.height = CHART_H_CM
    data_ref = Reference(ws_data, min_col=2, max_col=1 + n_bod, min_row=1, max_row=1 + n_fam)
    cats_ref = Reference(ws_data, min_col=1, min_row=2, max_row=1 + n_fam)
    c0.add_data(data_ref, titles_from_data=True)
    c0.set_categories(cats_ref)
    for i, color in enumerate(bodega_chart_colors[:n_bod]):
        c0.series[i].graphicalProperties = _solid_no_line(color)
    c0.legend.position = "b"
    # No data labels on the horizontal bars (matches reference template).

    # ------------------------------------------------------------------
    # Chart 1 — Doughnut: Participación por Familia
    # ------------------------------------------------------------------
    c1 = DoughnutChart()
    c1.holeSize = 75
    c1.firstSliceAng = 0
    c1.title = "Participación por Familia"
    c1.width = CHART_W_CM
    c1.height = CHART_H_CM
    fam_data = Reference(ws_data, min_col=6, min_row=1, max_row=1 + n_fam)
    fam_cats = Reference(ws_data, min_col=5, min_row=2, max_row=1 + n_fam)
    c1.add_data(fam_data, titles_from_data=True)
    c1.set_categories(fam_cats)
    c1.dataLabels = _quiet_labels(show_percent=True)
    # Stable color rotation per family — uses the corporate palette so
    # the donut matches the rest of the dashboard.
    fam_palette = [
        PALETTE_SECONDARY, "C0504D", PALETTE_GREEN, "8064A2", "4BACC6",
        "F79646", "1F4E78", "70AD47",
    ]
    for k in range(n_fam):
        dp = DataPoint(idx=k)
        dp.graphicalProperties = _solid_no_line(fam_palette[k % len(fam_palette)])
        c1.series[0].data_points.append(dp)
    c1.legend.position = "r"

    # ------------------------------------------------------------------
    # Chart 2 — BarChart vertical: Total de Venta por Bodega
    # ------------------------------------------------------------------
    c2 = BarChart()
    c2.type = "col"
    c2.grouping = "clustered"
    c2.style = 2
    c2.title = "Total de Venta por Bodega"
    c2.gapWidth = 219
    c2.overlap = -27
    c2.varyColors = False
    c2.width = CHART_W_CM
    c2.height = CHART_H_CM
    bod_data = Reference(ws_data, min_col=9, min_row=1, max_row=1 + n_bod)
    bod_cats = Reference(ws_data, min_col=8, min_row=2, max_row=1 + n_bod)
    c2.add_data(bod_data, titles_from_data=True)
    c2.set_categories(bod_cats)
    c2.series[0].dLbls = _quiet_labels(show_val=True)
    c2.series[0].graphicalProperties = _solid_no_line(PALETTE_SECONDARY)
    c2.legend = None

    # ------------------------------------------------------------------
    # Chart 3 — Doughnut: Participación por Bodega
    # ------------------------------------------------------------------
    c3 = DoughnutChart()
    c3.holeSize = 75
    c3.firstSliceAng = 0
    c3.title = "Participación por Bodega"
    c3.width = CHART_W_CM
    c3.height = CHART_H_CM
    c3.add_data(bod_data, titles_from_data=True)
    c3.set_categories(bod_cats)
    c3.dataLabels = _quiet_labels(show_percent=True)
    for k in range(n_bod):
        dp = DataPoint(idx=k)
        dp.graphicalProperties = _solid_no_line(bodega_chart_colors[k % len(bodega_chart_colors)])
        c3.series[0].data_points.append(dp)
    c3.legend.position = "b"

    # ------------------------------------------------------------------
    # Anchor the four charts in a 2×2 grid below the main table.
    # Width / height in cm above takes precedence over anchor cell span —
    # the anchor is just the top-left corner. Spacing rows of 22 lines
    # apart leaves enough room for the 9 cm tall charts.
    # ------------------------------------------------------------------
    # Layout grid:
    #   row base_row:   [chart0 at B]    [chart1 at E]
    #   row base_row+22:[chart2 at B]    [chart3 at E]
    # Col E sits past the family columns (B+C=44 chars) which roughly
    # matches the 11 cm chart width.
    base_row = anchor_row + 2
    right_col_letter = get_column_letter(5)  # E
    anchors = [
        (f"B{base_row}",                            c0),
        (f"{right_col_letter}{base_row}",           c1),
        (f"B{base_row + 22}",                       c2),
        (f"{right_col_letter}{base_row + 22}",      c3),
    ]
    for cell_ref, chart in anchors:
        ws_informe.add_chart(chart, cell_ref)


def _write_informe(
    ws,
    bodegas: list[str],
    fechas: list[str],
    table: dict[tuple[str, str], dict[str, float]],
    company_name: str = "MEGA PRIMAVERA GALAPAGOS SA",
) -> int:
    """Write the INFORME dashboard sheet using the corporate palette.

    Layout (top-down):
      Row 1     blank top margin
      Row 2-3   MEPRIGA banner (primary blue, white bold, 22pt)
                + subtitle "Reporte de Ventas Diarias" (secondary blue 12pt)
      Row 4     Período (gray subtle)
      Row 6-9   KPIs por bodega (cards horizontales)
      Row 11+   Tabla Familia x Bodega con totales por día y total general
    """
    fmt = MONEY_FORMAT
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center", indent=1)
    right = Alignment(horizontal="right", vertical="center", indent=1)

    primary_fill = PatternFill("solid", fgColor=PALETTE_PRIMARY)
    secondary_fill = PatternFill("solid", fgColor=PALETTE_SECONDARY)
    table_hdr_fill = PatternFill("solid", fgColor=PALETTE_PRIMARY)
    zebra_fill = PatternFill("solid", fgColor=PALETTE_ZEBRA)
    total_col_fill = PatternFill("solid", fgColor=PALETTE_TOTAL)
    subtotal_fill = PatternFill("solid", fgColor=PALETTE_SECONDARY)
    grand_fill = PatternFill("solid", fgColor=PALETTE_PRIMARY)

    title_font = Font(bold=True, size=22, color=PALETTE_WHITE, name="Calibri")
    subtitle_font = Font(bold=True, size=12, color=PALETTE_WHITE, name="Calibri")
    caption_font = Font(size=11, color=PALETTE_SUBTLE, name="Calibri", italic=True)
    kpi_label_font = Font(bold=True, size=11, color=PALETTE_WHITE, name="Calibri")
    kpi_value_font = Font(bold=True, size=20, color=PALETTE_PRIMARY, name="Calibri")
    kpi_value_font_total = Font(bold=True, size=20, color=PALETTE_WHITE, name="Calibri")
    section_font = Font(bold=True, size=13, color=PALETTE_PRIMARY, name="Calibri")
    hdr_font_white = Font(bold=True, size=11, color=PALETTE_WHITE, name="Calibri")
    body_font = Font(size=11, color="000000", name="Calibri")
    date_marker_font = Font(bold=True, size=11, color=PALETTE_SECONDARY, name="Calibri")
    subtotal_font = Font(bold=True, size=11, color=PALETTE_WHITE, name="Calibri")
    grand_font = Font(bold=True, size=12, color=PALETTE_WHITE, name="Calibri")

    thin = Side(border_style="thin", color="D0D7DE")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    n_bodegas = len(bodegas)

    # ------------------------------------------------------------------
    # Layout columns — matches the operator's reference template.
    # ------------------------------------------------------------------
    #
    #   col A    -> 2-char margin
    #   col B    -> Familia Principal label + family names (single col)
    #   col C    -> visual extension of the family column (gap in the
    #               body; coloured band in the header row so the table
    #               header looks continuous)
    #   col D..E -> ALMACEN, BODEGA (1 col each)
    #   col F    -> TOTAL (1 col)
    #
    # For n_bodegas=2 (Mepriga's case): 5 visible cols (B..F).
    #
    # FAMILY_LABEL_COL = 2 (B)  — family text
    # FAMILY_EXTRA_COL = 3 (C)  — header band extension; gap in body
    # value_start_col  = 4 (D)  — first bodega
    # total_col        = D + n_bodegas
    FAMILY_LABEL_COL = 2
    FAMILY_EXTRA_COL = 3
    value_start_col = 4
    total_col = value_start_col + n_bodegas

    # ------------------------------------------------------------------
    # 1. Banner row 2 (full width — merge B:total_col)
    # ------------------------------------------------------------------
    ws.merge_cells(start_row=2, start_column=2, end_row=2, end_column=total_col)
    banner = ws.cell(2, 2, f"MEPRIGA — {company_name}")
    banner.font = title_font
    banner.fill = primary_fill
    banner.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 38

    ws.merge_cells(start_row=3, start_column=2, end_row=3, end_column=total_col)
    sub = ws.cell(3, 2, "Reporte de Ventas Diarias")
    sub.font = subtitle_font
    sub.fill = secondary_fill
    sub.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 24

    if fechas:
        if len(fechas) == 1:
            periodo = f"Período: {_fmt_date_es(fechas[0])}"
        else:
            periodo = f"Período: {_fmt_date_es(fechas[0])} a {_fmt_date_es(fechas[-1])}"
    else:
        periodo = "Período: —"
    ws.merge_cells(start_row=4, start_column=2, end_row=4, end_column=total_col)
    cap = ws.cell(4, 2, periodo)
    cap.font = caption_font
    cap.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[4].height = 18

    # ------------------------------------------------------------------
    # 2. KPI cards row 6 (one per bodega + total general)
    # ------------------------------------------------------------------
    KPI_ROW_LABEL = 6
    KPI_ROW_VALUE = 7
    ws.row_dimensions[KPI_ROW_LABEL].height = 22
    ws.row_dimensions[KPI_ROW_VALUE].height = 32

    # Compute per-bodega and grand totals up front (needed by both KPIs
    # and the table grand-total row later).
    per_bodega_total: dict[str, float] = defaultdict(float)
    grand_total_all = 0.0
    for (fecha, fam), bodega_vals in table.items():
        for bod, v in bodega_vals.items():
            per_bodega_total[bod] += v
            grand_total_all += v

    # Layout: KPI cards mirror the operator's reference. Each bodega
    # card spans 2 cols (label + value text fits comfortably). TOTAL
    # GENERAL takes 1 col — intentionally narrower so it aligns with
    # the TOTAL column of the table below. For n_bodegas=2 that's:
    #   ALMACEN -> cols 2..3 (B..C)
    #   BODEGA  -> cols 4..5 (D..E)
    #   TOTAL   -> col 6     (F)
    # which exactly matches the FAMILY_LABEL + FAMILY_EXTRA + bodegas +
    # TOTAL layout of the table.
    cards = [(bod, per_bodega_total.get(bod, 0.0), _bodega_color(bod)) for bod in bodegas]
    cards.append(("TOTAL GENERAL", grand_total_all, PALETTE_PRIMARY))

    spans = [2] * n_bodegas + [1]

    col = 2
    for (label, value, color), span in zip(cards, spans):
        end_col = col + span - 1
        # Label band
        ws.merge_cells(start_row=KPI_ROW_LABEL, start_column=col,
                       end_row=KPI_ROW_LABEL, end_column=end_col)
        lcell = ws.cell(KPI_ROW_LABEL, col, label)
        lcell.font = kpi_label_font
        lcell.fill = PatternFill("solid", fgColor=color)
        lcell.alignment = center
        # Value band
        ws.merge_cells(start_row=KPI_ROW_VALUE, start_column=col,
                       end_row=KPI_ROW_VALUE, end_column=end_col)
        vcell = ws.cell(KPI_ROW_VALUE, col, value)
        vcell.font = (kpi_value_font_total if label == "TOTAL GENERAL"
                      else Font(bold=True, size=20, color=color, name="Calibri"))
        if label == "TOTAL GENERAL":
            vcell.fill = PatternFill("solid", fgColor=color)
        vcell.alignment = Alignment(horizontal="center", vertical="center")
        vcell.number_format = fmt
        vcell.border = box
        col = end_col + 1

    # ------------------------------------------------------------------
    # 3. Pivot table — Familia × Bodega with date subtotals
    # ------------------------------------------------------------------
    SECTION_ROW = 10
    ws.merge_cells(start_row=SECTION_ROW, start_column=2,
                   end_row=SECTION_ROW, end_column=total_col)
    sec = ws.cell(SECTION_ROW, 2, "Ventas por Familia y Bodega")
    sec.font = section_font
    sec.alignment = left
    ws.row_dimensions[SECTION_ROW].height = 22

    HDR_ROW = SECTION_ROW + 1
    ws.row_dimensions[HDR_ROW].height = 22
    # "Familia Principal" header in col B only (matches reference).
    # Col C is part of the header band but empty — fills with the same
    # azul oscuro so the band looks continuous across the table.
    h = ws.cell(HDR_ROW, FAMILY_LABEL_COL, "Familia Principal")
    h.font = hdr_font_white; h.fill = table_hdr_fill; h.alignment = left
    h.border = box
    ws.cell(HDR_ROW, FAMILY_EXTRA_COL).fill = table_hdr_fill
    for j, bod in enumerate(bodegas, start=value_start_col):
        c = ws.cell(HDR_ROW, j, bod)
        c.font = hdr_font_white; c.fill = table_hdr_fill; c.alignment = center
        c.border = box
    ct = ws.cell(HDR_ROW, total_col, "TOTAL")
    ct.font = hdr_font_white; ct.fill = table_hdr_fill; ct.alignment = center
    ct.border = box

    row = HDR_ROW + 1
    grand_by_bod: dict[str, float] = defaultdict(float)
    grand_total_recompute = 0.0
    use_date_grouping = len(fechas) > 1

    for fecha in fechas:
        if use_date_grouping:
            # Date marker row
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=total_col)
            dm = ws.cell(row, 2, _fmt_date_es(fecha))
            dm.font = date_marker_font
            dm.alignment = left
            ws.row_dimensions[row].height = 20
            row += 1

        familias = sorted({fam for (f, fam) in table if f == fecha})
        date_by_bod: dict[str, float] = defaultdict(float)
        date_total_all = 0.0

        for idx, fam in enumerate(familias):
            zebra = (idx % 2 == 1)
            # Familia name in col B only. Col C is empty (acts as visual
            # gap before the value columns). Zebra fill covers B+C so the
            # banding spans the full family-column area.
            fc = ws.cell(row, FAMILY_LABEL_COL, fam)
            fc.font = body_font
            fc.alignment = left
            if zebra:
                ws.cell(row, FAMILY_LABEL_COL).fill = zebra_fill
                ws.cell(row, FAMILY_EXTRA_COL).fill = zebra_fill
            row_total = 0.0
            for j, bod in enumerate(bodegas, start=value_start_col):
                v = table[(fecha, fam)].get(bod, 0.0)
                c = ws.cell(row, j, v if v else None)
                c.number_format = fmt
                c.alignment = right
                if zebra:
                    c.fill = zebra_fill
                row_total += v
                date_by_bod[bod] += v
                grand_by_bod[bod] += v
            tc = ws.cell(row, total_col, row_total)
            tc.number_format = fmt
            tc.alignment = right
            tc.font = Font(bold=True, size=11, color=PALETTE_PRIMARY, name="Calibri")
            tc.fill = total_col_fill
            date_total_all += row_total
            grand_total_recompute += row_total
            row += 1

        if use_date_grouping:
            # Date subtotal row — label merged B-C (consistent with the
            # grand total row below), values in D..F.
            ws.row_dimensions[row].height = 22
            ws.merge_cells(start_row=row, start_column=FAMILY_LABEL_COL,
                           end_row=row, end_column=FAMILY_EXTRA_COL)
            stc = ws.cell(row, FAMILY_LABEL_COL, f"Subtotal {_fmt_date_es(fecha)}")
            stc.font = subtotal_font; stc.fill = subtotal_fill; stc.alignment = left
            ws.cell(row, FAMILY_EXTRA_COL).fill = subtotal_fill
            for j, bod in enumerate(bodegas, start=value_start_col):
                c = ws.cell(row, j, date_by_bod[bod])
                c.number_format = fmt
                c.font = subtotal_font; c.fill = subtotal_fill; c.alignment = right
            c = ws.cell(row, total_col, date_total_all)
            c.number_format = fmt
            c.font = subtotal_font; c.fill = subtotal_fill; c.alignment = right
            row += 1

    # Grand total row — label merged B-C (matches the reference template
    # where "TOTAL GENERAL" sits under the family column band), values
    # in D..F line up with the body rows above.
    ws.row_dimensions[row].height = 26
    grand_label_align = Alignment(horizontal="left", vertical="center", indent=1)
    grand_value_align = Alignment(horizontal="right", vertical="center", indent=1)
    ws.merge_cells(start_row=row, start_column=FAMILY_LABEL_COL,
                   end_row=row, end_column=FAMILY_EXTRA_COL)
    gc = ws.cell(row, FAMILY_LABEL_COL, "TOTAL GENERAL")
    gc.font = grand_font; gc.fill = grand_fill; gc.alignment = grand_label_align
    ws.cell(row, FAMILY_EXTRA_COL).fill = grand_fill
    for j, bod in enumerate(bodegas, start=value_start_col):
        c = ws.cell(row, j, grand_by_bod[bod])
        c.number_format = fmt
        c.font = grand_font; c.fill = grand_fill; c.alignment = grand_value_align
    c = ws.cell(row, total_col, grand_total_recompute)
    c.number_format = fmt
    c.font = grand_font; c.fill = grand_fill; c.alignment = grand_value_align

    last_row = row

    # ------------------------------------------------------------------
    # 4. Column widths — sized so the KPI cards and the table both
    # line up cleanly:
    #   A   = 2  (margin)
    #   B   = 28 (family names + ALMACEN KPI label half)
    #   C   = 16 (extension of family column; visual gap in body)
    #   D-E = 22 (value columns + KPI label halves)
    #   F   = 22 (TOTAL value + single-col TOTAL GENERAL KPI)
    # With this, B+C (44) ≈ D+E (44), so the ALMACEN and BODEGA KPI
    # cards visually match in width, and the TOTAL card (col F) is the
    # narrower third card — matches the reference template exactly.
    # ------------------------------------------------------------------
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions[get_column_letter(FAMILY_LABEL_COL)].width = 28
    ws.column_dimensions[get_column_letter(FAMILY_EXTRA_COL)].width = 16
    for j in range(value_start_col, total_col + 1):
        ws.column_dimensions[get_column_letter(j)].width = 22
    ws.sheet_view.showGridLines = False

    return last_row


def _write_detalle(
    ws,
    rows: list[dict[str, Any]],
    bodega_names: dict[int, str],
    familia_names: dict[int, str],
    subfamilia_names: dict[int, str],
    product_info: dict[int, dict[str, Any]],
    factura_info: dict[int, dict[str, Any]],
) -> None:
    """Write VENTAS_DETALLE — 29-col raw line layout matching Velneo UI export.

    Visual polish on top of the raw export:
      * Header band in PALETTE_TABLE_HEADER (corporate blue), white bold.
      * ``freeze_panes='A2'`` so the header sticks while scrolling.
      * Money columns formatted as `"$"#,##0.00` instead of bare decimals.
      * Subtle zebra stripes (`PALETTE_ZEBRA`) every other row for readability
        on long sheets (~150k rows for a monthly report).
    """
    header_fill = PatternFill("solid", fgColor=PALETTE_TABLE_HEADER)
    header_font = Font(bold=True, color=PALETTE_WHITE, name="Calibri", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    zebra_fill = PatternFill("solid", fgColor=PALETTE_ZEBRA)

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
        c = ws.cell(1, j, h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = header_align
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "A2"

    money_fmt = MONEY_FORMAT
    money_cols = {7, 8, 9, 10, 11, 12, 14, 15, 16}  # 1-based

    row_i = 2
    for r in rows:
        pid_raw = r.get("PRODUCTOS")
        try:
            pid = int(pid_raw) if pid_raw else 0
        except (TypeError, ValueError):
            pid = 0
        prod = product_info.get(pid) or {}

        bod_raw = r.get("INV_BODEGA")
        try:
            bod_id = int(bod_raw) if bod_raw else 0
        except (TypeError, ValueError):
            bod_id = 0

        fam_raw = r.get("INV_FAMI") or prod.get("INV_FAMI")
        try:
            fam_id = int(fam_raw) if fam_raw else 0
        except (TypeError, ValueError):
            fam_id = 0

        sub_raw = prod.get("INV_SUBFAMI")
        try:
            sub_id = int(sub_raw) if sub_raw else 0
        except (TypeError, ValueError):
            sub_id = 0

        inv_raw = r.get("VENT_FACT_VENT")
        try:
            inv_id = int(inv_raw) if inv_raw else 0
        except (TypeError, ValueError):
            inv_id = 0
        fact = factura_info.get(inv_id) or {}

        fecha_str = _short_date(r.get("FECHA") or fact.get("FECHA"))
        yyyy, mm, dd = ("", "", "")
        if len(fecha_str) == 10:
            yyyy, mm, dd = fecha_str[0:4], fecha_str[5:7], fecha_str[8:10]

        familia_name = familia_names.get(fam_id, "")
        values = [
            r.get("COD_BAR") or "",
            prod.get("CODIGO") or "",
            r.get("NOMBRE") or prod.get("NAME") or "",
            r.get("ABREV") or "",
            r.get("FACTOR"),
            r.get("CAN"),
            r.get("PRECIO_BRUTO_EMPAQUE"),
            r.get("PORCENTAJE_DSCTO_VTA"),
            r.get("IVA_EMPAQUE"),
            r.get("PVP_LINEA"),
            r.get("COSTO_EMPAQUE"),
            r.get("PRECIO_NETO_EMPAQUE"),
            r.get("PORCENTAJE_UTILIDAD2"),
            r.get("COSTO_LINEA"),
            r.get("PRECIO_NETO_LINEA"),
            r.get("IVA_LINEA"),
            bodega_names.get(bod_id, ""),
            familia_name,
            subfamilia_names.get(sub_id, ""),
            inv_id or "",
            fact.get("ESTABLECIMIENTO") or r.get("EMP") or "",
            fact.get("PUNTOEMISION") or "",
            fact.get("SECUENCIA") or "",
            _fmt_date_es(fecha_str),
            fact.get("RAZONSOCIALCOMPRADOR") or "",
            int(mm) if mm else "",
            int(dd) if dd else "",
            int(yyyy) if yyyy else "",
            familia_name,
        ]
        # Zebra row index: row_i starts at 2 (data starts below header at 1).
        # Stripe odd offsets so the first data row stays white.
        is_zebra = ((row_i - 2) % 2 == 1)
        for j, v in enumerate(values, start=1):
            c = ws.cell(row_i, j, v)
            if j in money_cols:
                c.number_format = money_fmt
            if is_zebra:
                c.fill = zebra_fill
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
    resolve_products: bool = False,
    resolve_facturas: bool = False,
) -> dict[str, Any]:
    """Pull data + build XLSX. Returns ``{xlsx_bytes, total_lines, totals}``.

    ``date_from``/``date_to`` are ISO YYYY-MM-DD (inclusive). Sucursal
    defaults to the tenant's cfg.extra.velneo_sucursal.
    """
    from mcp_theos.tools.admin_ops import _tenant_sucursal
    from urllib.parse import quote
    from mcp_theos.velneo_http import _upper_keys

    # 1. Pull lines via the proceso — paginated in 500-row chunks to
    # keep the vServer happy. Velneo's REST honors page[number] /
    # page[size] on _process/<name> just like on table list endpoints
    # (verified empirically 2026-05-28).
    suc = sucursal or _tenant_sucursal(client)
    base_params = {
        "param[SUCURSAL]": suc,
        "param[FCH_FACT]": "1",
        "param[FCH_DES]": date_from,
        "param[FCH_HST]": date_to,
        "param[OFF]": "0",
    }
    PAGE_SIZE = 500
    rows: list[dict[str, Any]] = []
    total_count = 0
    page_num = 1
    while True:
        page_params = {
            **base_params,
            "page[size]": PAGE_SIZE,
            "page[number]": page_num,
        }
        try:
            resp = await client._client.get(  # noqa: SLF001
                f"_process/{quote('VENT_FACT_MOV_BUSQ_3P')}",
                params=page_params,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error_code": "transport",
                "error": f"{type(exc).__name__}: {exc}",
                "page_at_failure": page_num,
                "rows_collected": len(rows),
            }
        # First page tells us the universe.
        if total_count == 0:
            total_count = int(body.get("total_count") or 0)
        page_rows_raw = body.get("inv_movimientos") or []
        # Detect permission_denied / errors[] envelope on the very
        # first page only (cheap check).
        if page_num == 1 and not page_rows_raw and body.get("errors"):
            first = body["errors"][0]
            msg = first.get("message") if isinstance(first, dict) else str(first)
            return {
                "success": False, "error_code": "proceso_denied",
                "error": msg,
            }
        # Upper-case keys + filter to _KEEP_KEYS in one pass per row.
        for r in page_rows_raw:
            if not isinstance(r, dict):
                continue
            uk = _upper_keys(r)
            kept = {k: v for k, v in uk.items() if k in _KEEP_KEYS}
            rows.append(kept)
            if len(rows) >= max_rows:
                break
        # Stop if we hit the cap, the page was short, or we exhausted
        # the universe.
        if len(rows) >= max_rows:
            break
        if len(page_rows_raw) < PAGE_SIZE:
            break
        if total_count and len(rows) >= total_count:
            break
        page_num += 1
    truncated = total_count > len(rows)

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
    # INV_FAMI is a hierarchy — the leaf id on each line points to a
    # subfamily; we want the TOP-LEVEL parent's name as the pivot
    # row label (matches the operator's manual report).
    familia_parent, familia_self = await _resolve_family_hierarchy(client)
    familia_names = familia_parent  # used by _pivot (top-level)
    subfamilia_names = familia_self  # used by _write_detalle (leaf)

    product_ids: set[int] = set()
    invoice_ids: set[int] = set()
    for r in rows:
        try:
            pid = int(r.get("PRODUCTOS") or 0)
            if pid: product_ids.add(pid)
        except (TypeError, ValueError):
            pass
        try:
            iid = int(r.get("VENT_FACT_VENT") or 0)
            if iid: invoice_ids.add(iid)
        except (TypeError, ValueError):
            pass
    # Product resolution = 1 GET per UNIQUE product. A busy day at
    # Mepriga has ~1000 distinct products → 1000 sequential GETs ~3
    # minutes, well past the LLM tool-call timeout. The INFORME pivot
    # does NOT need PRODUCTOS rows (the line carries INV_FAMI already).
    # Only enable resolve_products when the caller really wants the
    # PRODUCTOS.CODIGO column in VENTAS_DETALLE — and even then expect
    # ~30s per 100 distinct products.
    if resolve_products:
        product_info = await _resolve_products(client, product_ids)
    else:
        product_info = {}
    # The factura join can be SLOW (1 GET per invoice). For typical
    # daily reports there are 100-300 distinct invoices. Disabled by
    # default — the line already carries everything the INFORME pivot
    # needs. Caller can re-enable with resolve_facturas=True if they
    # want the full 29-column VENTAS_DETALLE with the SRI number
    # decomposed.
    if resolve_facturas:
        factura_info = await _resolve_facturas(client, invoice_ids)
    else:
        factura_info = {}

    # 3. Build XLSX.
    wb = Workbook()
    wb.remove(wb.active)

    informe_ws = wb.create_sheet("INFORME")
    bodegas, fechas, table = _pivot(rows, bodega_names, familia_names)
    company_name = getattr(client.cfg, "commercial_name", None) or "MEPRIGA"
    last_row = _write_informe(informe_ws, bodegas, fechas, table,
                              company_name=company_name)

    # Hidden _datos_graficos sheet + 4 dashboard charts on INFORME.
    # Charts read from the hidden sheet, never from the visible pivot
    # table, so multi-day reports (where the visible pivot has per-date
    # subtotal rows interleaved) still chart cleanly.
    datos_ws = wb.create_sheet("_datos_graficos")
    familias_chart, fam_x_bod, bod_totals = _aggregate_for_charts(bodegas, table)
    _write_charts_data(datos_ws, bodegas, familias_chart, fam_x_bod, bod_totals)
    _add_dashboard_charts(informe_ws, datos_ws, bodegas, familias_chart,
                          anchor_row=last_row)

    detalle_ws = wb.create_sheet("VENTAS_DETALLE")
    _write_detalle(detalle_ws, rows, bodega_names, familia_names,
                   subfamilia_names, product_info, factura_info)

    # 4. Aggregate totals for the response payload.
    grand_total_pvp = sum(
        v for fb in table.values() for v in fb.values()
    )
    grand_total_neto = sum(
        float(r.get("PRECIO_NETO_LINEA") or 0) for r in rows
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
