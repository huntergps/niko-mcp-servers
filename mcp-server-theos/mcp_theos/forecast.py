"""Informe de PRONÓSTICO de ventas en PDF (Mepriga).

Proyecta el próximo mes a partir de N meses de historial usando:
  * Tendencia lineal sobre la serie diaria (polyfit grado 1).
  * Estacionalidad por día de la semana (factor = promedio del día / promedio global).
  * Reparto por familia proporcional a la participación histórica.

Recibe ``hist`` = salida de ``summarize_sales`` sobre la ventana histórica
(tiene ``por_dia`` con {day, pvp} y ``por_familia`` con {familia, pvp, pct}).
No hace I/O — la lectura del ERP la hace el caller. Devuelve (pdf_bytes, resumen).

Reusa las primitivas de dibujo de ``executive_report`` para mantener el
mismo look corporativo Mepriga.
"""
from __future__ import annotations

import io
import calendar
import datetime as _dt
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mtick  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from mcp_theos.executive_report import (  # noqa: E402
    _g, _list, _fnum, _money, _k, _chart, _style,
    _banner, _footer, _section, _kpi_cards, _table, _text_block,
    _chart_barh,
    PRIMARY, SECONDARY, GREEN, ORANGE, RED, SUBTLE, ZEBRA,
    H, PAGE_W, PAGE_H, LM, RM,
)

_DOW_ES = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]


def _parse_day(s: str):
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _next_month(ref: _dt.date) -> tuple[int, int]:
    y, m = ref.year, ref.month
    return (y + 1, 1) if m == 12 else (y, m + 1)


def compute_forecast(hist: dict[str, Any], target_year: int, target_month: int) -> dict[str, Any]:
    """Construye la proyección del mes objetivo a partir del histórico."""
    por_dia = _list(hist, "por_dia_combinado", "por_dia")
    serie = []  # (date, pvp)
    for d in por_dia:
        dd = _parse_day(_g(d, "day", "dia", "fecha"))
        if dd is not None:
            serie.append((dd, _fnum(_g(d, "pvp"))))
    serie.sort(key=lambda x: x[0])

    n = len(serie)
    vals = [v for _, v in serie]
    # Tendencia lineal (sobre indice de dia con dato).
    import numpy as np
    if n >= 3:
        x = np.arange(n)
        slope, intercept = np.polyfit(x, vals, 1)
    else:
        slope = 0.0
        intercept = (sum(vals) / n) if n else 0.0
    prom_hist = (sum(vals) / n) if n else 0.0

    # Estacionalidad por dia de semana: factor = prom(dow) / prom_global.
    dow_sum = [0.0] * 7
    dow_cnt = [0] * 7
    for dd, v in serie:
        w = dd.weekday()
        dow_sum[w] += v
        dow_cnt[w] += 1
    dow_factor = []
    for i in range(7):
        avg_dow = (dow_sum[i] / dow_cnt[i]) if dow_cnt[i] else prom_hist
        dow_factor.append((avg_dow / prom_hist) if prom_hist else 1.0)

    # Nivel base proyectado: valor de tendencia al centro del mes objetivo,
    # extendiendo el indice lineal desde el ultimo dia con dato.
    last_idx = n - 1 if n else 0
    days_in_target = calendar.monthrange(target_year, target_month)[1]
    # gap en dias desde el ultimo dato historico hasta el dia 1 del objetivo
    if serie:
        last_date = serie[-1][0]
        first_target = _dt.date(target_year, target_month, 1)
        gap = (first_target - last_date).days
    else:
        gap = 1

    proj_dias = []  # {day, pvp, dow}
    total_proj = 0.0
    for d in range(1, days_in_target + 1):
        fecha = _dt.date(target_year, target_month, d)
        idx = last_idx + gap + (d - 1)
        base = slope * idx + intercept
        base = max(base, prom_hist * 0.3, 0.0)  # piso defensivo
        val = base * dow_factor[fecha.weekday()]
        val = max(val, 0.0)
        total_proj += val
        proj_dias.append({"day": fecha.isoformat(), "pvp": round(val, 2),
                          "dow": fecha.weekday()})

    # Proyeccion por familia: proporcional a la participacion historica.
    por_fam = _list(hist, "por_familia")
    tot_fam = sum(_fnum(_g(f, "pvp")) for f in por_fam) or 1.0
    proj_familia = []
    for f in por_fam:
        share = _fnum(_g(f, "pvp")) / tot_fam
        proj_familia.append({"familia": str(_g(f, "familia", "nombre", default="?")),
                             "pvp": round(total_proj * share, 2),
                             "pct": round(share * 100, 1)})

    # Proyeccion por dia de semana (suma del mes objetivo por dow).
    dow_proj = [0.0] * 7
    for p in proj_dias:
        dow_proj[p["dow"]] += p["pvp"]

    tendencia = ("al alza" if slope > 0 else "a la baja" if slope < 0 else "estable")
    return {
        "target_year": target_year, "target_month": target_month,
        "proj_total": round(total_proj, 2),
        "proj_prom_diario": round(total_proj / days_in_target, 2) if days_in_target else 0.0,
        "hist_prom_diario": round(prom_hist, 2),
        "hist_total": round(sum(vals), 2),
        "n_dias_hist": n,
        "tendencia": tendencia,
        "slope": slope,
        "proj_dias": proj_dias,
        "proj_familia": proj_familia,
        "dow_proj": dow_proj,
        "serie_hist": [{"day": dd.isoformat(), "pvp": v} for dd, v in serie],
    }


