"""Dashboard PNG generator for the chat sales-summary tool.

We render a single 2x2 panel image with matplotlib (Agg backend so the
container needs no display). The four panels — Familia / Hora / Pago /
Caja — are the four lenses the operator most often asks for in chat.

The bytes returned by :func:`build_dashboard_png` are PNG-encoded and
ready to upload to Telegram via the Bot API ``sendPhoto`` or
``sendDocument`` endpoint.
"""

from __future__ import annotations

import io
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mtick  # noqa: E402


# Corporate palette — same hex codes the XLSX uses.
COLOR_PRIMARY = "#1F4E78"
COLOR_SECONDARY = "#2E75B6"
COLOR_GREEN = "#70AD47"
COLOR_RED = "#C0504D"
COLOR_ORANGE = "#ED7D31"
COLOR_PURPLE = "#8064A2"
COLOR_CYAN = "#4BACC6"
COLOR_SUBTLE = "#595959"
COLOR_ZEBRA = "#F8F9FB"

PALETTE_PIE = [
    COLOR_PRIMARY, COLOR_SECONDARY, COLOR_GREEN, COLOR_ORANGE,
    COLOR_PURPLE, COLOR_CYAN, COLOR_RED, "#A5A5A5",
]


def _fmt_dollar(x: float, _pos: int | None = None) -> str:
    if x >= 1000:
        return f"${x / 1000:.1f}k"
    return f"${int(x):,}"


def _format_period_label(date_from: str, date_to: str) -> str:
    """``2026-05-26`` / ``2026-05-28`` -> ``26 → 28 may 2026`` style."""
    try:
        from datetime import date
        df = date.fromisoformat(date_from[:10])
        dt = date.fromisoformat(date_to[:10])
    except ValueError:
        return f"{date_from} → {date_to}"
    months_es = ["ene", "feb", "mar", "abr", "may", "jun", "jul",
                 "ago", "sep", "oct", "nov", "dic"]
    if df == dt:
        return f"{df.day:02d} {months_es[df.month - 1]} {df.year}"
    if df.year == dt.year and df.month == dt.month:
        return f"{df.day:02d}→{dt.day:02d} {months_es[df.month - 1]} {df.year}"
    return f"{df.day:02d} {months_es[df.month - 1]} → {dt.day:02d} {months_es[dt.month - 1]} {df.year}"


