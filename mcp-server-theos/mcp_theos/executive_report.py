"""Informe ejecutivo de ventas en PDF (Mepriga).

Construye un PDF de 3 páginas a partir del dict que devuelve
``sales_quick_summary`` (mismo summarizer que alimenta el dashboard PNG).
No hace I/O ni llamadas al ERP: recibe el ``summary`` ya resuelto y
devuelve los bytes del PDF. La entrega (Telegram) la hace el caller con
``telegram_delivery.send_document``.

Diseño aprobado por el cliente (3 páginas):
  P1 — banner + 5 KPI cards con íconos (PVP, neto, ticket, facturas,
       devoluciones NC) + dona de familias + infografía contado/crédito.
  P2 — barras de ventas por hora (pico resaltado) + barras por punto de
       emisión + tabla top clientes.
  P3 — tendencia diaria con proyección a 3 días + lectura + recomendaciones.

Shape REAL consumido (verificado en sales_report.summarize_sales +
admin_ops.sales_quick_summary, 2026-05-30):

  summary = {
    "period": {"from": str, "to": str, "label": str},
    "totals": {"pvp": float, "neto": float, "n_facturas": int,
               "n_lineas": int, "ticket_promedio": float,
               "por_forma_pago": [{"forma": str, "pvp": float, ...}],
               "nc_total": float, "saldo_neto": float},   # nc_total/saldo_neto si include_credit_notes
    "por_dia":         [{"dia": str, "pvp": float, "n_facturas": int}],
    "por_hora":        [{"hora": int, "pvp": float, "n_facturas": int}],
    "por_familia":     [{"familia": str, "pvp": float, "pct": float, "n_lineas": int}],
    "por_bodega":      [{"bodega": str, "pvp": float, "n_facturas": int}],
    "por_pto_emision": [{"establecimiento_pto": str, "pvp": float, "n_facturas": int}],
    "top_clientes":    [{"cliente": str, "ruc": str, "pvp": float, "n_facturas": int}],
    "credit_notes":    {"total": float, "n_ncs": int, "por_dia": [...]} | None,
  }

Las funciones de chart usan accesores defensivos por si alguna clave
cambia de nombre en el futuro, pero estos son los nombres canónicos.
"""
from __future__ import annotations

import io
import datetime as _dt
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mtick  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.lib.colors import HexColor  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402

# ---- paleta corporativa Mepriga (idéntica a sales_chart.py) ----
PRIMARY = "#1F4E78"; SECONDARY = "#2E75B6"; GREEN = "#70AD47"; RED = "#C0504D"
ORANGE = "#ED7D31"; PURPLE = "#8064A2"; CYAN = "#4BACC6"; SUBTLE = "#595959"
ZEBRA = "#F4F7FB"; GREY = "#A5A5A5"
PIE = [PRIMARY, SECONDARY, GREEN, ORANGE, PURPLE, CYAN, RED, GREY]
H = lambda s: HexColor(s)  # noqa: E731
PAGE_W, PAGE_H = A4
LM = RM = 38
TM = 50
BM = 36


# ---------------------------------------------------------------------------
# accesores defensivos
# ---------------------------------------------------------------------------
def _g(d: Any, *keys: str, default: Any = 0.0) -> Any:
    d = d or {}
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _list(s: Any, *keys: str) -> list:
    s = s or {}
    for k in keys:
        v = s.get(k)
        if v:
            return v
    return []


