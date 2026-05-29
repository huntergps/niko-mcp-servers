"""PDF rendering for customer statements (estado de cuenta).

Velneo's REST API does not expose the desktop print engine, so the
account statement that Theos prints from its UI is reproduced here
in Python via reportlab. The layout matches the print sample the
owner shared on 2026-05-28: a centered title, the customer header
block, the table of debts (FECHA, VENCIMIENTO, DETALLE/REF INTERNA,
TOTAL, PAGADO, SALDO), and a TOTAL row.

The renderer is intentionally small — no logos, no per-tenant
templates. The brand string passed in is used in the title; anything
fancier can grow later without changing call sites.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _money(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def render_statement_pdf(data: dict[str, Any], *, brand: str = "") -> bytes:
    """Return a Letter-sized statement PDF as bytes.

    ``data`` is the dict produced by
    :func:`mcp_theos.tools.invoices.get_customer_statement`.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Estado de cuenta",
        author=brand or "Theos",
    )
    styles = getSampleStyleSheet()
    base = styles["BodyText"]
    title_style = ParagraphStyle(
        "title", parent=base, alignment=1, fontSize=14,
        leading=18, spaceAfter=6,
    )
    label_style = ParagraphStyle(
        "label", parent=base, fontName="Helvetica-Bold",
        fontSize=9, leading=11,
    )
    value_style = ParagraphStyle(
        "value", parent=base, fontSize=9, leading=11,
    )
    right_style = ParagraphStyle(
        "right", parent=base, fontSize=9, leading=11, alignment=2,
    )

    elements: list = []
    safe_brand = (brand or "").strip()
    title_text = (
        f"* * * * *  DEUDAS DE CLIENTES — {safe_brand}  * * * * *"
        if safe_brand
        else "* * * * *  DEUDAS DE CLIENTES  * * * * *"
    )
    elements.append(Paragraph(title_text, title_style))

    # ---- header block (NOMBRE / RUC / DIRECCION / FECHA DE CORTE) ----
    partner = data.get("partner") or {}
    cutoff = data.get("cutoff_date") or ""
    header_rows = [
        [Paragraph("NOMBRE:", label_style), Paragraph(partner.get("name") or "—", value_style),
         Paragraph("FECHA DE CORTE:", label_style), Paragraph(cutoff or "—", value_style)],
        [Paragraph("RUC:", label_style), Paragraph(partner.get("cif") or "—", value_style),
         Paragraph("CLIENTE ID:", label_style), Paragraph(str(partner.get("id") or "—"), value_style)],
        [Paragraph("DIRECCION:", label_style), Paragraph(partner.get("address") or "—", value_style),
         Paragraph("EMAIL:", label_style), Paragraph(partner.get("email") or "—", value_style)],
    ]
    header_tbl = Table(
        header_rows,
        colWidths=[26 * mm, 75 * mm, 32 * mm, 50 * mm],
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 4 * mm))

    # ---- debts table ----
    items = data.get("items") or []
    rows: list[list[Any]] = [[
        Paragraph("FECHA", label_style),
        Paragraph("VENCIMIENTO", label_style),
        Paragraph("REFERENCIA", label_style),
        Paragraph("DIAS", label_style),
        Paragraph("TOTAL", right_style),
        Paragraph("PAGADO", right_style),
        Paragraph("SALDO", right_style),
    ]]
    for it in items:
        rows.append([
            Paragraph(it.get("fecha") or "", value_style),
            Paragraph(it.get("vencimiento") or "", value_style),
            Paragraph(it.get("referencia") or "", value_style),
            Paragraph(str(it.get("dias")) if it.get("dias") is not None else "", right_style),
            Paragraph(_money(it.get("total_deuda")), right_style),
            Paragraph(_money(it.get("pagado")), right_style),
            Paragraph(_money(it.get("saldo")), right_style),
        ])

    totals = data.get("totals") or {}
    rows.append([
        "", "", "", Paragraph("TOTAL:", label_style),
        Paragraph(_money(totals.get("total_deuda")), right_style),
        Paragraph(_money(totals.get("pagado")), right_style),
        Paragraph(_money(totals.get("saldo")), right_style),
    ])

    tbl = Table(
        rows,
        colWidths=[22 * mm, 26 * mm, 38 * mm, 14 * mm, 24 * mm, 24 * mm, 24 * mm],
        repeatRows=1,
    )
    tbl.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 0.4, colors.black),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.whitesmoke, colors.white]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(tbl)

    # ---- footer ----
    elements.append(Spacer(1, 6 * mm))
    footer_style = ParagraphStyle(
        "footer", parent=base, fontSize=8, alignment=0, textColor=colors.grey,
    )
    elements.append(Paragraph(
        f"Impreso por {safe_brand or 'Theos'} · "
        f"{datetime.now().strftime('%d-%b-%Y %H:%M')} · "
        f"{len(items)} ítems",
        footer_style,
    ))

    doc.build(elements)
    return buf.getvalue()