def _chart_hist_proj(serie_hist, proj_dias):
    """Serie histórica (barras claras) + proyección (barras rayadas)."""
    fig, ax = plt.subplots(figsize=(9.4, 3.4))
    hv = [_fnum(_g(d, "pvp")) for d in serie_hist]
    pv = [_fnum(_g(d, "pvp")) for d in proj_dias]
    nh, npj = len(hv), len(pv)
    xh = list(range(nh))
    xp = list(range(nh, nh + npj))
    ax.bar(xh, hv, color="#9DC3E6", width=0.9, label="Historico (diario)")
    ax.bar(xp, pv, color="#F2C9A0", width=0.9, hatch="//", alpha=0.9, label="Proyeccion")
    # linea de tendencia sobre todo
    import numpy as np
    allv = hv + pv
    if len(allv) >= 3:
        xx = np.arange(len(allv))
        sl, ic = np.polyfit(np.arange(nh) if nh >= 2 else np.arange(len(allv)),
                            hv if nh >= 2 else allv, 1)
        ax.plot(xx, sl * xx + ic, color=ORANGE, linewidth=2, linestyle="--", label="Tendencia")
    ax.axvline(nh - 0.5, color=SUBTLE, linewidth=1, linestyle=":")
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(_k))
    ax.tick_params(axis="x", labelbottom=False)
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="y", color=ZEBRA, linewidth=1); _style(ax)
    ax.legend(fontsize=8.5, frameon=False, loc="upper left")
    ax.set_title("Ventas diarias: historico y proyeccion", fontsize=11,
                 fontweight="bold", color=PRIMARY, loc="left")
    return _chart(fig)


def _chart_dow(dow_proj):
    """Proyección por día de la semana."""
    fig, ax = plt.subplots(figsize=(9.2, 3.0))
    bars = ax.bar(_DOW_ES, dow_proj, color=SECONDARY, width=0.7)
    if any(dow_proj):
        pk = max(range(7), key=lambda i: dow_proj[i])
        bars[pk].set_color(PRIMARY)
        ax.annotate(f"mejor: {_DOW_ES[pk]}", (pk, dow_proj[pk]),
                    textcoords="offset points", xytext=(0, 6), ha="center",
                    fontsize=9, fontweight="bold", color=PRIMARY)
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(_k))
    ax.tick_params(labelsize=8.5)
    ax.grid(axis="y", color=ZEBRA, linewidth=1); _style(ax)
    ax.set_title("Proyeccion por dia de la semana (total del mes)", fontsize=11,
                 fontweight="bold", color=PRIMARY, loc="left")
    return _chart(fig)