def _fnum(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _money(v: Any) -> str:
    return f"${_fnum(v):,.0f}"


def _k(v: Any, _pos: int | None = None) -> str:
    v = _fnum(v)
    return f"${v / 1000:.1f}k" if v >= 1000 else f"${int(v):,}"


def _forma_pago_split(por_forma: list) -> tuple[float, float]:
    """Suma contado vs crédito desde la lista por_forma_pago.

    ``forma`` es la etiqueta de la forma de pago. Cualquier etiqueta que
    contenga 'cred' (CREDITO / Crédito / a crédito) cuenta como crédito;
    el resto (CONTADO / EFECTIVO / TARJETA / etc.) como contado.
    """
    contado = credito = 0.0
    for fp in por_forma or []:
        pvp = _fnum(_g(fp, "pvp", "total"))
        label = str(_g(fp, "forma", "nombre", "name", default="")).lower()
        if "cred" in label:
            credito += pvp
        else:
            contado += pvp
    return contado, credito


# ---------------------------------------------------------------------------
# matplotlib -> ImageReader
# ---------------------------------------------------------------------------
def _chart(fig, dpi: int = 150) -> ImageReader:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return ImageReader(buf)


def _style(ax) -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_axisbelow(True)


# ---------------------------------------------------------------------------
# primitivas reportlab
# ---------------------------------------------------------------------------
def _banner(c, titulo: str, subtitulo: str, rango: str) -> None:
    c.setFillColor(H(PRIMARY)); c.rect(0, PAGE_H - 96, PAGE_W, 96, fill=1, stroke=0)
    c.setFillColor(H(SECONDARY)); c.rect(0, PAGE_H - 100, PAGE_W, 4, fill=1, stroke=0)
    c.setFillColor(H("#FFFFFF"))
    c.setFont("Helvetica-Bold", 25); c.drawString(LM, PAGE_H - 44, "MEPRIGA")
    c.setFont("Helvetica", 14); c.drawString(LM, PAGE_H - 64, titulo)
    c.setFont("Helvetica-Oblique", 10); c.drawString(LM, PAGE_H - 82, f"{subtitulo}  ·  {rango}")
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(PAGE_W - RM, PAGE_H - 44, _dt.date.today().strftime("%d/%m/%Y"))


def _footer(c, page: int, total: int) -> None:
    c.setStrokeColor(H("#E3E8EF")); c.setLineWidth(0.7); c.line(LM, 28, PAGE_W - RM, 28)
    c.setFillColor(H(SUBTLE)); c.setFont("Helvetica", 7.5)
    c.drawString(LM, 18, "Generado por Lila · datos del ERP en vivo · uso interno Mepriga")
    c.drawRightString(PAGE_W - RM, 18, f"Pagina {page} de {total}")


def _section(c, y: float, titulo: str) -> float:
    c.setFillColor(H(PRIMARY)); c.rect(LM, y - 20, 4, 18, fill=1, stroke=0)
    c.setFillColor(H(PRIMARY)); c.setFont("Helvetica-Bold", 13)
    c.drawString(LM + 12, y - 16, titulo.upper())
    return y - 34


def _icon(c, x: float, y: float, kind: str, col: str) -> None:
    c.saveState(); c.setStrokeColor(H(col)); c.setFillColor(H(col)); c.setLineWidth(1.6)
    if kind == "dollar":
        c.setFont("Helvetica-Bold", 17); c.drawCentredString(x, y - 6, "$")
    elif kind == "net":
        c.setFont("Helvetica-Bold", 15); c.drawCentredString(x, y - 6, "=")
    elif kind == "doc":
        c.rect(x - 6, y - 9, 12, 16, fill=0, stroke=1)
        for i in range(3):
            c.line(x - 3, y + 2 - i * 4, x + 3, y + 2 - i * 4)
    elif kind == "ticket":
        c.roundRect(x - 8, y - 6, 16, 12, 2, fill=0, stroke=1); c.line(x, y - 6, x, y + 6)
    elif kind == "return":
        c.setFont("Helvetica-Bold", 16); c.drawCentredString(x, y - 6, "R")
    c.restoreState()


def _kpi_cards(c, y: float, cards: list[dict]) -> float:
    n = len(cards); gap = 9
    cw = (PAGE_W - LM - RM - (n - 1) * gap) / n
    ch = 76
    for i, k in enumerate(cards):
        x = LM + i * (cw + gap)
        c.setFillColor(H("#FFFFFF")); c.setStrokeColor(H("#E0E6EF")); c.setLineWidth(1)
        c.roundRect(x, y - ch, cw, ch, 7, fill=1, stroke=1)
        accent = k.get("accent", PRIMARY)
        c.setFillColor(H(accent)); c.roundRect(x, y - ch, cw, 5, 2, fill=1, stroke=0)
        c.setFillColor(H("#EEF3FA")); c.circle(x + 16, y - 20, 11, fill=1, stroke=0)
        _icon(c, x + 16, y - 20, k.get("icon", "dollar"), accent)
        c.setFillColor(H(SUBTLE)); c.setFont("Helvetica", 7.5)
        c.drawString(x + 31, y - 18, k["label"].upper()[:18])
        c.setFillColor(H(PRIMARY)); c.setFont("Helvetica-Bold", 18)
        c.drawString(x + 10, y - 46, k["valor"])
        if k.get("sub"):
            c.setFillColor(H(k.get("subcol", SUBTLE))); c.setFont("Helvetica", 8)
            c.drawString(x + 10, y - 62, k["sub"])
    return y - ch - 16


def _table(c, y: float, headers: list, rows: list, widths: list, align=None) -> float:
    align = align or ["l"] * len(headers)
    rh = 17
    c.setFillColor(H(PRIMARY)); c.rect(LM, y - rh, sum(widths), rh, fill=1, stroke=0)
    c.setFillColor(H("#FFFFFF")); c.setFont("Helvetica-Bold", 8.5)
    x = LM
    for i, hd in enumerate(headers):
        if align[i] == "r":
            c.drawRightString(x + widths[i] - 6, y - rh + 5, str(hd))
        else:
            c.drawString(x + 6, y - rh + 5, str(hd))
        x += widths[i]
    y -= rh
    c.setFont("Helvetica", 8.5)
    for ri, row in enumerate(rows):
        if ri % 2:
            c.setFillColor(H(ZEBRA)); c.rect(LM, y - rh, sum(widths), rh, fill=1, stroke=0)
        c.setFillColor(H("#1A1A1A"))
        x = LM
        for i, v in enumerate(row):
            if align[i] == "r":
                c.drawRightString(x + widths[i] - 6, y - rh + 5, str(v))
            else:
                c.drawString(x + 6, y - rh + 5, str(v))
            x += widths[i]
        y -= rh
    return y - 8


def _text_block(c, y: float, parr: list[str], size: float = 9.5, lead: float = 13) -> float:
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    st = getSampleStyleSheet()["BodyText"]
    st.fontName = "Helvetica"; st.fontSize = size; st.leading = lead; st.textColor = H("#222222")
    for p in parr:
        para = Paragraph(p, st)
        w, h = para.wrap(PAGE_W - LM - RM, 400)
        para.drawOn(c, LM, y - h)
        y -= h + 6
    return y


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------
def _chart_familias(por_familia: list, topn: int = 6) -> ImageReader:
    fig, ax = plt.subplots(figsize=(4.4, 3.4))
    top = por_familia[:topn]
    labels = [str(_g(d, "familia", "nombre", default="?"))[:16] for d in top]
    vals = [_fnum(_g(d, "pvp")) for d in top]
    otros = sum(_fnum(_g(d, "pvp")) for d in por_familia[topn:])
    if otros > 0:
        labels.append("Otros"); vals.append(otros)
    if not vals:
        vals = [1]; labels = ["s/d"]
    wedges, _t, _a = ax.pie(
        vals, labels=None, autopct="%1.0f%%", startangle=90, colors=PIE[:len(vals)],
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1.5},
        pctdistance=0.79, textprops={"fontsize": 8, "color": "white", "fontweight": "bold"},
    )
    ax.legend(wedges, labels, loc="center left", bbox_to_anchor=(0.96, 0.5), fontsize=8, frameon=False)
    ax.text(0, 0, f"{_k(sum(vals))}\nventas", ha="center", va="center",
            fontsize=10, fontweight="bold", color=PRIMARY)
    return _chart(fig)


