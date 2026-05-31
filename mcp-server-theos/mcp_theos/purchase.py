"""Informe de RECOMENDACIÓN DE COMPRA en PDF (Mepriga).

Cruza los productos más vendidos (demanda histórica) con las existencias
actuales (EXS) y recomienda cuánto comprar para el próximo mes, con tres
criterios lado a lado:

  1. Reposición simple    = max(0, demanda_mes - stock)
  2. Con +20% seguridad   = max(0, demanda_mes*1.20 - stock)
  3. Cobertura N meses     = max(0, demanda_mes*N - stock)   (N configurable)

``compute_purchase`` recibe:
  * ``top`` = lista de productos vendidos: [{producto_id, nombre, cod_bar,
    cantidad}] (de summarize_sales -> top_productos_por_cantidad).
  * ``stock_by_id`` = {producto_id: {"existencia", "costo", "familia", "nombre"}}.
  * ``n_days_hist`` = días del rango histórico (para la tasa mensual).
  * ``coverage_months`` = N para el 3er criterio.

No hace I/O. La lectura del ERP la hace el caller. Devuelve (pdf, resumen).
Reusa las primitivas de dibujo de ``executive_report``.
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

from mcp_theos.executive_report import (  # noqa: E402
    _g, _fnum, _money, _k, _chart, _style,
    _banner, _footer, _section, _kpi_cards, _table, _text_block,
    PRIMARY, SECONDARY, GREEN, ORANGE, RED, SUBTLE, ZEBRA,
    H, PAGE_W, PAGE_H, LM, RM,
)


def _int(v: Any) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def compute_purchase(top: list, stock_by_id: dict, n_days_hist: int,
                     coverage_months: float = 1.5) -> dict[str, Any]:
    days = max(1, int(n_days_hist or 1))
    items = []
    for p in top:
        pid = _int(_g(p, "producto_id", "id"))
        vendidas = _fnum(_g(p, "cantidad", "vendidas"))
        if vendidas <= 0:
            continue
        st = stock_by_id.get(pid, {})
        stock = _fnum(_g(st, "existencia", "exs"))
        costo = _fnum(_g(st, "costo", "costo_compra", "costo_promedio"))
        nombre = str(_g(p, "nombre", "name") or _g(st, "nombre") or f"ID#{pid}")
        familia = str(_g(st, "familia", default="") or "")
        demanda_mes = vendidas / days * 30.0
        comprar_simple = max(0.0, demanda_mes - stock)
        comprar_seg = max(0.0, demanda_mes * 1.20 - stock)
        comprar_cob = max(0.0, demanda_mes * coverage_months - stock)
        cobertura_dias = (stock / (demanda_mes / 30.0)) if demanda_mes > 0 else 9999
        items.append({
            "producto_id": pid, "nombre": nombre, "familia": familia,
            "vendidas": vendidas, "demanda_mes": demanda_mes,
            "stock": stock, "costo": costo,
            "cobertura_dias": cobertura_dias,
            "comprar_simple": comprar_simple,
            "comprar_seg": comprar_seg,
            "comprar_cob": comprar_cob,
            "inversion_seg": comprar_seg * costo,
        })
    items.sort(key=lambda x: -x["demanda_mes"])
    inv_simple = sum(i["comprar_simple"] * i["costo"] for i in items)
    inv_seg = sum(i["comprar_seg"] * i["costo"] for i in items)
    inv_cob = sum(i["comprar_cob"] * i["costo"] for i in items)
    urgentes = [i for i in items if i["stock"] <= 0 or i["cobertura_dias"] < 15]
    return {
        "items": items,
        "n_productos": len(items),
        "coverage_months": coverage_months,
        "inv_simple": inv_simple, "inv_seg": inv_seg, "inv_cob": inv_cob,
        "n_urgentes": len(urgentes),
        "urgentes": urgentes,
    }


def _chart_top_compra(items, topn=12):
    """Barras horizontales: unidades a comprar (criterio +20%) por producto."""
    fig, ax = plt.subplots(figsize=(9.2, max(2.4, 0.42 * min(len(items), topn) + 1.2)))
    top = [i for i in items if i["comprar_seg"] > 0][:topn]
    if not top:
        ax.text(0.5, 0.5, "Sin compras sugeridas (stock suficiente)", ha="center",
                va="center", transform=ax.transAxes, color=SUBTLE); ax.axis("off")
        return _chart(fig)
    labels = [str(i["nombre"])[:30] for i in reversed(top)]
    vals = [i["comprar_seg"] for i in reversed(top)]
    bars = ax.barh(labels, vals, color=SECONDARY, height=0.7)
    if vals:
        bars[-1].set_color(PRIMARY)
    for b, v in zip(bars, vals):
        ax.text(v, b.get_y() + b.get_height() / 2, f" {_int(v):,}", va="center",
                fontsize=8.5, color=SUBTLE)
    ax.tick_params(labelsize=8)
    ax.grid(axis="x", color=ZEBRA, linewidth=1); _style(ax)
    ax.set_title("Unidades a comprar (con +20% seguridad)", fontsize=11,
                 fontweight="bold", color=PRIMARY, loc="left")
    return _chart(fig)


def build_purchase_report_pdf(pur: dict[str, Any], periodo_label: str,
                              target_label: str) -> tuple[bytes, str]:
    items = pur["items"]
    cov = pur["coverage_months"]
    rango = f"Compra para {target_label}"
    sub = f"Demanda base: {periodo_label}"

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    # Páginas: 1 = resumen + chart, luego N páginas de tabla detalle.
    per_page = 26
    n_detail_pages = max(1, (len(items) + per_page - 1) // per_page)
    TP = 1 + n_detail_pages

    # ---- Página 1 ----
    _banner(c, "Recomendacion de compra", sub, rango)
    y = PAGE_H - 118
    y = _section(c, y, "Que comprar para el proximo mes")
    y = _kpi_cards(c, y, [
        {"label": "Productos", "valor": f"{pur['n_productos']}", "icon": "doc",
         "accent": PRIMARY, "sub": "analizados"},
        {"label": "Inversion +20%", "valor": _money(pur["inv_seg"]), "icon": "dollar",
         "accent": SECONDARY, "sub": "criterio recomendado"},
        {"label": f"Cobertura {cov:g}m", "valor": _money(pur["inv_cob"]), "icon": "net",
         "accent": GREEN, "sub": "para no quebrar"},
        {"label": "Urgentes", "valor": f"{pur['n_urgentes']}", "icon": "return",
         "accent": RED, "sub": "stock < 15 dias", "subcol": RED},
    ])
    c.drawImage(_chart_top_compra(items), LM, y - 230, width=PAGE_W - LM - RM,
                height=230, preserveAspectRatio=True, mask="auto")
    y -= 246
    y = _section(c, y, "Como leer este informe")
    _text_block(c, y, [
        f"Para cada producto se proyecta la <b>demanda del proximo mes</b> segun su "
        f"velocidad de venta en {periodo_label}, y se resta el <b>stock actual</b>. "
        f"La compra sugerida se muestra con tres criterios:",
        "<b>1. Reposicion</b> = cubrir justo la demanda. <b>2. +20% seguridad</b> = "
        "colchon para no quedar sin stock (recomendado). "
        f"<b>3. Cobertura {cov:g} meses</b> = comprar para {cov:g} meses de demanda.",
        f"Inversiones totales estimadas (a costo): reposicion {_money(pur['inv_simple'])} · "
        f"+20% {_money(pur['inv_seg'])} · cobertura {_money(pur['inv_cob'])}.",
    ])
    _footer(c, 1, TP)
    c.showPage()

    # ---- Páginas de detalle ----
    headers = ["Producto", "Vend.", "Dem.mes", "Stock", "Repos.", "+20%", f"{cov:g}m", "Inv.+20%"]
    widths = [150, 40, 50, 44, 48, 48, 44, 75]
    align = ["l", "r", "r", "r", "r", "r", "r", "r"]
    page = 2
    for start in range(0, len(items), per_page):
        chunk = items[start:start + per_page]
        _banner(c, "Detalle por producto",
                f"Compra sugerida (pag {page - 1} de {n_detail_pages})", rango)
        y = PAGE_H - 118
        y = _section(c, y, "Productos mas vendidos vs existencias")
        rows = []
        for i in chunk:
            rows.append([
                str(i["nombre"])[:26],
                _int(i["vendidas"]),
                _int(i["demanda_mes"]),
                _int(i["stock"]),
                _int(i["comprar_simple"]),
                _int(i["comprar_seg"]),
                _int(i["comprar_cob"]),
                _money(i["inversion_seg"]),
            ])
        _table(c, y, headers, rows, widths, align=align)
        _footer(c, page, TP)
        c.showPage()
        page += 1

    c.save()
    resumen = (f"Recomendacion de compra para {target_label}: {pur['n_productos']} productos, "
               f"inversion sugerida {_money(pur['inv_seg'])} (+20% seguridad), "
               f"{pur['n_urgentes']} productos urgentes (stock < 15 dias).")
    return buf.getvalue(), resumen
