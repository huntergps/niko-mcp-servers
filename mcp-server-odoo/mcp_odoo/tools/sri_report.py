"""Informe de estado de documentos electronicos SRI (csolish/tecnoh).

Tool DETERMINISTICO: clasifica los documentos en PENDIENTES de enviar al SRI vs
INFORMATIVO (draft/cancel), aplicando la regla de saneamiento (ver memoria
reference-sri-saneamiento-documentos). Lo usa Lila en vez de improvisar con
aggregate_records/search_count (que clasifican mal: cuentan compras sin
retencion, incluyen NC compra que es silencio, etc.).

Devuelve el informe como texto monospace listo para Telegram, igual que el
script `sri_report.py` del cron — pero como tool, para que Lila NO improvise.
"""
from __future__ import annotations

from datetime import date, timedelta

from mcp_odoo.transports.xmlrpc import odoo_pool

VENTANA_DIAS = 7

VENTA_COLS = ["NO ENVIADO", "DEVUELTA", "NO AUTORIZADO", "SIN COMPROBANTE"]
VENTA_OTROS = ["RECIBIDA", "FAIL READ", "ERROR TCP"]
GUIA_ACCIONABLE = ["NO ENVIADO", "DEVUELTA"]


def _count(creds, model, domain):
    return odoo_pool.execute(*creds, model, "search_count", [domain], {})


def _search_invoice_ids(creds, model, domain, field="invoice_id"):
    rows = odoo_pool.execute(*creds, model, "search_read", [domain], {"fields": [field]})
    out = set()
    for r in rows or []:
        v = r.get(field)
        if isinstance(v, (list, tuple)):
            v = v[0]
        if v:
            out.add(v)
    return out


def _compras_pendiente(creds, fecha_from):
    """Compras (in_invoice) SIN COMPROBANTE que son pendiente real: liquidacion
    de compra (comprobante_id.code='03') O retencion real (tax.line tiporet
    ret_fuente/ret_iva, amount>0). Cuenta facturas DISTINTAS."""
    base = [["type", "=", "in_invoice"], ["state", "=", "posted"],
            ["ce_state", "=", "SIN COMPROBANTE"], ["invoice_date", ">=", fecha_from]]
    liq = _search_invoice_ids(creds, "account.move",
                              base + [["comprobante_id.code", "=", "03"]], field="id")
    ret = _search_invoice_ids(creds, "l10n_ec_sri.tax.line", [
        ["tiporet", "in", ["ret_fuente", "ret_iva"]],
        ["amount", ">", 0],
        ["invoice_id.type", "=", "in_invoice"],
        ["invoice_id.state", "=", "posted"],
        ["invoice_id.ce_state", "=", "SIN COMPROBANTE"],
        ["invoice_id.retencion_electronica_id", "=", False],
        ["invoice_id.invoice_date", ">=", fecha_from],
    ])
    return len(liq | ret)


def _empresa_nombre(creds):
    try:
        rows = odoo_pool.execute(*creds, "res.company", "search_read",
                                 [[]], {"fields": ["name"], "limit": 1})
        if rows:
            return rows[0].get("name") or "Empresa"
    except Exception:  # noqa: BLE001
        pass
    return "Empresa"