def _chart_pago(contado: float, credito: float) -> ImageReader:
    tot = contado + credito
    pc = round(contado / tot * 100) if tot else 0
    try:
        from pywaffle import Waffle
        fig = plt.figure(
            FigureClass=Waffle, rows=5, columns=10,
            values={f"Contado {pc}%": pc, f"Credito {100 - pc}%": 100 - pc},
            colors=[GREEN, ORANGE], icons=["money-bill-wave", "credit-card"],
            icon_legend=True, font_size=15,
            legend={"loc": "lower center", "bbox_to_anchor": (0.5, -0.25),
                    "ncol": 2, "framealpha": 0, "fontsize": 9},
            figsize=(4.4, 3.0),
        )
        return _chart(fig)
    except Exception:
        fig, ax = plt.subplots(figsize=(4.4, 3.0))
        for i in range(100):
            r, col = divmod(i, 10)
            ax.add_patch(plt.Rectangle((col, r), 0.86, 0.86, color=GREEN if i < pc else ORANGE))
        ax.set_xlim(-0.3, 10); ax.set_ylim(-0.3, 10); ax.invert_yaxis()
        ax.axis("off"); ax.set_aspect("equal")
        ax.text(5, 11.0, f"Contado {pc}%   -   Credito {100 - pc}%", ha="center",
                fontsize=10, fontweight="bold", color=PRIMARY)
        return _chart(fig)