def build_dashboard_png(
    summary: dict[str, Any],
    *,
    period_label: str | None = None,
    top_n: int = 6,
) -> bytes:
    """Render the 2x2 sales dashboard. Returns PNG bytes.

    ``summary`` is the ``sales_quick_summary`` JSON response.
    """
    totals = summary.get("totals") or {}
    por_familia = summary.get("por_familia") or []
    por_hora = summary.get("por_hora") or []
    por_pto_emi = summary.get("por_pto_emision") or []
    por_pago = summary.get("por_forma_pago") or []

    period_label = period_label or _format_period_label(
        summary.get("date_from") or "",
        summary.get("date_to") or summary.get("date_from") or "",
    )

    total_pvp = float(totals.get("pvp") or 0)
    n_fact = int(totals.get("n_facturas") or 0)
    n_lin = int(totals.get("n_lineas") or 0)
    saldo_neto = totals.get("saldo_neto")
    nc_total = totals.get("nc_total")

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), dpi=110)
    fig.patch.set_facecolor("white")
    suptitle = f"Ventas — {period_label}"
    subtitle = f"Total ${total_pvp:,.0f}  ·  {n_fact:,} fact  ·  {n_lin:,} líneas"
    if nc_total is not None and saldo_neto is not None:
        subtitle += f"  ·  NCs -${float(nc_total):,.0f}  ·  Neto ${float(saldo_neto):,.0f}"
    fig.suptitle(suptitle, fontsize=18, fontweight="bold",
                 color=COLOR_PRIMARY, y=0.99)
    fig.text(0.5, 0.95, subtitle, ha="center", fontsize=11,
             color=COLOR_SUBTLE)

    # -----------------------------------------------------------------
    # Panel (0,0): Familia — donut top N + "Otros"
    # -----------------------------------------------------------------
    ax = axes[0, 0]
    if por_familia:
        top = por_familia[:top_n]
        labels = [d["familia"][:18] for d in top]
        values = [float(d["pvp"]) for d in top]
        otros = sum(float(d["pvp"]) for d in por_familia[top_n:])
        if otros > 0:
            labels.append("Otros")
            values.append(otros)
        colors = PALETTE_PIE[:len(values)]
        wedges, texts, autotexts = ax.pie(
            values, labels=None, autopct="%1.0f%%",
            startangle=90, colors=colors,
            wedgeprops={"width": 0.45, "edgecolor": "white", "linewidth": 1.5},
            pctdistance=0.78,
            textprops={"fontsize": 10, "color": "white", "fontweight": "bold"},
        )
        ax.legend(wedges, labels, loc="center left",
                  bbox_to_anchor=(1.0, 0.5),
                  fontsize=9, frameon=False)
    else:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=COLOR_SUBTLE)
        ax.axis("off")
    ax.set_title("Por familia", fontsize=13, fontweight="bold",
                 color=COLOR_PRIMARY, pad=14)

    # -----------------------------------------------------------------
    # Panel (0,1): Hora — barras verticales
    # -----------------------------------------------------------------
    ax = axes[0, 1]
    if por_hora:
        hrs = [d["hora"] for d in por_hora]
        vals = [float(d["pvp"]) for d in por_hora]
        bars = ax.bar(hrs, vals, color=COLOR_SECONDARY, edgecolor="none",
                      width=0.7)
        # Highlight the peak in primary blue
        if vals:
            peak_i = max(range(len(vals)), key=lambda i: vals[i])
            bars[peak_i].set_color(COLOR_PRIMARY)
        ax.yaxis.set_major_formatter(mtick.FuncFormatter(_fmt_dollar))
        ax.tick_params(axis="x", labelsize=8, rotation=45)
        ax.tick_params(axis="y", labelsize=9)
        ax.grid(axis="y", color=COLOR_ZEBRA, linewidth=1)
        ax.set_axisbelow(True)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
    else:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=COLOR_SUBTLE)
        ax.axis("off")
    ax.set_title("Por hora", fontsize=13, fontweight="bold",
                 color=COLOR_PRIMARY, pad=14)

    # -----------------------------------------------------------------
    # Panel (1,0): Contado vs Crédito — donut comparativo
    # -----------------------------------------------------------------
    ax = axes[1, 0]
    contado = float(totals.get("pvp_contado") or 0)
    credito = float(totals.get("pvp_credito") or 0)
    if contado + credito > 0:
        labels = ["Contado", "Crédito"]
        values = [contado, credito]
        colors = [COLOR_GREEN, COLOR_ORANGE]
        wedges, texts, autotexts = ax.pie(
            values, labels=None, autopct=lambda p: f"{p:.0f}%",
            startangle=90, colors=colors,
            wedgeprops={"width": 0.45, "edgecolor": "white", "linewidth": 1.5},
            pctdistance=0.78,
            textprops={"fontsize": 13, "color": "white", "fontweight": "bold"},
        )
        custom_labels = [
            f"Contado  ${contado:,.0f}",
            f"Crédito  ${credito:,.0f}",
        ]
        ax.legend(wedges, custom_labels, loc="center left",
                  bbox_to_anchor=(1.0, 0.5), fontsize=10, frameon=False)
    else:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=COLOR_SUBTLE)
        ax.axis("off")
    ax.set_title("Contado vs Crédito", fontsize=13, fontweight="bold",
                 color=COLOR_PRIMARY, pad=14)

    # -----------------------------------------------------------------
    # Panel (1,1): Top cajas — barras horizontales
    # -----------------------------------------------------------------
    ax = axes[1, 1]
    if por_pto_emi:
        top_cajas = por_pto_emi[:top_n]
        labels = [d["establecimiento_pto"] for d in reversed(top_cajas)]
        vals = [float(d["pvp"]) for d in reversed(top_cajas)]
        bars = ax.barh(labels, vals, color=COLOR_SECONDARY,
                       edgecolor="none", height=0.7)
        # Highlight top earner in primary
        if vals:
            top_i = len(vals) - 1  # last after reverse = highest
            bars[top_i].set_color(COLOR_PRIMARY)
        ax.xaxis.set_major_formatter(mtick.FuncFormatter(_fmt_dollar))
        ax.tick_params(axis="x", labelsize=9)
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="x", color=COLOR_ZEBRA, linewidth=1)
        ax.set_axisbelow(True)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        # Annotate values to the right of each bar
        for bar, v in zip(bars, vals):
            ax.text(v, bar.get_y() + bar.get_height() / 2,
                    f"  ${v:,.0f}", va="center", fontsize=9,
                    color=COLOR_SUBTLE)
    else:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=COLOR_SUBTLE)
        ax.axis("off")
    ax.set_title("Top cajas (estab-pto)", fontsize=13, fontweight="bold",
                 color=COLOR_PRIMARY, pad=14)

    fig.tight_layout(rect=(0, 0, 1, 0.93))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="white", dpi=110)
    plt.close(fig)
    return buf.getvalue()