def build_forecast_report_pdf(hist: dict[str, Any], target_year: int,
                              target_month: int, hist_label: str = "") -> tuple[bytes, str]:
    f = compute_forecast(hist, target_year, target_month)
    mes_nombre = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
                  "julio", "agosto", "septiembre", "octubre", "noviembre",
                  "diciembre"][target_month]
    rango = f"Pronostico {mes_nombre} {target_year}"
    sub = f"Base historica: {hist_label}" if hist_label else "Basado en historial reciente"

    proj_total = f["proj_total"]
    fam_top = f["proj_familia"][0] if f["proj_familia"] else {"familia": "s/d", "pvp": 0}
    var_pct = ((f["proj_prom_diario"] - f["hist_prom_diario"]) / f["hist_prom_diario"] * 100
               if f["hist_prom_diario"] else 0.0)
    dow_best = max(range(7), key=lambda i: f["dow_proj"][i]) if any(f["dow_proj"]) else 0

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    TP = 2

    # ---- Página 1: KPIs + grafico historico/proyeccion ----
    _banner(c, "Pronostico de ventas", sub, rango)
    y = PAGE_H - 118
    y = _section(c, y, "Proyeccion del proximo mes")
    y = _kpi_cards(c, y, [
        {"label": "Venta proyectada", "valor": _money(proj_total), "icon": "dollar",
         "accent": PRIMARY, "sub": f"{mes_nombre} {target_year}"},
        {"label": "Prom. diario proy.", "valor": _money(f["proj_prom_diario"]), "icon": "net",
         "accent": SECONDARY, "sub": f"{var_pct:+.0f}% vs historico",
         "subcol": (GREEN if var_pct >= 0 else RED)},
        {"label": "Tendencia", "valor": f["tendencia"].split()[-1].capitalize(), "icon": "ticket",
         "accent": (GREEN if f["slope"] >= 0 else RED), "sub": "del periodo"},
        {"label": "Mejor dia", "valor": _DOW_ES[dow_best][:3], "icon": "doc",
         "accent": ORANGE, "sub": "proyectado"},
    ])
    c.drawImage(_chart_hist_proj(f["serie_hist"], f["proj_dias"]),
                LM, y - 200, width=PAGE_W - LM - RM, height=200,
                preserveAspectRatio=True, mask="auto")
    y -= 216
    y = _section(c, y, "Lectura")
    _text_block(c, y, [
        f"Con base en {f['n_dias_hist']} dias de historial ({hist_label or 'reciente'}), "
        f"la venta proyectada para <b>{mes_nombre} {target_year}</b> es <b>{_money(proj_total)}</b> "
        f"(promedio diario {_money(f['proj_prom_diario'])}, {var_pct:+.0f}% vs el promedio historico).",
        f"La tendencia del periodo base es <b>{f['tendencia']}</b>. La familia con mayor "
        f"proyeccion es <b>{fam_top['familia']}</b> ({_money(fam_top['pvp'])}). "
        f"El mejor dia de la semana proyectado es <b>{_DOW_ES[dow_best]}</b>.",
        "<i>Metodo: tendencia lineal + estacionalidad por dia de la semana + reparto por "
        "familia segun participacion historica. Es una estimacion, no un compromiso.</i>",
    ])
    _footer(c, 1, TP)
    c.showPage()

    # ---- Página 2: por dia de semana + por familia ----
    _banner(c, "Detalle del pronostico", "Por dia de la semana y por familia", rango)
    y = PAGE_H - 118
    y = _section(c, y, "Proyeccion por dia de la semana")
    c.drawImage(_chart_dow(f["dow_proj"]), LM, y - 180, width=PAGE_W - LM - RM,
                height=180, preserveAspectRatio=True, mask="auto")
    y -= 196
    y = _section(c, y, "Proyeccion por familia")
    c.drawImage(_chart_barh(f["proj_familia"], ["familia"], ["pvp"], "Venta proyectada por familia"),
                LM, y - 200, width=PAGE_W - LM - RM, height=200,
                preserveAspectRatio=True, mask="auto")
    _footer(c, 2, TP)
    c.showPage()
    c.save()

    resumen = (f"Pronostico {mes_nombre} {target_year}: {_money(proj_total)} proyectado "
               f"({var_pct:+.0f}% vs historico), tendencia {f['tendencia']}. "
               f"Familia top: {fam_top['familia']}. Mejor dia: {_DOW_ES[dow_best]}.")
    return buf.getvalue(), resumen