def render_quotation_pdf(data: dict[str, Any], *, brand: str = "") -> bytes:
    """Render a sales-order / quotation PDF (proforma).

    ``data`` is the dict returned by
    :func:`mcp_theos.tools.sales.get_quotation` — i.e.
    ``{order: {...}, lines: [{...}], line_count: N}``. We don't try to
    reproduce Velneo's full ticket layout (logos, fiscal blocks,
    barcodes); the goal is a clean attachment the customer can keep
    as a record of what was quoted, with enough detail for the
    cashier to find the order by NRO and finalise it.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Proforma",
        author=brand or "Theos",
    )
    styles = getSampleStyleSheet()
    base = styles["BodyText"]
    title_style = ParagraphStyle(
        "title", parent=base, alignment=1, fontSize=14,
        leading=18, spaceAfter=6,
    )
    label_style = ParagraphStyle(
        "label", parent=base, fontName="Helvetica-Bold",
        fontSize=9, leading=11,
    )
    value_style = ParagraphStyle(
        "value", parent=base, fontSize=9, leading=11,
    )
    right_style = ParagraphStyle(
        "right", parent=base, fontSize=9, leading=11, alignment=2,
    )
    cell_style = ParagraphStyle(
        "cell", parent=base, fontSize=8, leading=10,
    )
    cell_right = ParagraphStyle(
        "cellr", parent=base, fontSize=8, leading=10, alignment=2,
    )

    order = data.get("order") or {}
    lines = data.get("lines") or []
    safe_brand = (brand or "").strip()

    elements: list = []
    title_text = (
        f"* * * * *  PROFORMA — {safe_brand}  * * * * *"
        if safe_brand
        else "* * * * *  PROFORMA  * * * * *"
    )
    elements.append(Paragraph(title_text, title_style))

    # ---- header block ----
    fecha = (order.get("FECHA") or "")[:10]
    header_rows = [
        [Paragraph("NRO:", label_style),
         Paragraph(str(order.get("ID") or "—"), value_style),
         Paragraph("FECHA:", label_style),
         Paragraph(fecha or "—", value_style)],
        [Paragraph("CLIENTE:", label_style),
         Paragraph(order.get("NAME") or "—", value_style),
         Paragraph("ID CLIENTE:", label_style),
         Paragraph(str(order.get("ENT_ERP_CLI") or "—"), value_style)],
        [Paragraph("EMPRESA:", label_style),
         Paragraph(str(order.get("EMP") or "—"), value_style),
         Paragraph("SUCURSAL:", label_style),
         Paragraph(str(order.get("SUC") or "—"), value_style)],
        [Paragraph("BODEGA:", label_style),
         Paragraph(str(order.get("INV_BODEGA") or "—"), value_style),
         Paragraph("TARIFA:", label_style),
         Paragraph(str(order.get("INV_TARIFAS") or "—"), value_style)],
    ]
    header_tbl = Table(
        header_rows,
        colWidths=[26 * mm, 75 * mm, 32 * mm, 50 * mm],
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 4 * mm))

    # ---- lines table ----
    rows: list[list[Any]] = [[
        Paragraph("#", label_style),
        Paragraph("CÓDIGO", label_style),
        Paragraph("DESCRIPCIÓN", label_style),
        Paragraph("CANT", right_style),
        Paragraph("P. UNIT", right_style),
        Paragraph("DSCTO%", right_style),
        Paragraph("TOTAL", right_style),
    ]]
    subtotal = 0.0
    for ln in lines:
        try:
            total_line = float(ln.get("PVP_LINEA") or 0)
        except (TypeError, ValueError):
            total_line = 0.0
        subtotal += total_line
        rows.append([
            Paragraph(str(ln.get("NUM_LINEA") or ""), cell_style),
            Paragraph(ln.get("COD_BAR") or ln.get("INV_PRESENT_PRODUCTO") or "", cell_style),
            Paragraph(ln.get("NOMBRE") or ln.get("NAME") or "", cell_style),
            Paragraph(_money(ln.get("CAN")), cell_right),
            Paragraph(_money(ln.get("PVP")), cell_right),
            Paragraph(_money(ln.get("PORCENTAJE_DSCTO_VTA")), cell_right),
            Paragraph(_money(total_line), cell_right),
        ])

    # totals row
    rows.append([
        "", "", "",
        "", "",
        Paragraph("TOTAL:", label_style),
        Paragraph(
            _money(order.get("TOTAL") if order.get("TOTAL") not in (None, "", "0") else subtotal),
            right_style,
        ),
    ])

    tbl = Table(
        rows,
        colWidths=[10 * mm, 28 * mm, 70 * mm, 14 * mm, 22 * mm, 18 * mm, 22 * mm],
        repeatRows=1,
    )
    tbl.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 0.4, colors.black),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.whitesmoke, colors.white]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(tbl)

    # ---- footer ----
    elements.append(Spacer(1, 6 * mm))
    footer_style = ParagraphStyle(
        "footer", parent=base, fontSize=8, alignment=0, textColor=colors.grey,
    )
    fpago = order.get("CAJ_FORM_PAGO1")
    note = order.get("NAME") or ""
    elements.append(Paragraph(
        f"Forma de pago: {fpago or '—'} · "
        f"{note} · "
        f"Impreso por {safe_brand or 'Theos'} · "
        f"{datetime.now().strftime('%d-%b-%Y %H:%M')} · "
        f"{len(lines)} líneas",
        footer_style,
    ))

    doc.build(elements)
    return buf.getvalue()