def _chart_hora(por_hora: list) -> ImageReader:
    fig, ax = plt.subplots(figsize=(9.2, 3.0))
    hrs = []
    for d in por_hora:
        h = str(_g(d, "hora", "hour", default="")).replace("h00", "").strip()
        try:
            hrs.append(f"{int(h):02d}")
        except (TypeError, ValueError):
            hrs.append(str(_g(d, "hora", "hour")))
    vals = [_fnum(_g(d, "pvp")) for d in por_hora]
    bars = ax.bar(hrs, vals, color=SECONDARY, width=0.72)
    if vals:
        pk = max(range(len(vals)), key=lambda i: vals[i])
        bars[pk].set_color(PRIMARY)
        ax.annotate(f"pico {_k(vals[pk])}", (pk, vals[pk]), textcoords="offset points",
                    xytext=(0, 6), ha="center", fontsize=9, fontweight="bold", color=PRIMARY)
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(_k))
    ax.tick_params(labelsize=8)
    ax.grid(axis="y", color=ZEBRA, linewidth=1); _style(ax)
    ax.set_title("Ventas por hora - horas pico", fontsize=11, fontweight="bold", color=PRIMARY, loc="left")
    return _chart(fig)


def _chart_barh(items: list, namekeys: list, valkeys: list, titulo: str, topn: int = 10) -> ImageReader:
    fig, ax = plt.subplots(figsize=(9.2, 3.4))
    top = items[:topn]
    labels = [str(_g(d, *namekeys, default="?"))[:28] for d in reversed(top)]
    vals = [_fnum(_g(d, *valkeys)) for d in reversed(top)]
    if not vals:
        vals = [0]; labels = ["s/d"]
    bars = ax.barh(labels, vals, color=SECONDARY, height=0.7)
    if vals:
        bars[-1].set_color(PRIMARY)
    for b, v in zip(bars, vals):
        ax.text(v, b.get_y() + b.get_height() / 2, f" {_k(v)}", va="center", fontsize=8.5, color=SUBTLE)
    ax.xaxis.set_major_formatter(mtick.FuncFormatter(_k))
    ax.tick_params(labelsize=9)
    ax.grid(axis="x", color=ZEBRA, linewidth=1); _style(ax)
    ax.set_title(titulo, fontsize=11, fontweight="bold", color=PRIMARY, loc="left")
    return _chart(fig)


def _chart_tendencia(por_dia: list):
    import numpy as np
    fig, ax = plt.subplots(figsize=(9.4, 3.6))
    dias = [str(_g(d, "dia", "day", "fecha", "date")) for d in por_dia]
    vals = [_fnum(_g(d, "pvp")) for d in por_dia]
    xlab = []
    for d in dias:
        try:
            dd = _dt.date.fromisoformat(d[:10]); xlab.append(f"{dd.day:02d}/{dd.month:02d}")
        except ValueError:
            xlab.append(d[-5:])
    x = np.arange(len(vals))
    ax.bar(x, vals, color="#9DC3E6", width=0.6, label="Venta diaria")
    proj_txt = ""
    if len(vals) >= 3:
        sl, ic = np.polyfit(x, vals, 1)
        ax.plot(x, sl * x + ic, color=ORANGE, linewidth=2.2, linestyle="--", label="Tendencia")
        fx = np.arange(len(vals), len(vals) + 3)
        fy = np.clip(sl * fx + ic, 0, None)
        ax.bar(fx, fy, color="#F2C9A0", width=0.6, alpha=0.85, hatch="//", label="Proyeccion")
        for _ in fx:
            xlab.append("proy")
        proj_txt = _k(float(fy.sum()))
    ax.set_xticks(range(len(xlab))); ax.set_xticklabels(xlab, fontsize=7, rotation=45)
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(_k)); ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="y", color=ZEBRA, linewidth=1); _style(ax)
    ax.legend(fontsize=8.5, frameon=False, loc="upper left")
    ax.set_title("Tendencia diaria + proyeccion 3 dias", fontsize=11, fontweight="bold", color=PRIMARY, loc="left")
    return _chart(fig), proj_txt


