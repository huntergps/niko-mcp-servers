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
import re as _re
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


# ---------------------------------------------------------------------------
# Small helper for write_only mode — every styled cell needs a fresh
# WriteOnlyCell instance bound to the sheet. We use this helper everywhere
# so the call sites stay readable.
# ---------------------------------------------------------------------------

def _cell(ws, value=None, font=None, fill=None, alignment=None,
          number_format=None, border=None):
    """Build a styled ``WriteOnlyCell`` for ``ws``.

    ``write_only`` worksheets can't be addressed via ``ws.cell(row, col)``
    — rows are appended in order and each cell must be a fresh
    ``WriteOnlyCell`` (so the styles aren't shared accidentally). This
    wrapper keeps the call sites in :func:`_write_informe` /
    :func:`_write_detalle` short.
    """
    c = WriteOnlyCell(ws, value=value)
    if font:
        c.font = font
    if fill:
        c.fill = fill
    if alignment:
        c.alignment = alignment
    if number_format:
        c.number_format = number_format
    if border:
        c.border = border
    return c


def _merge(ws, *, start_row: int, start_column: int,
           end_row: int, end_column: int) -> None:
    """Add a merge range to a write_only worksheet.

    The normal :meth:`Worksheet.merge_cells` helper doesn't exist on
    :class:`WriteOnlyWorksheet`. Instead the merged range goes directly
    onto :attr:`ws.merged_cells` (a ``MultiCellRange``).
    """
    a = f"{get_column_letter(start_column)}{start_row}"
    b = f"{get_column_letter(end_column)}{end_row}"
    ws.merged_cells.add(f"{a}:{b}")

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
    "FECHA", "FECHA_CONTA",  # FECHA_CONTA carries the exact timestamp (hour:min:sec)
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
    "NAME",  # descriptor textual de la factura: id|#bill-id FAC-VTA estab-pto-sec cif-NOMBRE_CLIENTE
})


# Pre-compiled regex for parsing the ``NAME`` field of an INV_MOVIMIENTOS
# row. Velneo embeds the SRI number + customer CIF + customer name in
# that descriptor, e.g.
#   ``936246|   #102180  FAC-VTA 002-002-561795 2000026688001-GUERRERO ALDAS NARCISA``
# Group captures: establecimiento, punto_emision, secuencia, cif, customer_name.
# Optional trailing date "28 May 2026" is ignored.
_RX_NAME_FACVTA = _re.compile(
    r"FAC-VTA\s+(?P<est>\d{1,3})-(?P<pto>\d{1,3})-(?P<sec>\d+)\s+"
    r"(?P<cif>\S+?)-(?P<cli>.+?)"
    r"(?:\s+\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})?$"
)


def _parse_invoice_name(name: str) -> dict[str, str]:
    """Parse the ``NAME`` descriptor of an INV_MOVIMIENTOS row.

    Returns a dict with ``establecimiento``, ``pto_emision``, ``secuencia``,
    ``cif`` and ``cliente``. Missing/unparseable rows return empty strings.
    Cheap regex, no joins — the SRI number and customer name are right
    there in the descriptor so we never need to hit VENT_FACT_VENT or
    ENT for the daily report.
    """
    if not name or not isinstance(name, str):
        return {"establecimiento": "", "pto_emision": "", "secuencia": "",
                "cif": "", "cliente": ""}
    m = _RX_NAME_FACVTA.search(name)
    if not m:
        return {"establecimiento": "", "pto_emision": "", "secuencia": "",
                "cif": "", "cliente": ""}
    return {
        "establecimiento": m.group("est"),
        "pto_emision": m.group("pto"),
        "secuencia": m.group("sec"),
        "cif": m.group("cif"),
        "cliente": m.group("cli").strip(),
    }


