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


def build_single_day_hourly_png(
    *,
    hourly: list[dict[str, Any]],
    period_label: str,
    total_pvp: float,
    n_fact: int,
    reference_pvp: float | None = None,
    reference_label: str | None = None,
) -> bytes:
    """Single-day evolution: barras por hora (volumen) + línea de tendencia
    superpuesta con marcadores, pico resaltado.

    Estilo combinado barras+línea: la barra muestra el volumen de cada hora y
    la línea encima marca la curva del día (la "evolución" que el gerente
    espera ver). If ``reference_pvp`` is provided, a dashed horizontal line is
    drawn at that value (e.g. avg-per-hour over the last 7 days).
    """
    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=110)
    fig.patch.set_facecolor("white")
    fig.suptitle(f"Evolución horaria — {period_label}", fontsize=17,
                 fontweight="bold", color=COLOR_PRIMARY, y=0.97)
    fig.text(0.5, 0.92,
             f"Total ${total_pvp:,.0f}  ·  {n_fact:,} fact",
             ha="center", fontsize=11, color=COLOR_SUBTLE)

    if not hourly:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=COLOR_SUBTLE)
        ax.axis("off")
    else:
        hrs = [d["hora"] for d in hourly]
        vals = [float(d["pvp"]) for d in hourly]
        xs = list(range(len(hrs)))
        # Barras = volumen por hora (suaves, para que la línea destaque).
        bars = ax.bar(xs, vals, color=COLOR_SECONDARY, edgecolor="none",
                      width=0.62, alpha=0.55, zorder=2)
        # Línea de tendencia encima con marcadores = la "curva" del día.
        ax.plot(xs, vals, color=COLOR_PRIMARY, linewidth=2.4,
                marker="o", markersize=6, markerfacecolor="white",
                markeredgecolor=COLOR_PRIMARY, markeredgewidth=1.8,
                zorder=4)
        ax.set_xticks(xs)
        ax.set_xticklabels(hrs)
        if vals:
            peak_i = max(range(len(vals)), key=lambda i: vals[i])
            bars[peak_i].set_color(COLOR_PRIMARY)
            bars[peak_i].set_alpha(0.85)
            # Anotar el pico sobre el marcador de la línea.
            ax.annotate(f"pico {hrs[peak_i]}  ${vals[peak_i]:,.0f}",
                        xy=(peak_i, vals[peak_i]),
                        xytext=(0, 14), textcoords="offset points",
                        ha="center", va="bottom", fontsize=10,
                        fontweight="bold", color=COLOR_PRIMARY, zorder=6)
        if reference_pvp is not None and reference_pvp > 0:
            label = reference_label or f"Promedio ${reference_pvp:,.0f}"
            ax.axhline(reference_pvp, color=COLOR_ORANGE,
                       linestyle="--", linewidth=1.6, alpha=0.85,
                       label=label, zorder=5)
            ax.legend(loc="upper right", fontsize=9, frameon=False)
        # Holgura arriba para que la anotación del pico no se corte.
        if vals and max(vals) > 0:
            ax.set_ylim(0, max(vals) * 1.18)
        ax.yaxis.set_major_formatter(mtick.FuncFormatter(_fmt_dollar))
        ax.tick_params(axis="x", labelsize=10, rotation=45)
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="y", color=COLOR_ZEBRA, linewidth=1)
        ax.set_axisbelow(True)
        ax.set_xlabel("Hora", fontsize=11, color=COLOR_SUBTLE)
        ax.set_ylabel("PVP ($)", fontsize=11, color=COLOR_SUBTLE)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="white", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def build_hourly_compare_png(
    *,
    day_x_hour: dict[str, dict[str, float]],
    period_label: str,
    totals_per_day: dict[str, float],
) -> bytes:
    """Multi-day evolution: one line per day, X axis = hours.

    ``day_x_hour`` shape: ``{day_iso: {hora_label: pvp}}``.
    """
    fig, ax = plt.subplots(figsize=(13, 7), dpi=110)
    fig.patch.set_facecolor("white")
    fig.suptitle(f"Evolución por hora — {period_label}", fontsize=17,
                 fontweight="bold", color=COLOR_PRIMARY, y=0.97)

    # Build a sorted superset of hour labels across all days.
    all_hours: set[str] = set()
    for hours_dict in day_x_hour.values():
        all_hours.update(hours_dict.keys())
    sorted_hours = sorted(all_hours)

    palette = [COLOR_PRIMARY, COLOR_GREEN, COLOR_ORANGE, COLOR_PURPLE,
               COLOR_CYAN, COLOR_RED, COLOR_SECONDARY, "#404040"]

    if not sorted_hours or not day_x_hour:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=COLOR_SUBTLE)
        ax.axis("off")
    else:
        # Use NaN for hours-without-data instead of 0 so matplotlib
        # draws a gap on the line rather than crashing the plot to $0
        # (which gave the misleading "ventas cayeron a cero" effect on
        # the in-progress current day before noon).
        import math as _math
        sorted_days = sorted(day_x_hour.keys())
        for idx, day in enumerate(sorted_days):
            day_hours = day_x_hour[day]
            y = [day_hours.get(h, _math.nan) for h in sorted_hours]
            color = palette[idx % len(palette)]
            # Recompute day_total only over the hours WE HAVE — passing
            # NaN to sum would explode. Falls back to the externally
            # provided totals_per_day when present.
            real_vals = [v for v in y if not _math.isnan(v)]
            day_total = totals_per_day.get(day, sum(real_vals))
            # day label: "dd/mm  $total"
            from datetime import date
            try:
                d = date.fromisoformat(day)
                day_lbl = f"{d.day:02d}/{d.month:02d}  ${day_total:,.0f}"
            except ValueError:
                day_lbl = f"{day}  ${day_total:,.0f}"
            ax.plot(sorted_hours, y, marker="o", linewidth=2.4,
                    color=color, label=day_lbl, markersize=6)

        ax.yaxis.set_major_formatter(mtick.FuncFormatter(_fmt_dollar))
        ax.tick_params(axis="x", labelsize=10, rotation=45)
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="y", color=COLOR_ZEBRA, linewidth=1)
        ax.set_axisbelow(True)
        ax.set_xlabel("Hora", fontsize=11, color=COLOR_SUBTLE)
        ax.set_ylabel("PVP ($)", fontsize=11, color=COLOR_SUBTLE)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.legend(loc="best", fontsize=10, frameon=False)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="white", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def build_daily_trend_png(
    *,
    daily: list[dict[str, Any]],
    period_label: str,
    total_pvp: float,
    n_fact_total: int,
    benchmark_pvp: float | None = None,
    benchmark_label: str | None = None,
) -> bytes:
    """Daily trend: LÍNEA por día con relleno de área (la curva de evolución
    temporal que el gerente espera), promedio y benchmark de referencia."""
    fig, ax = plt.subplots(figsize=(13, 7), dpi=110)
    fig.patch.set_facecolor("white")
    fig.suptitle(f"Tendencia diaria — {period_label}", fontsize=17,
                 fontweight="bold", color=COLOR_PRIMARY, y=0.97)
    fig.text(0.5, 0.92,
             f"Total ${total_pvp:,.0f}  ·  {n_fact_total:,} fact",
             ha="center", fontsize=11, color=COLOR_SUBTLE)

    if not daily:
        ax.text(0.5, 0.5, "sin datos", ha="center", va="center",
                transform=ax.transAxes, color=COLOR_SUBTLE)
        ax.axis("off")
    else:
        from datetime import date
        labels = []
        for d in daily:
            try:
                dd = date.fromisoformat(d["day"])
                labels.append(f"{dd.day:02d}/{dd.month:02d}")
            except (ValueError, KeyError):
                labels.append(d.get("day", "?"))
        vals = [float(d["pvp"]) for d in daily]
        xs = list(range(len(labels)))
        # Línea de evolución + relleno de área debajo.
        ax.fill_between(xs, vals, color=COLOR_SECONDARY, alpha=0.18, zorder=1)
        ax.plot(xs, vals, color=COLOR_PRIMARY, linewidth=2.6,
                marker="o", markersize=6, markerfacecolor="white",
                markeredgecolor=COLOR_PRIMARY, markeredgewidth=1.8, zorder=3)
        ax.set_xticks(xs)
        ax.set_xticklabels(labels)
        avg = sum(vals) / len(vals) if vals else 0
        ax.axhline(avg, color=COLOR_ORANGE, linewidth=1.5, linestyle="--",
                   label=f"Prom. rango ${avg:,.0f}", alpha=0.85, zorder=2)
        if benchmark_pvp is not None and benchmark_pvp > 0:
            bench_lbl = benchmark_label or f"Prom. anterior ${benchmark_pvp:,.0f}"
            ax.axhline(benchmark_pvp, color=COLOR_GREEN, linewidth=1.5,
                       linestyle=":", label=bench_lbl, alpha=0.85, zorder=2)
        if vals:
            peak_i = max(range(len(vals)), key=lambda i: vals[i])
            ax.annotate(f"máx ${vals[peak_i]:,.0f}",
                        xy=(peak_i, vals[peak_i]),
                        xytext=(0, 12), textcoords="offset points",
                        ha="center", fontsize=10, fontweight="bold",
                        color=COLOR_PRIMARY, zorder=5)
            ax.set_ylim(0, max(vals) * 1.18)
        ax.yaxis.set_major_formatter(mtick.FuncFormatter(_fmt_dollar))
        ax.tick_params(axis="x", labelsize=10, rotation=45)
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="y", color=COLOR_ZEBRA, linewidth=1)
        ax.set_axisbelow(True)
        ax.set_xlabel("Fecha", fontsize=11, color=COLOR_SUBTLE)
        ax.set_ylabel("PVP ($)", fontsize=11, color=COLOR_SUBTLE)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.legend(loc="best", fontsize=10, frameon=False)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="white", dpi=110)
    plt.close(fig)
    return buf.getvalue()


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