def sri_status_report(tenant_id, url, db, user, password):
    """Genera el informe SRI clasificado (PENDIENTES + INFORMATIVO) en texto."""
    creds = (tenant_id, url, db, user, password)
    hoy = date.today()
    fecha_from = (hoy - timedelta(days=VENTANA_DIAS)).isoformat()
    empresa = _empresa_nombre(creds)

    # ----- PENDIENTES -----
    pend = {}
    tipos = ["Facturas venta", "NC venta", "Facturas compra", "Guias", "Retenciones"]

    def venta(nombre, tp):
        b = [["type", "=", tp], ["state", "=", "posted"], ["invoice_date", ">=", fecha_from]]
        for est in VENTA_COLS:
            pend[(nombre, est)] = _count(creds, "account.move", b + [["ce_state", "=", est]])
        pend[(nombre, "OTROS")] = _count(creds, "account.move", b + [["ce_state", "in", VENTA_OTROS]])

    venta("Facturas venta", "out_invoice")
    venta("NC venta", "out_refund")

    for est in VENTA_COLS + ["OTROS"]:
        pend[("Facturas compra", est)] = 0
    pend[("Facturas compra", "SIN COMPROBANTE")] = _compras_pendiente(creds, fecha_from)
    # NC compra (in_refund): SILENCIO -> no se cuenta.

    gb = [["guia_remision_electronica_id", "!=", False], ["fechaemision", ">=", fecha_from]]
    for est in VENTA_COLS:
        pend[("Guias", est)] = (_count(creds, "stock.picking", gb + [["ce_state", "=", est]])
                                if est in GUIA_ACCIONABLE else 0)
    pend[("Guias", "OTROS")] = 0

    rb = [["comprobante_id.code", "=", "07"], ["create_date", ">=", fecha_from]]
    for est in VENTA_COLS:
        pend[("Retenciones", est)] = _count(creds, "l10n_ec_sri.documento.electronico",
                                            rb + [["estado", "=", est]])
    pend[("Retenciones", "OTROS")] = _count(creds, "l10n_ec_sri.documento.electronico",
                                            rb + [["estado", "in", VENTA_OTROS]])

    # ----- INFORMATIVO: draft/cancel -----
    info = {}
    for nombre, tp in [("Fact. venta", "out_invoice"), ("NC venta", "out_refund"),
                       ("Fact. compra", "in_invoice"), ("NC compra", "in_refund")]:
        for st in ("draft", "cancel"):
            info[(nombre, st)] = _count(creds, "account.move",
                [["type", "=", tp], ["state", "=", st], ["invoice_date", ">=", fecha_from]])

    # ----- RENDER -----
    cols = ["NO ENVIADO", "DEVUELTA", "NO AUTORIZADO", "SIN COMPROBANTE", "OTROS"]
    def ft(t):
        return sum(pend[(t, c)] for c in cols)
    gran = sum(ft(t) for t in tipos)

    out = [f"📋 Informe SRI {empresa} · {fecha_from} → {hoy.isoformat()}", "",
           "PENDIENTES de enviar al SRI:", "```",
           f"{'Tipo':<16}{'NoEnv':>6}{'Dev':>5}{'NoAut':>6}{'SinC':>6}{'Otros':>6}{'TOT':>5}"]
    for t in tipos:
        out.append(f"{t:<16}{pend[(t,'NO ENVIADO')]:>6}{pend[(t,'DEVUELTA')]:>5}"
                   f"{pend[(t,'NO AUTORIZADO')]:>6}{pend[(t,'SIN COMPROBANTE')]:>6}"
                   f"{pend[(t,'OTROS')]:>6}{ft(t):>5}")
    out.append("─" * 50)
    out.append(f"{'TOTAL':<16}{sum(pend[(t,'NO ENVIADO')] for t in tipos):>6}"
               f"{sum(pend[(t,'DEVUELTA')] for t in tipos):>5}"
               f"{sum(pend[(t,'NO AUTORIZADO')] for t in tipos):>6}"
               f"{sum(pend[(t,'SIN COMPROBANTE')] for t in tipos):>6}"
               f"{sum(pend[(t,'OTROS')] for t in tipos):>6}{gran:>5}")
    out.append("```")

    itipos = ["Fact. venta", "NC venta", "Fact. compra", "NC compra"]
    grani = sum(info[(t, e)] for t in itipos for e in ("draft", "cancel"))
    if grani > 0:
        out += ["", "Informativo (contabilidad — NO se envían al SRI):", "```",
                f"{'Tipo':<16}{'Borrador':>9}{'Cancel':>8}"]
        for t in itipos:
            d, c = info[(t, "draft")], info[(t, "cancel")]
            if d or c:
                out.append(f"{t:<16}{d:>9}{c:>8}")
        out.append("```")

    out.append("")
    if gran == 0:
        out.append("✅ Sin pendientes de enviar al SRI.")
    else:
        if sum(pend[(t, "DEVUELTA")] for t in tipos) > 0:
            out.append("⚠️ DEVUELTA puede ser falso positivo (verificar en portal SRI).")
        out.append("¿Querés el listado detallado?")
    if grani > 0:
        out.append("ℹ️ Borradores/cancelados son informativos, no se envían.")

    return "\n".join(out)