def _fmt_datetime_es(value: Any) -> str:
    """Velneo ISO timestamp → ``dd/mm/yyyy HH:MM:SS`` (Ecuador display).

    Velneo emits ``...Z`` suffix on timestamps but the actual storage is
    UTC; we convert to America/Guayaquil (fixed UTC-5, no DST). If the
    convention turns out to be naive-local (Velneo's docs are unclear on
    this), set ``VELNEO_TZ_OFFSET_HOURS=0`` on the MCP env to skip the
    shift.
    """
    if not value or value == "Invalid Date":
        return ""
    s = str(value)
    if "T" not in s:
        return _fmt_date_es(s[:10])
    try:
        dt = datetime.strptime(s.replace("Z", "").split(".")[0],
                               "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        d, t = s.split("T", 1)
        return f"{_fmt_date_es(d)} {t.replace('Z', '').split('.')[0][:8]}"
    import os
    try:
        offset = float(os.environ.get("VELNEO_TZ_OFFSET_HOURS", "-5"))
    except (TypeError, ValueError):
        offset = -5.0
    if offset:
        from datetime import timedelta
        dt = dt + timedelta(hours=offset)
    return dt.strftime("%d/%m/%Y %H:%M:%S")


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
    # Write_only mode: rows must be appended in order. We assemble each
    # row across the 3 side-by-side blocks (cols A, B..1+n_bodegas, gap,
    # E, F, gap, H, I) and append once per row.
    #
    # Total cols used: max(1 + n_bodegas, 6, 9) = 9 (assuming n_bodegas<=8).
    n_bodegas = len(bodegas)
    max_col = max(1 + n_bodegas, 6, 9)
    n_rows = max(1 + len(familias), 1 + n_bodegas)

    # Header row
    hdr = [None] * max_col
    hdr[0] = "Familia"
    for j, bod in enumerate(bodegas, start=2):
        hdr[j - 1] = bod
    hdr[4] = "Familia"  # col E
    hdr[5] = "Total"    # col F
    hdr[7] = "Bodega"   # col H
    hdr[8] = "Venta"    # col I
    ws.append(hdr)

    # Data rows — one row per index ``i`` from 0 to n_rows-2 (i.e. body).
    # Each body row simultaneously holds: familia row in cols A..1+n_bodegas
    # AND cols E-F (if i < len(familias)), AND bodega row in cols H-I
    # (if i < n_bodegas).
    for i in range(n_rows - 1):
        row = [None] * max_col
        if i < len(familias):
            fam = familias[i]
            row[0] = fam
            for j, bod in enumerate(bodegas, start=2):
                row[j - 1] = fam_x_bod.get(fam, {}).get(bod, 0)
            row[4] = fam
            row[5] = sum(fam_x_bod.get(fam, {}).get(b, 0) for b in bodegas)
        if i < n_bodegas:
            bod = bodegas[i]
            row[7] = bod
            row[8] = bod_totals.get(bod, 0)
        ws.append(row)

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

    Uses openpyxl's ``write_only`` API throughout: rows are appended in
    order via ``ws.append([list of WriteOnlyCells])``, column widths and
    row heights and merged ranges are set before the row is written.

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
    kpi_value_font_total = Font(bold=True, size=20, color=PALETTE_WHITE, name="Calibri")
    section_font = Font(bold=True, size=13, color=PALETTE_PRIMARY, name="Calibri")
    hdr_font_white = Font(bold=True, size=11, color=PALETTE_WHITE, name="Calibri")
    body_font = Font(size=11, color="000000", name="Calibri")
    date_marker_font = Font(bold=True, size=11, color=PALETTE_SECONDARY, name="Calibri")
    subtotal_font = Font(bold=True, size=11, color=PALETTE_WHITE, name="Calibri")
    grand_font = Font(bold=True, size=12, color=PALETTE_WHITE, name="Calibri")
    bodega_total_font = Font(bold=True, size=11, color=PALETTE_PRIMARY, name="Calibri")

    thin = Side(border_style="thin", color="D0D7DE")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    n_bodegas = len(bodegas)

    # Layout columns (see module docstring above). For n_bodegas=2:
    #   A    margin (2 chars)
    #   B    Familia Principal label + family names
    #   C    visual extension of family column (header band + zebra)
    #   D-E  bodega values (ALMACEN, BODEGA)
    #   F    TOTAL
    FAMILY_LABEL_COL = 2
    FAMILY_EXTRA_COL = 3
    value_start_col = 4
    total_col = value_start_col + n_bodegas

    # In write_only mode, an ``empty`` row is a list of total_col Nones —
    # the indexing below is 0-based against this list.
    def _empty_row() -> list:
        return [None] * total_col

    # Column dimensions & sheet view — must be set BEFORE any append.
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions[get_column_letter(FAMILY_LABEL_COL)].width = 28
    ws.column_dimensions[get_column_letter(FAMILY_EXTRA_COL)].width = 16
    for j in range(value_start_col, total_col + 1):
        ws.column_dimensions[get_column_letter(j)].width = 22
    ws.sheet_view.showGridLines = False

    # Aggregate per-bodega and grand totals from the pivot table — used
    # by both the KPI cards and the grand-total row at the bottom.
    per_bodega_total: dict[str, float] = defaultdict(float)
    grand_total_all = 0.0
    for (_fecha, _fam), bodega_vals in table.items():
        for bod, v in bodega_vals.items():
            per_bodega_total[bod] += v
            grand_total_all += v

    # ------------------------------------------------------------------
    # Row 1: blank top margin
    # ------------------------------------------------------------------
    ws.append(_empty_row())

    # ------------------------------------------------------------------
    # Row 2: MEPRIGA banner (merged B..total_col)
    # ------------------------------------------------------------------
    ws.row_dimensions[2].height = 38
    _merge(ws, start_row=2, start_column=2, end_row=2, end_column=total_col)
    row_data = _empty_row()
    row_data[FAMILY_LABEL_COL - 1] = _cell(
        ws, value=f"MEPRIGA — {company_name}",
        font=title_font, fill=primary_fill,
        alignment=Alignment(horizontal="center", vertical="center"),
    )
    ws.append(row_data)

    # ------------------------------------------------------------------
    # Row 3: subtitle
    # ------------------------------------------------------------------
    ws.row_dimensions[3].height = 24
    _merge(ws, start_row=3, start_column=2, end_row=3, end_column=total_col)
    row_data = _empty_row()
    row_data[FAMILY_LABEL_COL - 1] = _cell(
        ws, value="Reporte de Ventas Diarias",
        font=subtitle_font, fill=secondary_fill,
        alignment=Alignment(horizontal="center", vertical="center"),
    )
    ws.append(row_data)

    # ------------------------------------------------------------------
    # Row 4: period caption
    # ------------------------------------------------------------------
    if fechas:
        if len(fechas) == 1:
            periodo = f"Período: {_fmt_date_es(fechas[0])}"
        else:
            periodo = f"Período: {_fmt_date_es(fechas[0])} a {_fmt_date_es(fechas[-1])}"
    else:
        periodo = "Período: —"
    ws.row_dimensions[4].height = 18
    _merge(ws, start_row=4, start_column=2, end_row=4, end_column=total_col)
    row_data = _empty_row()
    row_data[FAMILY_LABEL_COL - 1] = _cell(
        ws, value=periodo,
        font=caption_font,
        alignment=Alignment(horizontal="center", vertical="center"),
    )
    ws.append(row_data)

    # ------------------------------------------------------------------
    # Row 5: blank gutter
    # ------------------------------------------------------------------
    ws.append(_empty_row())

    # ------------------------------------------------------------------
    # KPI cards (rows 6-7)
    # ------------------------------------------------------------------
    # For n_bodegas=2, layout is:
    #   ALMACEN   merged B-C
    #   BODEGA    merged D-E
    #   TOTAL     col F (single, intentionally narrower)
    cards = [(bod, per_bodega_total.get(bod, 0.0), _bodega_color(bod)) for bod in bodegas]
    cards.append(("TOTAL GENERAL", grand_total_all, PALETTE_PRIMARY))
    spans = [2] * n_bodegas + [1]

    # Row 6: labels
    ws.row_dimensions[6].height = 22
    row_data = _empty_row()
    col = 2
    for (label, _value, color), span in zip(cards, spans):
        end_col = col + span - 1
        if span > 1:
            _merge(ws, start_row=6, start_column=col, end_row=6, end_column=end_col)
        row_data[col - 1] = _cell(
            ws, value=label,
            font=kpi_label_font,
            fill=PatternFill("solid", fgColor=color),
            alignment=center,
        )
        col = end_col + 1
    ws.append(row_data)

    # Row 7: values
    ws.row_dimensions[7].height = 32
    row_data = _empty_row()
    col = 2
    for (label, value, color), span in zip(cards, spans):
        end_col = col + span - 1
        if span > 1:
            _merge(ws, start_row=7, start_column=col, end_row=7, end_column=end_col)
        if label == "TOTAL GENERAL":
            vfont = kpi_value_font_total
            vfill = PatternFill("solid", fgColor=color)
        else:
            vfont = Font(bold=True, size=20, color=color, name="Calibri")
            vfill = None
        row_data[col - 1] = _cell(
            ws, value=value,
            font=vfont, fill=vfill,
            alignment=Alignment(horizontal="center", vertical="center"),
            number_format=fmt, border=box,
        )
        col = end_col + 1
    ws.append(row_data)

    # ------------------------------------------------------------------
    # Rows 8-9: blank gutters before the pivot table
    # ------------------------------------------------------------------
    ws.append(_empty_row())
    ws.append(_empty_row())

    # ------------------------------------------------------------------
    # Row 10: section title "Ventas por Familia y Bodega"
    # ------------------------------------------------------------------
    SECTION_ROW = 10
    ws.row_dimensions[SECTION_ROW].height = 22
    _merge(ws, start_row=SECTION_ROW, start_column=2,
                   end_row=SECTION_ROW, end_column=total_col)
    row_data = _empty_row()
    row_data[FAMILY_LABEL_COL - 1] = _cell(
        ws, value="Ventas por Familia y Bodega",
        font=section_font, alignment=left,
    )
    ws.append(row_data)

    # ------------------------------------------------------------------
    # Row 11: table header
    # ------------------------------------------------------------------
    HDR_ROW = 11
    ws.row_dimensions[HDR_ROW].height = 22
    row_data = _empty_row()
    row_data[FAMILY_LABEL_COL - 1] = _cell(
        ws, value="Familia Principal",
        font=hdr_font_white, fill=table_hdr_fill,
        alignment=left, border=box,
    )
    row_data[FAMILY_EXTRA_COL - 1] = _cell(ws, fill=table_hdr_fill)
    for j, bod in enumerate(bodegas, start=value_start_col):
        row_data[j - 1] = _cell(
            ws, value=bod,
            font=hdr_font_white, fill=table_hdr_fill,
            alignment=center, border=box,
        )
    row_data[total_col - 1] = _cell(
        ws, value="TOTAL",
        font=hdr_font_white, fill=table_hdr_fill,
        alignment=center, border=box,
    )
    ws.append(row_data)

    # ------------------------------------------------------------------
    # Rows 12+: family rows (with optional per-fecha date markers and
    # subtotals when the range spans multiple days)
    # ------------------------------------------------------------------
    row = HDR_ROW + 1
    grand_by_bod: dict[str, float] = defaultdict(float)
    grand_total_recompute = 0.0
    use_date_grouping = len(fechas) > 1

    for fecha in fechas:
        if use_date_grouping:
            # Date marker row (merged label across all visible cols).
            ws.row_dimensions[row].height = 20
            _merge(ws, start_row=row, start_column=2,
                           end_row=row, end_column=total_col)
            row_data = _empty_row()
            row_data[FAMILY_LABEL_COL - 1] = _cell(
                ws, value=_fmt_date_es(fecha),
                font=date_marker_font, alignment=left,
            )
            ws.append(row_data)
            row += 1

        familias = sorted({fam for (f, fam) in table if f == fecha})
        date_by_bod: dict[str, float] = defaultdict(float)
        date_total_all = 0.0

        for idx, fam in enumerate(familias):
            zebra = (idx % 2 == 1)
            row_data = _empty_row()
            row_data[FAMILY_LABEL_COL - 1] = _cell(
                ws, value=fam,
                font=body_font, alignment=left,
                fill=zebra_fill if zebra else None,
            )
            if zebra:
                row_data[FAMILY_EXTRA_COL - 1] = _cell(ws, fill=zebra_fill)
            row_total = 0.0
            for j, bod in enumerate(bodegas, start=value_start_col):
                v = table[(fecha, fam)].get(bod, 0.0)
                row_data[j - 1] = _cell(
                    ws, value=(v if v else None),
                    alignment=right, number_format=fmt,
                    fill=zebra_fill if zebra else None,
                )
                row_total += v
                date_by_bod[bod] += v
                grand_by_bod[bod] += v
            row_data[total_col - 1] = _cell(
                ws, value=row_total,
                font=bodega_total_font,
                fill=total_col_fill,
                alignment=right, number_format=fmt,
            )
            date_total_all += row_total
            grand_total_recompute += row_total
            ws.append(row_data)
            row += 1

        if use_date_grouping:
            # Date subtotal row (label merged B-C; values in D..F).
            ws.row_dimensions[row].height = 22
            _merge(ws, start_row=row, start_column=FAMILY_LABEL_COL,
                           end_row=row, end_column=FAMILY_EXTRA_COL)
            row_data = _empty_row()
            row_data[FAMILY_LABEL_COL - 1] = _cell(
                ws, value=f"Subtotal {_fmt_date_es(fecha)}",
                font=subtotal_font, fill=subtotal_fill, alignment=left,
            )
            row_data[FAMILY_EXTRA_COL - 1] = _cell(ws, fill=subtotal_fill)
            for j, bod in enumerate(bodegas, start=value_start_col):
                row_data[j - 1] = _cell(
                    ws, value=date_by_bod[bod],
                    font=subtotal_font, fill=subtotal_fill,
                    alignment=right, number_format=fmt,
                )
            row_data[total_col - 1] = _cell(
                ws, value=date_total_all,
                font=subtotal_font, fill=subtotal_fill,
                alignment=right, number_format=fmt,
            )
            ws.append(row_data)
            row += 1

    # ------------------------------------------------------------------
    # Grand total row — label merged B-C, values D..F
    # ------------------------------------------------------------------
    ws.row_dimensions[row].height = 26
    _merge(ws, start_row=row, start_column=FAMILY_LABEL_COL,
                   end_row=row, end_column=FAMILY_EXTRA_COL)
    grand_label_align = Alignment(horizontal="left", vertical="center", indent=1)
    grand_value_align = Alignment(horizontal="right", vertical="center", indent=1)
    row_data = _empty_row()
    row_data[FAMILY_LABEL_COL - 1] = _cell(
        ws, value="TOTAL GENERAL",
        font=grand_font, fill=grand_fill, alignment=grand_label_align,
    )
    row_data[FAMILY_EXTRA_COL - 1] = _cell(ws, fill=grand_fill)
    for j, bod in enumerate(bodegas, start=value_start_col):
        row_data[j - 1] = _cell(
            ws, value=grand_by_bod[bod],
            font=grand_font, fill=grand_fill,
            alignment=grand_value_align, number_format=fmt,
        )
    row_data[total_col - 1] = _cell(
        ws, value=grand_total_recompute,
        font=grand_font, fill=grand_fill,
        alignment=grand_value_align, number_format=fmt,
    )
    ws.append(row_data)
    last_row = row

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

    headers = [
        "CodBar", "Codigo", "Nombre", "Empaque", "Factor", "Cantidad",
        "Precio Bruto Empaque", "Dscto Empaque", "IVA Empaque", "PVP Linea",
        "Costo Empaque", "Precio Neto Empaque", "Utilidad",
        "Costo Linea", "Precio Neto Linea", "IVA Linea",
        "Bodega", "Familia Principal", "SubFamilia",
        "Id Venta", "Establecimiento", "Pto Emision", "Secuencia",
        "Fecha", "Fecha Hora", "Cliente", "CIF", "MES", "DIA", "AÑO",
    ]

    # Column widths — must be set BEFORE appending any row in write_only mode.
    # 30 columns (matches headers above).
    widths = [16, 12, 34, 8, 8, 10,
              12, 10, 10, 12,
              12, 12, 10,
              12, 14, 10,
              22, 22, 22,
              10, 6, 6, 10,
              12, 19, 28, 16, 6, 6, 6]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "A2"

    money_fmt = MONEY_FORMAT
    money_cols_idx0 = {6, 7, 8, 9, 10, 11, 13, 14, 15}  # 0-based for list indexing

    # Header row — appended via WriteOnlyCell so each header carries the
    # corporate-blue band + white bold font.
    hdr_row = [
        _cell(ws, value=h, font=header_font, fill=header_fill, alignment=header_align)
        for h in headers
    ]
    ws.append(hdr_row)

    # Data rows — appended as plain Python lists, no per-cell styling.
    # Money columns get their format from a single per-row WriteOnlyCell
    # (cheaper than full styling on every cell).
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

        # SubFamilia is the LEAF family id (the value in INV_FAMI itself).
        # ``subfamilia_names`` maps leaf_id -> leaf_name. The old code
        # tried to read INV_SUBFAMI from the product join (always empty
        # without resolve_products=True). The row already carries what
        # we need — no join required.
        sub_name = subfamilia_names.get(fam_id, "")

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

        # Parse the row's NAME descriptor for SRI estab/pto/seq + customer
        # CIF + customer name — no VENT_FACT_VENT join required.
        parsed = _parse_invoice_name(r.get("NAME") or "")
        # When the regex couldn't parse, fall back to the factura_info
        # join (only populated if resolve_facturas=True), then to EMP/SUC
        # as a last resort for the establecimiento/pto cells.
        est = parsed["establecimiento"] or fact.get("ESTABLECIMIENTO") or r.get("EMP") or ""
        pto = parsed["pto_emision"] or fact.get("PUNTOEMISION") or (
            f"{int(r.get('SUC')):03d}" if isinstance(r.get("SUC"), int) else ""
        )
        seq = parsed["secuencia"] or fact.get("SECUENCIA") or ""
        cliente = parsed["cliente"] or fact.get("RAZONSOCIALCOMPRADOR") or ""
        cif = parsed["cif"] or fact.get("SRI_IDENTIFICACION") or ""

        fecha_hora_str = _fmt_datetime_es(r.get("FECHA_CONTA"))

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
            sub_name,
            inv_id or "",
            est,
            pto,
            seq,
            _fmt_date_es(fecha_str),
            fecha_hora_str,
            cliente,
            cif,
            int(mm) if mm else "",
            int(dd) if dd else "",
            int(yyyy) if yyyy else "",
        ]
        # Wrap money columns in WriteOnlyCells so they keep the currency
        # number format; other columns go as plain Python values (cheap).
        out_row = []
        for j, v in enumerate(values):
            if j in money_cols_idx0:
                out_row.append(_cell(ws, value=v, number_format=money_fmt))
            else:
                out_row.append(v)
        ws.append(out_row)


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

    # 3. Build XLSX. ``write_only`` mode streams rows directly to disk
    # (via the openpyxl internal LXML serializer) instead of building
    # a Python tree of Cell objects in memory. Required to keep RAM
    # bounded on rangos grandes — a normal Workbook with 100k×29 cells
    # consumes ~700 MB; write_only keeps the same workload under ~10 MB.
    wb = Workbook(write_only=True)

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