def _build_matrix(cross: list, row_key: str, col_key: str,
                  top_rows: int = 6, top_cols: int = 8):
    """De una lista plana de cruces -> (rows, cols, matrix[r][c]) con las
    filas/columnas top por total. ``cross`` = [{row_key, col_key, 'pvp'}]."""
    row_tot: dict[str, float] = {}
    col_tot: dict[str, float] = {}
    cell: dict[tuple, float] = {}
    for d in cross:
        r = str(_g(d, row_key, default="?"))
        c = str(_g(d, col_key, default="?"))
        v = _fnum(_g(d, "pvp"))
        row_tot[r] = row_tot.get(r, 0) + v
        col_tot[c] = col_tot.get(c, 0) + v
        cell[(r, c)] = cell.get((r, c), 0) + v
    rows = [r for r, _ in sorted(row_tot.items(), key=lambda x: -x[1])[:top_rows]]
    cols = [c for c, _ in sorted(col_tot.items(), key=lambda x: -x[1])[:top_cols]]
    matrix = [[cell.get((r, c), 0.0) for c in cols] for r in rows]
    return rows, cols, matrix


def _chart_heatmap(rows, cols, matrix, titulo, row_label="", col_label=""):
    """Heatmap con valores anotados ($k). Filas=familias, cols=ubicaciones."""
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap
    nr, nc = len(rows), len(cols)
    fig, ax = plt.subplots(figsize=(9.4, max(2.2, 0.55 * nr + 1.3)))
    if nr == 0 or nc == 0:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=SUBTLE); ax.axis("off")
        return _chart(fig)
    M = np.array(matrix, dtype=float)
    cmap = LinearSegmentedColormap.from_list("mep", ["#FFFFFF", SECONDARY, PRIMARY])
    ax.imshow(M, aspect="auto", cmap=cmap, vmin=0, vmax=(M.max() or 1))
    ax.set_xticks(range(nc)); ax.set_xticklabels(cols, fontsize=8, rotation=30, ha="right")
    ax.set_yticks(range(nr)); ax.set_yticklabels([r[:22] for r in rows], fontsize=8)
    thr = (M.max() or 1) * 0.55
    for i in range(nr):
        for j in range(nc):
            v = M[i, j]
            if v <= 0:
                continue
            ax.text(j, i, _k(v), ha="center", va="center", fontsize=7,
                    color=("white" if v >= thr else "#1A1A1A"))
    for s in ("top", "right", "left", "bottom"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.set_title(titulo, fontsize=11, fontweight="bold", color=PRIMARY, loc="left")
    fig.tight_layout()
    return _chart(fig)


def _chart_dia_sucursal(cross_dia_pto, top_ptos, topn_dias=31):
    """Línea por sucursal a lo largo de los días (qué días rinden por caja)."""
    fig, ax = plt.subplots(figsize=(9.4, 3.4))
    # set de dias ordenados
    dias = sorted({str(_g(d, "day")) for d in cross_dia_pto})
    if not dias or not top_ptos:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=SUBTLE); ax.axis("off")
        return _chart(fig)
    val = {(str(_g(d, "day")), str(_g(d, "pto"))): _fnum(_g(d, "pvp")) for d in cross_dia_pto}
    palette = [PRIMARY, GREEN, ORANGE, PURPLE, CYAN, RED, SECONDARY, "#404040"]
    xlab = []
    for dd in dias:
        try:
            o = _dt.date.fromisoformat(dd[:10]); xlab.append(f"{o.day:02d}/{o.month:02d}")
        except ValueError:
            xlab.append(dd[-5:])
    x = list(range(len(dias)))
    for idx, pto in enumerate(top_ptos):
        y = [val.get((dd, pto), 0.0) for dd in dias]
        ax.plot(x, y, marker="o", markersize=3, linewidth=1.8,
                color=palette[idx % len(palette)], label=str(pto)[:18])
    ax.set_xticks(x); ax.set_xticklabels(xlab, fontsize=6.5, rotation=45)
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(_k)); ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="y", color=ZEBRA, linewidth=1); _style(ax)
    ax.legend(fontsize=7.5, frameon=False, ncol=3, loc="upper center")
    ax.set_title("Ventas diarias por sucursal", fontsize=11, fontweight="bold", color=PRIMARY, loc="left")
    return _chart(fig)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def build_executive_report_pdf(summary: dict[str, Any]) -> tuple[bytes, str]:
    """Construye el informe ejecutivo PDF. Devuelve ``(pdf_bytes, resumen)``.

    ``summary`` es el dict de ``sales_quick_summary`` (con
    ``include_credit_notes=True`` para que traiga nc_total/saldo_neto).
    """
    import numpy as np

    # Período: summarize_sales expone date_from/date_to top-level (no "period").
    d_from = str(_g(summary, "date_from", default=""))
    d_to = str(_g(summary, "date_to", default=""))
    rango = f"{d_from} -> {d_to}".strip(" ->") if (d_from or d_to) else ""

    totals = summary.get("totals") or {}
    por_familia = _list(summary, "por_familia")
    por_hora = _list(summary, "por_hora")
    por_pto = _list(summary, "por_pto_emision")
    por_bodega = _list(summary, "por_bodega")
    por_dia = _list(summary, "por_dia_combinado", "por_dia")
    top_cli = _list(summary, "top_clientes")
    cnotes = summary.get("credit_notes") or {}
    # Cruces (presentes solo si el tool pidio include_cross_tabs).
    cross_fam_pto = _list(summary, "cross_familia_pto")
    cross_fam_bod = _list(summary, "cross_familia_bodega")
    cross_dia_pto = _list(summary, "cross_dia_pto")
    por_sucursal = _list(summary, "por_sucursal")

    pvp = _fnum(_g(totals, "pvp"))
    nfac = int(_fnum(_g(totals, "n_facturas")))
    nc_tot = _fnum(_g(totals, "nc_total"))
    neto = _fnum(_g(totals, "saldo_neto", "neto", default=pvp - nc_tot))
    n_ncs = int(_fnum(_g(cnotes, "n_ncs")))
    # ticket: summarize_sales usa "ticket_promedio_pvp".
    ticket = _fnum(_g(totals, "ticket_promedio_pvp", "ticket_promedio",
                      default=(pvp / nfac if nfac else 0)))
    # Contado/credito: summarize_sales los da directos en totals
    # (pvp_contado / pvp_credito). Fallback: split de por_forma_pago.
    contado = _fnum(_g(totals, "pvp_contado"))
    credito = _fnum(_g(totals, "pvp_credito"))
    if contado == 0 and credito == 0:
        contado, credito = _forma_pago_split(_list(summary, "por_forma_pago"))
    pct_contado = contado / (contado + credito) * 100 if (contado + credito) else 0

    fam_lider = str(_g(por_familia[0], "familia", "nombre", default="s/d")) if por_familia else "s/d"
    fam_pct = _fnum(_g(por_familia[0], "pct")) if por_familia else 0
    if not fam_pct and por_familia and pvp:
        fam_pct = _fnum(_g(por_familia[0], "pvp")) / pvp * 100

    # Hora pico: "hora" puede venir como "08h00" (string) o como int.
    pico = max(por_hora, key=lambda d: _fnum(_g(d, "pvp")), default=None)
    if pico is not None:
        ph = str(_g(pico, "hora", default="")).replace("h00", "").strip()
        try:
            pico_lbl = f"{int(ph):02d}h"
        except (TypeError, ValueError):
            pico_lbl = f"{_g(pico, 'hora')}"
        pico_txt = f"{pico_lbl} ({_money(_g(pico, 'pvp'))})"
    else:
        pico_txt = "s/d"

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    TP = 6 if cross_fam_pto else 4

    # ---- Página 1 ----
    _banner(c, "Informe ejecutivo de ventas", "Resumen del periodo", rango)
    y = PAGE_H - 118
    y = _section(c, y, "Indicadores clave")
    y = _kpi_cards(c, y, [
        {"label": "PVP total", "valor": _money(pvp), "icon": "dollar", "accent": PRIMARY, "sub": f"{nfac:,} facturas"},
        {"label": "Venta neta", "valor": _money(neto), "icon": "net", "accent": GREEN, "sub": "tras NCs", "subcol": GREEN},
        {"label": "Ticket prom.", "valor": _money(ticket), "icon": "ticket", "accent": SECONDARY, "sub": "por factura"},
        {"label": "Devoluc. NC", "valor": _money(nc_tot), "icon": "return", "accent": RED, "sub": f"{n_ncs} notas", "subcol": RED},
    ])
    img_fam = _chart_familias(por_familia)
    img_pago = _chart_pago(contado, credito)
    half = (PAGE_W - LM - RM - 14) / 2
    chh = 175
    c.setFillColor(H(SUBTLE)); c.setFont("Helvetica-Bold", 10)
    c.drawString(LM, y - 2, "Ventas por familia")
    c.drawString(LM + half + 14, y - 2, "Contado vs credito")
    c.drawImage(img_fam, LM, y - chh - 6, width=half, height=chh, preserveAspectRatio=True, mask="auto")
    c.drawImage(img_pago, LM + half + 14, y - chh - 6, width=half, height=chh, preserveAspectRatio=True, mask="auto")
    y = y - chh - 22
    y = _section(c, y, "Resumen ejecutivo")
    _text_block(c, y, [
        f"- Ventas brutas <b>{_money(pvp)}</b> en {nfac:,} facturas; neto <b>{_money(neto)}</b> tras {_money(nc_tot)} en devoluciones (NC).",
        f"- Familia lider: <b>{fam_lider}</b> con el {fam_pct:.0f}% de la venta.",
        f"- Hora pico: <b>{pico_txt}</b>. Cobro: <b>{pct_contado:.0f}% contado</b> / {100 - pct_contado:.0f}% credito.",
    ])
    _footer(c, 1, TP)
    c.showPage()

    # ---- Página 2 ----
    _banner(c, "Operacion del periodo", "Horas y clientes", rango)
    y = PAGE_H - 118
    y = _section(c, y, "Ventas por hora")
    c.drawImage(_chart_hora(por_hora), LM, y - 200, width=PAGE_W - LM - RM, height=200, preserveAspectRatio=True, mask="auto")
    y -= 216
    if top_cli:
        y = _section(c, y, "Top clientes")
        rows = [[i, str(_g(cl, "cliente", "nombre", "name", default="?"))[:38],
                 f"{int(_fnum(_g(cl, 'n_facturas')))}", _money(_g(cl, "pvp", "ticket", "total"))]
                for i, cl in enumerate(top_cli[:10], 1)]
        _table(c, y, ["#", "Cliente", "Fact.", "Monto"], rows, [26, 320, 50, 123], align=["l", "l", "r", "r"])
    _footer(c, 2, TP)
    c.showPage()

    # ---- Página 3: desgloses por punto de emision (sucursal) y por bodega ----
    _banner(c, "Sucursales y bodegas", "Desglose de ventas por ubicacion", rango)
    y = PAGE_H - 118
    y = _section(c, y, "Ventas por punto de emision (sucursal / caja)")
    c.drawImage(_chart_barh(por_pto, ["establecimiento_pto", "pto", "name"], ["pvp"], "Por punto de emision"),
                LM, y - 250, width=PAGE_W - LM - RM, height=250, preserveAspectRatio=True, mask="auto")
    y -= 268
    y = _section(c, y, "Ventas por bodega")
    c.drawImage(_chart_barh(por_bodega, ["bodega", "name", "nombre"], ["pvp"], "Por bodega"),
                LM, y - 250, width=PAGE_W - LM - RM, height=250, preserveAspectRatio=True, mask="auto")
    _footer(c, 3, TP)
    c.showPage()

    pg = 4  # numero de pagina dinamico para lo que sigue

    # ---- Páginas de cruces (solo si hay cross-tabs) ----
    if cross_fam_pto:
        # Página: Familia x ubicacion (dos heatmaps)
        _banner(c, "Familia por ubicacion", "Que familia se vende mas en cada sitio", rango)
        y = PAGE_H - 118
        y = _section(c, y, "Familia x sucursal (punto de emision)")
        r1, c1, m1 = _build_matrix(cross_fam_pto, "familia", "pto", top_rows=6, top_cols=8)
        c.drawImage(_chart_heatmap(r1, c1, m1, "Ventas por familia y punto de emision ($)"),
                    LM, y - 250, width=PAGE_W - LM - RM, height=250, preserveAspectRatio=True, mask="auto")
        y -= 268
        y = _section(c, y, "Familia x bodega")
        r2, c2, m2 = _build_matrix(cross_fam_bod, "familia", "bodega", top_rows=6, top_cols=5)
        c.drawImage(_chart_heatmap(r2, c2, m2, "Ventas por familia y bodega ($)"),
                    LM, y - 230, width=PAGE_W - LM - RM, height=230, preserveAspectRatio=True, mask="auto")
        _footer(c, pg, TP); c.showPage(); pg += 1

        # Página: Comparativo entre sucursales
        _banner(c, "Comparativo de sucursales", "Ranking e indicadores por punto de emision", rango)
        y = PAGE_H - 118
        y = _section(c, y, "Ventas diarias por sucursal")
        top_ptos = [str(_g(s, "pto")) for s in por_sucursal[:6]]
        c.drawImage(_chart_dia_sucursal(cross_dia_pto, top_ptos),
                    LM, y - 200, width=PAGE_W - LM - RM, height=200, preserveAspectRatio=True, mask="auto")
        y -= 216
        y = _section(c, y, "Indicadores por sucursal")
        rows = []
        for s in por_sucursal[:10]:
            try:
                dt_lbl = _dt.date.fromisoformat(str(_g(s, "dia_top"))[:10]).strftime("%d/%m")
            except ValueError:
                dt_lbl = str(_g(s, "dia_top"))[-5:]
            rows.append([
                str(_g(s, "pto"))[:14],
                _money(_g(s, "pvp")),
                f"{_fnum(_g(s, 'pct')):.0f}%",
                f"{int(_fnum(_g(s, 'n_facturas')))}",
                _money(_g(s, "ticket_promedio")),
                str(_g(s, "familia_top", default="-"))[:16],
                dt_lbl,
            ])
        _table(c, y,
               ["Sucursal", "Ventas", "%", "Fact.", "Ticket", "Familia top", "Mejor dia"],
               rows, [70, 86, 34, 42, 70, 110, 50],
               align=["l", "r", "r", "r", "r", "l", "r"])
        _footer(c, pg, TP); c.showPage(); pg += 1

    # ---- Página: Tendencia (ultima) ----
    _banner(c, "Tendencia y proyeccion", "Lectura del periodo", rango)
    y = PAGE_H - 118
    y = _section(c, y, "Tendencia diaria y proyeccion")
    img_t, proj = _chart_tendencia(por_dia)
    c.drawImage(img_t, LM, y - 180, width=PAGE_W - LM - RM, height=180, preserveAspectRatio=True, mask="auto")
    y -= 196
    serie = [_fnum(_g(d, "pvp")) for d in por_dia]
    if len(serie) >= 3:
        slope = float(np.polyfit(np.arange(len(serie)), serie, 1)[0])
        tend = "al alza" if slope > 0 else "a la baja" if slope < 0 else "estable"
    else:
        tend = "sin datos suficientes"
    y = _section(c, y, "Lectura del periodo")
    y = _text_block(c, y, [
        f"La venta del periodo cierra en <b>{_money(pvp)}</b> brutos (<b>{_money(neto)}</b> netos tras NCs). "
        f"El ritmo diario viene <b>{tend}</b>"
        + (f", con proyeccion de <b>{proj}</b> para los proximos 3 dias si se mantiene la tendencia." if proj else "."),
        f"El cobro se concentra en {pct_contado:.0f}% contado (sostiene el flujo de caja); "
        f"el {100 - pct_contado:.0f}% a credito alimenta la cartera por cobrar.",
    ])
    y -= 6
    y = _section(c, y, "Recomendaciones")
    _text_block(c, y, [
        f"1. <b>Reforzar inventario</b> de la familia {fam_lider}, motor de la venta.",
        f"2. <b>Reforzar caja/personal</b> en la franja pico ({pico_txt}).",
        f"3. <b>Gestionar devoluciones</b> ({_money(nc_tot)} en NC): revisar causas para reducir el impacto en el neto.",
    ])
    _footer(c, pg, TP)
    c.showPage()
    c.save()

    resumen = (f"Informe ejecutivo {rango}: {_money(pvp)} PVP ({_money(neto)} neto), "
               f"tendencia {tend}. Familia lider: {fam_lider}.")
    return buf.getvalue(), resumen
