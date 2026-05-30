"""Inventory tools for Mepriga (Velneo / Theos).

Two tools:

* ``generate_negative_stock_report`` — uses the Velneo process
  ``BUSCAR_PRODUCTOS_SIN_EXISTENCIAS`` (filters products with EXS<0 on
  the server) + plural ``EXISTENCIAS_INV_PRODUCTOS`` to pivot by
  warehouse + ``INV_BODEGA`` for column headers. Outputs an XLSX with
  the same layout as the operator's ``PRODUCTOS CON SALDO NEGATIVO.xlsx``.

* ``inventory_movements_window`` — wraps the new
  ``INV_DOC_MOV_BUSQ_JS`` JS process (the multi-tipo doc movements
  search) so Lila can ask for movements by tipo (V/W/C/D), date range,
  optional product/bodega/sucursal.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

from mcp_theos.velneo_http import VelneoClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — fetch paginated lists from Velneo
# ---------------------------------------------------------------------------

async def _fetch_all(
    client: VelneoClient,
    table: str,
    *,
    filters: dict[str, Any] | None = None,
    fields: list[str] | None = None,
    page_size: int = 500,
    max_pages: int = 200,
) -> list[dict[str, Any]]:
    """Walk every page of a table with optional filters / fields."""
    out: list[dict[str, Any]] = []
    params: dict[str, Any] = {"page[size]": page_size}
    if fields:
        params["fields"] = ",".join(fields)
    for k, v in (filters or {}).items():
        params[f"filter[{k}]"] = v
    for page in range(1, max_pages + 1):
        params["page[number]"] = page
        resp = await client._client.get(table, params=params)  # noqa: SLF001
        resp.raise_for_status()
        data = resp.json()
        rows = data.get(table.lower()) or data.get(table) or []
        if not isinstance(rows, list):
            rows = [rows]
        out.extend(rows)
        if len(rows) < page_size:
            break
    return out


async def _fetch_plural(
    client: VelneoClient,
    table: str,
    record_id: int,
    plural_name: str,
    *,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """GET /TABLE/{id}/PLURAL_NAME — fetch a plural relation."""
    params: dict[str, Any] = {"page[size]": 500}
    if fields:
        params["fields"] = ",".join(fields)
    resp = await client._client.get(  # noqa: SLF001
        f"{table}/{record_id}/{plural_name}",
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()
    # The envelope key matches the lowercased TARGET table, not the parent.
    # We pick the first list-valued key we find.
    for key, val in data.items():
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            return [val]
    return []


# ---------------------------------------------------------------------------
# generate_negative_stock_report
# ---------------------------------------------------------------------------

async def generate_negative_stock_report(
    client: VelneoClient,
    *,
    deliver_to_chat: str | None = None,
    include_zero_total_with_negative_bodega: bool = False,
) -> dict[str, Any]:
    """Build the "PRODUCTOS CON SALDO NEGATIVO" XLSX and upload it.

    Steps:
      1. Call Velneo process ``BUSCAR_PRODUCTOS_SIN_EXISTENCIAS`` →
         returns the PRODUCTOS rows where ``#EXS<0`` (consolidated
         negative). No params needed.
      2. For each product, GET the EXISTENCIAS plural to obtain the
         per-bodega breakdown.
      3. GET INV_BODEGA for the column header names (M..V).
      4. Build the XLSX in write_only mode with corporate palette,
         AutoFilter, negative cells in red, frozen header.
      5. Send to Telegram chat via Bot API sendDocument.

    The list size is typically small (~50-500 products) — runs sync,
    no background task needed.

    ``include_zero_total_with_negative_bodega``: if True, ALSO include
    products whose total = 0 but at least one bodega has negative
    stock (over-sold in one location but compensated globally). The
    Velneo process only filters by total <0, so this requires an
    additional pass.
    """
    if not deliver_to_chat:
        return {"success": False,
                "error": "deliver_to_chat is required"}

    # 1) Process: products with negative consolidated EXS
    try:
        proc_resp = await client.process("BUSCAR_PRODUCTOS_SIN_EXISTENCIAS")
    except Exception as exc:  # noqa: BLE001
        return {"success": False,
                "error": f"BUSCAR_PRODUCTOS_SIN_EXISTENCIAS failed: {exc}"}

    products = proc_resp.get("productos") or proc_resp.get("PRODUCTOS") or []
    if not isinstance(products, list):
        products = [products] if products else []
    if not products:
        return {"success": True,
                "delivered": False,
                "note": "No hay productos con saldo negativo."}

    # 2) Bodega names — fetch from INV_BODEGA WITHOUT fields filter
    # (verified working: returns 20 bodegas with full data including
    # nombre_corto which makes nicer column headers than the long NAME).
    try:
        bodegas = await _fetch_all(client, "INV_BODEGA", page_size=200)
    except Exception as exc:  # noqa: BLE001
        bodegas = []
        logger.warning("INV_BODEGA fetch failed: %s — using IDs", exc)
    bodega_name_by_id: dict[int, str] = {}
    for b in bodegas:
        if not isinstance(b, dict):
            continue
        try:
            bid = int(b.get("id") or 0)
        except (TypeError, ValueError):
            bid = 0
        if not bid:
            continue
        # Prefer nombre_corto (e.g. "COMERCIAL", "VIVERES") over the
        # full name ("ALMACEN 01 - COMERCIAL"). Fall back to NAME.
        nombre = ((b.get("nombre_corto") or "").strip()
                  or (b.get("name") or "").strip()
                  or f"BOD {bid}")
        bodega_name_by_id[bid] = nombre.upper()

    # 3) Build rows. Use only the consolidated EXS field from the
    # product. The per-bodega virtual fields (EXS_BOD1..12) return '0'
    # in the JSON because they're computed by Velneo at the client
    # layer, not by REST. Per-bodega breakdown requires direct GET on
    # the EXISTENCIAS table, which currently returns empty under the
    # niko_saas API key (not allow-listed). When the operator adds
    # EXISTENCIAS to the API key (Seguridad → API key → Tablas), this
    # code will pick up the breakdown automatically via fetch_existencias.
    rows: list[dict[str, Any]] = []
    bodega_ids_used: set[int] = set()
    for p in products:
        if not isinstance(p, dict):
            continue
        pid = int(p.get("id") or 0)
        if not pid:
            continue

        # Bodega IDs linked to this product (slots 1..12)
        product_bodega_ids: list[int] = []
        for i in range(1, 13):
            bid = p.get(f"inv_bodega{i}") or 0
            try:
                bid = int(bid)
            except (TypeError, ValueError):
                bid = 0
            if bid > 0:
                product_bodega_ids.append(bid)
                bodega_ids_used.add(bid)

        # Try to get per-bodega breakdown from EXISTENCIAS (returns 0
        # rows today; will work once API key permits it).
        by_bod: dict[int, float] = {}
        try:
            exs_rows = await _fetch_all(
                client, "EXISTENCIAS",
                filters={"INV_PRODUCTOS": pid},
                page_size=100,
                max_pages=2,
            )
            for r in exs_rows:
                if not isinstance(r, dict):
                    continue
                try:
                    bid = int(r.get("inv_bodegas") or 0)
                    exs = float(r.get("exs") or 0)
                except (TypeError, ValueError):
                    continue
                if bid:
                    by_bod[bid] = by_bod.get(bid, 0) + exs
        except Exception as exc:  # noqa: BLE001
            logger.warning("EXISTENCIAS fetch failed for product %s: %s",
                           pid, exc)

        # Total existence: prefer the consolidated EXS from the product
        # (it's the field the process filters on) but fall back to the
        # sum of per-bodega rows if EXISTENCIAS works.
        try:
            total = float(p.get("exs") or 0)
        except (TypeError, ValueError):
            total = 0.0
        if total == 0 and by_bod:
            total = sum(by_bod.values())

        if include_zero_total_with_negative_bodega:
            keep = total < 0 or any(v < 0 for v in by_bod.values())
        else:
            keep = total < 0
        if not keep:
            continue

        rows.append({
            "id": pid,
            "familia": (p.get("inv_fami") or "").strip(),
            "codigo": (p.get("codigo") or "").strip(),
            "nombre": (p.get("name") or "").strip(),
            "costo_promedio": float(p.get("costo_promedio") or 0),
            "costo_compra": float(p.get("costo_compra") or 0),
            "pvp_sin_iva": float(p.get("pvp_minimo") or 0),
            "porc_utilidad": float(p.get("tasautilidadreco") or 0),
            "unidad_minima": (p.get("emp_minimo") or "").strip(),
            "iva_compras": int(p.get("imp_fis_impuestos_compra") or 0),
            "iva_ventas": int(p.get("imp_fis_impuestos_vta") or 0),
            "existencia": total,
            "por_bodega": by_bod,
            "product_bodega_ids": product_bodega_ids,
        })

    # Bodega column order: stable, sorted by ID, restricted to those
    # actually linked to at least one product in the report.
    bodega_order = sorted(bodega_ids_used)

    if not rows:
        return {"success": True,
                "delivered": False,
                "note": "Tras revisar el detalle por bodega, no hay productos con saldo negativo neto."}

    # 4) Build XLSX in write_only mode
    xlsx_bytes = _build_negative_stock_xlsx(rows, bodega_order, bodega_name_by_id)

    # 5) Send to Telegram
    from mcp_theos.telegram_delivery import (
        send_document as _send_doc, BotTokenMissing,
    )
    ec_now = datetime.now(timezone.utc) - timedelta(hours=5)
    filename = f"productos_saldo_negativo_{ec_now.strftime('%Y-%m-%d_%H%M')}.xlsx"
    caption = (
        f"<b>Productos con saldo negativo</b>\n"
        f"Total: {len(rows)} productos · "
        f"Al {ec_now.strftime('%d/%m/%Y %H:%M')} ECU"
    )
    try:
        await _send_doc(
            chat_id=str(deliver_to_chat),
            data=xlsx_bytes,
            filename=filename,
            caption=caption,
            parse_mode="HTML",
        )
    except BotTokenMissing as e:
        return {"success": False, "error": "telegram_bot_token_missing",
                "message": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": "telegram_upload_failed",
                "message": str(e)}

    return {
        "success": True,
        "delivered": True,
        "delivered_to_chat": str(deliver_to_chat),
        "n_productos": len(rows),
        "xlsx_size_kb": round(len(xlsx_bytes) / 1024, 1),
        "filename": filename,
    }


def _build_negative_stock_xlsx(
    rows: list[dict[str, Any]],
    bodega_order: list[int],
    bodega_name_by_id: dict[int, str],
) -> bytes:
    """Render the XLSX with corporate palette + AutoFilter + frozen header."""
    from openpyxl import Workbook
    from openpyxl.styles import (
        Font, PatternFill, Alignment, Border, Side, NamedStyle
    )

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="Saldos Negativos")
    ws.freeze_panes = "A6"

    # Corporate palette (same as sales report)
    PRIMARY = "1F4E78"
    HEADER_FILL = PatternFill("solid", fgColor=PRIMARY)
    HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
    TITLE_FONT = Font(bold=True, color=PRIMARY, size=14)
    SUBTITLE_FONT = Font(bold=True, color="404040", size=11)
    NEG_FONT = Font(color="C00000", bold=True)
    BODY_FONT = Font(size=10)
    CENTER = Alignment(horizontal="center", vertical="center")
    LEFT = Alignment(horizontal="left", vertical="center", wrap_text=False)
    RIGHT = Alignment(horizontal="right", vertical="center")
    thin = Side(border_style="thin", color="BFBFBF")
    BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)

    from openpyxl.cell import WriteOnlyCell as _C

    # Row 1: empty
    ws.append([])

    # Row 2: company
    c = _C(ws, value="MEGA PRIMAVERA GALÁPAGOS S.A.")
    c.font = TITLE_FONT
    c.alignment = LEFT
    ws.append([c])

    # Row 3: subtitle
    ec_now = datetime.now(timezone.utc) - timedelta(hours=5)
    fecha_str = ec_now.strftime("%d/%m/%Y %H:%M")
    c = _C(ws, value=f"DETALLE DE PRODUCTOS CON SALDOS EN NEGATIVO AL {fecha_str}")
    c.font = SUBTITLE_FONT
    c.alignment = LEFT
    ws.append([c])

    # Row 4: empty
    ws.append([])

    # Row 5: headers
    fixed_headers = [
        "ID", "Familia", "Código", "Nombre", "Promedio", "Ult.Compra",
        "PVP sin IVA", "% Utilidad", "Unidad Minima",
        "IVA en Compras", "IVA en Ventas", "Existencia",
    ]
    bodega_headers = [
        bodega_name_by_id.get(bid, f"BOD {bid}").upper() for bid in bodega_order
    ]
    all_headers = fixed_headers + bodega_headers

    header_row = []
    for h in all_headers:
        c = _C(ws, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = CENTER
        c.border = BORDER
        header_row.append(c)
    ws.append(header_row)

    # Rows 6+: data
    for r in rows:
        cells = [
            r["id"],
            r["familia"],
            r["codigo"],
            r["nombre"],
            round(r["costo_promedio"], 4),
            round(r["costo_compra"], 4),
            round(r["pvp_sin_iva"], 4),
            round(r["porc_utilidad"], 4),
            r["unidad_minima"],
            r["iva_compras"],
            r["iva_ventas"],
            round(r["existencia"], 2),
        ]
        for bid in bodega_order:
            cells.append(round(r["por_bodega"].get(bid, 0), 2))

        wo_cells = []
        for i, val in enumerate(cells):
            c = _C(ws, value=val)
            c.font = BODY_FONT
            c.alignment = LEFT if i in (1, 2, 3, 8) else RIGHT
            c.border = BORDER
            if isinstance(val, (int, float)) and val < 0:
                c.font = NEG_FONT
            wo_cells.append(c)
        ws.append(wo_cells)

    # Column widths
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 50
    ws.column_dimensions["E"].width = 11
    ws.column_dimensions["F"].width = 11
    ws.column_dimensions["G"].width = 11
    ws.column_dimensions["H"].width = 11
    ws.column_dimensions["I"].width = 14
    ws.column_dimensions["J"].width = 11
    ws.column_dimensions["K"].width = 11
    ws.column_dimensions["L"].width = 11
    # Bodega columns
    from openpyxl.utils import get_column_letter
    for i, _ in enumerate(bodega_order, start=13):
        ws.column_dimensions[get_column_letter(i)].width = 13

    # AutoFilter
    last_col = len(all_headers)
    last_row = 5 + len(rows)
    last_col_letter = get_column_letter(last_col)
    ws.auto_filter.ref = f"A5:{last_col_letter}{last_row}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# inventory_movements_window — wraps INV_DOC_MOV_BUSQ_JS
# ---------------------------------------------------------------------------

async def inventory_movements_window(
    client: VelneoClient,
    *,
    date_from: str,
    date_to: str,
    tipo_doc: str = "",
    sucursal: str | None = None,
    producto: int = 0,
    bodega: int = 0,
    off: int = 0,
    limit: int = 1000,
) -> dict[str, Any]:
    """Call the INV_DOC_MOV_BUSQ_JS process (multi-tipo doc movements).

    ``tipo_doc``: "V"|"W"|"C"|"D" or "" for all 4 types.
    Returns the list of INV_MOVIMIENTOS rows aggregated.
    """
    params: dict[str, Any] = {
        "FCH_DES": date_from,
        "FCH_HST": date_to,
        "TIPO_DOC": tipo_doc or "",
        "OFF": off,
        "PRODUCTO": producto,
        "BODEGA": bodega,
    }
    if sucursal:
        params["SUCURSAL"] = sucursal

    try:
        resp = await client.process("INV_DOC_MOV_BUSQ_JS", params)
    except Exception as exc:  # noqa: BLE001
        return {"success": False,
                "error": f"INV_DOC_MOV_BUSQ_JS failed: {exc}"}

    rows = resp.get("inv_movimientos") or resp.get("INV_MOVIMIENTOS") or []
    if not isinstance(rows, list):
        rows = [rows] if rows else []

    # Truncate response to LIMIT to avoid blowing up the LLM context
    truncated = len(rows) > limit
    head = rows[:limit]

    return {
        "success": True,
        "tipo_doc": tipo_doc or "(todos)",
        "date_from": date_from,
        "date_to": date_to,
        "n_movimientos": len(rows),
        "truncated": truncated,
        "returned": len(head),
        "rows": head,
    }
