# Candidatos de automatización Odoo 13 → MCP tools

> Documento de análisis para definir qué procesos de Odoo 13 conviene exponer como **tools MCP** para que los agentes IA del worker SaaS los invoquen por chat.
>
> **Patrón de referencia**: `/Users/elmers/Documents/develop/2026/worker/mcp-server-odoo/mcp_odoo/tools/sri.py` — encapsular flujos multi‑paso de Odoo en una sola tool con inputs simples y outputs verificables.
>
> **Generado por**: 3 agentes paralelos explorando Odoo 13 community + módulos custom Ecuador (`working/common`) + módulos de ferretería (`working/ferreteria`).
>
> **Fecha**: 2026‑04‑07
>
> ⚠️ **Excluido por petición del usuario**: cierre de caja, sesiones POS, `collection.session`, operaciones de caja diaria.

---

## Criterios de selección

Para cada candidato se evalúa:

| Criterio | Qué significa |
|---|---|
| **Repetitivo** | El usuario humano lo hace varias veces a la semana o al día |
| **Inputs simples** | Pocos parámetros, idealmente IDs + fechas + flags |
| **Verificable** | El resultado se puede leer en un campo o se devuelve un archivo |
| **Auto‑contenido** | La lógica vive en métodos del modelo, no requiere lógica externa |
| **De negocio** | NO infraestructura/framework — sale, account, stock, sri, hr, mrp |

---

## 🏆 TOP 10 GLOBAL — empezar por estos

| # | Reporte / Proceso | Modelo · Método | Origen | Frecuencia | Por qué primero |
|---|---|---|---|---|---|
| **1** | **Cartera individual de cliente (Excel + email)** | `account.report.partnerledger._get_report_values` | working/common (account_dynamic_reports) | diario | Es el reporte exacto de la imagen del usuario — botones "Descargar a Excel" y "Enviar por correo". El agente puede generarlo y mandarlo por Telegram al cliente |
| **2** | **Cartera de Clientes con deudas (Excel masivo)** | `xls.excel.create_excel_cartera` (es_cartera_clientes=True) | working/common (l10n_ec_export_cartera) | diario | Vista cartera completa para gestión de cobranza, ya tiene wizard listo |
| **3** | **Cartera de Proveedores (Excel masivo)** | `xls.excel.create_excel_cartera` (es_cartera_proveedores=True) | working/common (l10n_ec_export_cartera) | semanal | Mismo wizard que #2 con flag distinto |
| **4** | **Antigüedad de cartera (Aged Partner Balance)** | `ins.partner.ageing.action_xlsx` o nativo `account.aged.partner.balance` | working/common · core | semanal | Clave para estrategia de cobranza por rangos 30/60/90/120 |
| **5** | **Generar XML del Anexo Transaccional (ATS)** | `l10n_ec_sri.tax.form.generar_xml` | working/common (l10n_ec_sri) | mensual | Procesamiento del reporte de declaraciones mensuales SRI (la imagen del usuario) |
| **6** | **Descargar XML del ATS** | `l10n_ec_sri.tax.form.descargar_xml` | working/common (l10n_ec_sri) | mensual | Complemento de #5, agente devuelve el XML para que el contador lo suba al SRI |
| **7** | **Libro Mayor (General Ledger Excel + PDF)** | `ins.general.ledger.action_xlsx` | working/common (account_dynamic_reports) | mensual | Reporte financiero más usado en cierre |
| **8** | **Balance de Comprobación (Trial Balance)** | `ins.trial.balance.action_xlsx` | working/common (account_dynamic_reports) | mensual | Validación de integridad contable previa al cierre |
| **9** | **Estado de Resultados (P&L) y Hoja de Balance** | `ins.financial.report.action_xlsx` | working/common (account_dynamic_reports) | mensual | Reporte ejecutivo, alta visibilidad gerencial |
| **10** | **Detalle de facturación (ventas + compras)** | `invoice.report.export.wizard.get_xlsx_report` | working/common (export_informe_facturas_xls) | diario | Pre‑validación de lo que se va a declarar al SRI |

---

## 📂 Bloque A — Reportes de cartera y estados de cuenta (working/common)

### A.1. Cartera individual de cliente (con descarga Excel + envío email)

> Es el caso de uso #1 que pidió el usuario explícitamente. Reproduce los botones de la captura: "Descargar a Excel", "Enviar por Correo", "Refrescar Pagos".

- **Tipo**: Reporte XLSX + Email HTML
- **Módulo**: `account_dynamic_reports` (working/common)
- **Modelo**: `ins.partner.ledger` (TransientModel)
- **Métodos**:
  - `action_xlsx` → archivo XLSX
  - `action_pdf` → archivo PDF
  - `action_send_email` → envía al partner por correo (existe en variantes)
- **Inputs mínimos**: `partner_id`, `date_from`, `date_to`, `reconciled` (bool, opcional)
- **Output**: archivo binario (xlsx/pdf) o `mail.message` creado
- **Frecuencia**: diaria — los vendedores y cobradores piden esto constantemente
- **Fuente**: `account_dynamic_reports/wizard/partner_ledger.py`

**MCP tool sugerida**:
```python
def cartera_individual_cliente(partner_id: int, date_from: str = None, date_to: str = None, format: str = "xlsx") -> bytes
```

### A.2. Cartera de Clientes con deudas (Excel masivo)
- **Tipo**: Reporte XLSX masivo
- **Módulo**: `l10n_ec_export_cartera`
- **Modelo**: `xls.excel` (TransientModel)
- **Método**: `create_excel_cartera`
- **Inputs mínimos**: filtro (`partner_ids` o domain), context `es_cartera_clientes=True`
- **Output**: XLSX con RUC, Nombre, Cupo, Deuda total, Vencido
- **Frecuencia**: diaria
- **Fuente**: `l10n_ec_export_cartera/models/cartera_js_excel.py:26-119`

### A.3. Cartera de Proveedores (Excel masivo)
- **Tipo**: Reporte XLSX masivo
- **Módulo**: `l10n_ec_export_cartera`
- **Modelo**: `xls.excel`
- **Método**: `create_excel_cartera` con context `es_cartera_proveedores=True`
- **Inputs mínimos**: `partner_ids`
- **Output**: XLSX con obligaciones a pagar
- **Frecuencia**: semanal
- **Fuente**: `l10n_ec_export_cartera/models/cartera_js_excel.py:45-59`

### A.4. Deudas con Pagos a Fecha
- **Tipo**: Reporte XLSX + PDF
- **Módulo**: `account_deuda_pagos_report`
- **Modelo**: `ins.deudas.pagos` (TransientModel)
- **Métodos**: `action_xlsx`, `action_pdf`
- **Inputs mínimos**: `date_from`, `date_to`, `partner_ids`, `type` (`receivable`|`payable`)
- **Output**: XLSX con saldo inicial + movimientos + saldo final
- **Frecuencia**: semanal
- **Fuente**: `account_deuda_pagos_report/wizard/deudas_pagos.py:650-664`

### A.5. Deudas a Fecha (snapshot)
- **Tipo**: Reporte XLSX
- **Módulo**: `account_deuda_pagos_report`
- **Modelo**: `ins.deudas.fecha`
- **Método**: `action_xlsx`
- **Inputs mínimos**: `date_from`, `date_to`, `type`, `partner_category_ids`
- **Output**: XLSX con deudas al cierre del período
- **Frecuencia**: mensual
- **Fuente**: `account_deuda_pagos_report/wizard/deudas_fecha.py`

### A.6. Estado de Cuentas por entidad
- **Tipo**: Reporte XLSX
- **Módulo**: `account_deuda_pagos_report`
- **Modelo**: `ins.estado.cuentas`
- **Método**: `action_xlsx`
- **Inputs mínimos**: `date_from`, `date_to`, `partner_ids`, `include_details` (bool)
- **Output**: XLSX con movimientos y saldo por entidad
- **Frecuencia**: mensual / semanal
- **Fuente**: `account_deuda_pagos_report/wizard/estado_cuentas.py`

---

## 📂 Bloque B — Reportes financieros (working/common)

### B.1. Libro Mayor (General Ledger)
- **Tipo**: XLSX + PDF + HTML dinámico
- **Módulo**: `account_dynamic_reports`
- **Modelo**: `ins.general.ledger` (TransientModel)
- **Métodos**: `action_xlsx`, `action_pdf`, `action_view`
- **Inputs mínimos**: `date_from`, `date_to`, `account_ids` (opcional)
- **Output**: XLSX con movimientos por cuenta y saldos iniciales/finales
- **Frecuencia**: mensual / trimestral
- **Fuente**: `account_dynamic_reports/wizard/general_ledger.py`

### B.2. Balance de Comprobación (Trial Balance)
- **Tipo**: XLSX + PDF
- **Módulo**: `account_dynamic_reports`
- **Modelo**: `ins.trial.balance`
- **Métodos**: `action_xlsx`, `action_pdf`
- **Inputs mínimos**: `date_from`, `date_to`, `display_accounts` (`all`|`balance_not_zero`)
- **Output**: XLSX con Debe/Haber/Saldo por cuenta
- **Frecuencia**: mensual
- **Fuente**: `account_dynamic_reports/wizard/trial_balance.py`

### B.3. Reporte Financiero (P&L / Hoja de Balance)
- **Tipo**: XLSX + PDF + HTML
- **Módulo**: `account_dynamic_reports`
- **Modelo**: `ins.financial.report`
- **Métodos**: `action_xlsx`, `action_pdf`
- **Inputs mínimos**: `date_from`, `date_to`, `compare_period` (opcional)
- **Output**: XLSX con cuentas de resultado
- **Frecuencia**: mensual
- **Fuente**: `account_dynamic_reports/wizard/financial_report.py`

### B.4. Antigüedad de Cartera (Aged Partner Balance)
- **Tipo**: XLSX + PDF
- **Módulo**: `account_dynamic_reports`
- **Modelo**: `ins.partner.ageing`
- **Métodos**: `action_xlsx`, `action_pdf`
- **Inputs mínimos**: `date_to`, `ageing_periods` (30/60/90/120 días)
- **Output**: XLSX con deudas clasificadas por antigüedad
- **Frecuencia**: semanal / quincenal
- **Fuente**: `account_dynamic_reports/wizard/partner_ageing.py`

### B.5. Mayor por Cliente (Partner Ledger)
- **Tipo**: XLSX + PDF
- **Módulo**: `account_dynamic_reports`
- **Modelo**: `ins.partner.ledger`
- **Métodos**: `action_xlsx`, `action_pdf`
- **Inputs mínimos**: `date_from`, `date_to`, `partner_ids`, `reconciled` (bool)
- **Output**: XLSX con detalle de facturas y pagos por cliente
- **Frecuencia**: mensual
- **Fuente**: `account_dynamic_reports/wizard/partner_ledger.py`

---

## 📂 Bloque C — Declaraciones SRI (working/common)

> Estos son los reportes mensuales de la imagen del usuario (Anexo Transaccional Simplificado, Formulario 103, Formulario 104).

### C.1. Generar XML del Anexo Transaccional (ATS)
- **Tipo**: Generación XML
- **Módulo**: `l10n_ec_sri`
- **Modelo**: `l10n_ec_sri.tax.form`
- **Método**: `generar_xml`
- **Inputs mínimos**: `tax_form_id` (id de la declaración del mes)
- **Output**: archivo XML válido para subida al SRI
- **Frecuencia**: mensual
- **Fuente**: `l10n_ec_sri/models/sri_tax_form.py`

### C.2. Descargar XML generado
- **Tipo**: Descarga binaria
- **Módulo**: `l10n_ec_sri`
- **Modelo**: `l10n_ec_sri.tax.form`
- **Método**: campo `xml_file` (binary) o `descargar_xml`
- **Inputs mínimos**: `tax_form_id`
- **Output**: bytes del XML
- **Frecuencia**: mensual
- **Fuente**: `l10n_ec_sri/models/sri_tax_form.py`

### C.3. Detalle de facturas que se van a declarar (validación pre‑ATS)
- **Tipo**: Reporte XLSX
- **Módulo**: `export_informe_facturas_xls`
- **Modelo**: `wizard.informe.sri` (TransientModel)
- **Método**: `get_xlsx_report`
- **Inputs mínimos**: `date_from`, `date_to`, `compras` (bool), `ventas` (bool), `nc_compras` (bool), `nc_ventas` (bool)
- **Output**: XLSX con todas las facturas del período filtrado por tipo
- **Frecuencia**: mensual (antes de declarar)
- **Fuente**: `export_informe_facturas_xls/models/wizard.py:20-87`

### C.4. Detalle por línea de factura (margen y precios)
- **Tipo**: Reporte XLSX
- **Módulo**: `l10n_ec_payment`, `export_informe_facturas_xls`
- **Modelo**: `invoice.report.export.wizard`
- **Método**: `action_export` → `get_xlsx_report`
- **Inputs mínimos**: `date_from`, `date_to`, `report_type` (`sales`|`purchases`), `partner_id`, `salesman_id`
- **Output**: XLSX con línea‑producto‑precio‑margen
- **Frecuencia**: diaria / semanal
- **Fuente**: `l10n_ec_payment/wizard/invoice_report_export_wizard.py:34-58`

### C.5. Kardex de stock (valorización)
- **Tipo**: PDF / HTML / XLSX
- **Módulo**: `stock_kardex_report`
- **Modelo**: `stock.card.report.wizard`
- **Métodos**: `button_export_xlsx`, `button_export_pdf`, `button_export_html`
- **Inputs mínimos**: `date_from`, `date_to`, `location_id`, `product_ids`
- **Output**: XLSX con movimientos y saldo de inventario
- **Frecuencia**: mensual (cierre)
- **Fuente**: `stock_kardex_report/wizard/stock_kardex_report_wizard.py:62-79`

---

## 📂 Bloque D — Reportes nativos de Odoo 13 core

> Útiles cuando el cliente NO tiene los módulos custom de `working/common`.

### D.1. Aged Partner Balance (cartera por edad nativa)
- **Módulo**: `addons/account`
- **Modelo**: `report.account.report_agedpartnerbalance` (AbstractModel)
- **Método**: `_get_report_values`
- **Inputs**: `date_from`, `target_move` (`all`|`posted`), `period_length`, `result_selection` (`customer`|`supplier`|`both`)
- **Output**: PDF
- **Fuente**: `addons/account/report/account_aged_partner_balance.py:245`

### D.2. Mayor por Partner nativo
- **Módulo**: `addons/account`
- **Modelo**: `report.account.report_partnerledger`
- **Método**: heredado de `account.common.partner.report`
- **Inputs**: `date_from`, `date_to`, `target_move`, `partner_ids`
- **Output**: PDF
- **Fuente**: `addons/account/wizard/account_report_common.py:37`

### D.3. Diario contable (Journal Report)
- **Módulo**: `addons/account`
- **Modelo**: `report.account.report_journal` (AbstractModel)
- **Método**: `lines()`, `_sum_debit()`, `_sum_credit()`, `_get_taxes()`
- **Inputs**: `journal_ids`, `date_from`, `date_to`, `target_move`, `sort_selection`
- **Output**: PDF con asientos del diario
- **Fuente**: `addons/account/report/account_journal.py:12`

### D.4. Imprimir Diario (Wizard)
- **Módulo**: `addons/account`
- **Modelo**: `account.print.journal` (TransientModel)
- **Método**: `_print_report`
- **Inputs**: `journal_ids`, `sort_selection`, `date_from`, `date_to`, `target_move`
- **Output**: PDF action
- **Fuente**: `addons/account/wizard/account_report_print_journal.py:14`

### D.5. Análisis de ventas (sale.report)
- **Módulo**: `addons/sale`
- **Modelo**: `sale.report` (vista SQL `_auto=False`)
- **Inputs**: `date_from`, `date_to`, `product_id`, `partner_id`, `team_id`, `state`
- **Output**: dataset tabular para BI
- **Fuente**: `addons/sale/report/sale_report.py:8`

### D.6. Análisis de compras (purchase.report)
- **Módulo**: `addons/purchase`
- **Modelo**: `purchase.report` (vista SQL)
- **Inputs**: `date_from`, `date_to`, `partner_id`, `category_id`, `delay`
- **Output**: dataset tabular
- **Fuente**: `addons/purchase/report/purchase_report.py:11`

### D.7. Análisis de facturas (invoice.report)
- **Módulo**: `addons/account`
- **Modelo**: `account.invoice.report` (vista SQL)
- **Campos**: `invoice_date`, `partner_id`, `invoice_user_id`, `amount_total`, `residual`
- **Output**: dataset tabular
- **Fuente**: `addons/account/report/account_invoice_report.py:9`

### D.8. Reporte de cantidad de stock (forecast)
- **Módulo**: `addons/stock`
- **Modelo**: `report.stock.quantity` (vista SQL)
- **Estados**: `forecast`, `in`, `out`
- **Inputs**: `product_id`, `warehouse_id`, `date`, `state`
- **Output**: pronóstico de stock
- **Fuente**: `addons/stock/report/report_stock_quantity.py:7`

### D.9. Reporte de reglas de stock
- **Módulo**: `addons/stock`
- **Modelo**: `stock.rules.report` (TransientModel)
- **Método**: `print_report`
- **Inputs**: `product_id`, `warehouse_ids`
- **Output**: PDF con rutas de reabastecimiento
- **Fuente**: `addons/stock/wizard/stock_rules_report.py:46`

### D.10. Resumen de vacaciones (HR Holidays Summary)
- **Módulo**: `addons/hr_holidays`
- **Modelo**: `report.hr_holidays.report_holidayssummary` (AbstractModel)
- **Inputs**: `date_from`, `emp` (Many2many), `holiday_type` (`Approved`|`Confirmed`|`both`)
- **Output**: PDF matriz 60 días × empleados
- **Fuente**: `addons/hr_holidays/report/holidays_summary_report.py:111`

### D.11. Estructura de BOM
- **Módulo**: `addons/mrp`
- **Modelo**: `report.mrp.report_bom_structure`
- **Método**: `_get_report_values`
- **Inputs**: `bom_id`, `quantity`, `report_type`
- **Output**: PDF jerárquico
- **Fuente**: `addons/mrp/report/mrp_report_bom_structure.py:12`

### D.12. Lista de precios
- **Módulo**: `addons/product`
- **Modelo**: `product.pricelist`
- **Método**: `print_report` en wizard `product.price.list`
- **Inputs**: `pricelist_id`, `qty1..qty5`
- **Output**: PDF con precios escalonados
- **Fuente**: `addons/product/wizard/product_price_list.py:19`

### D.13. Exportación FEC (Francia, ejemplo de CSV regulatorio)
- **Módulo**: `addons/l10n_fr_fec`
- **Modelo**: `account.fr.fec` (TransientModel)
- **Método**: `generate_fec`
- **Inputs**: `date_from`, `date_to`, `export_type`
- **Output**: CSV base64 (patrón aplicable a futuras exportaciones SRI Ecuador)
- **Fuente**: `addons/l10n_fr_fec/wizard/account_fr_fec.py:106`

---

## 🎯 Plan de implementación recomendado

### Fase 1 — Cartera (semana 1)

Es el caso de uso #1 que el usuario pidió explícitamente.

```python
# mcp-server-odoo/mcp_odoo/tools/cartera.py
def cartera_cliente(partner_id, date_to=None) -> dict
def cartera_individual_xlsx(partner_id, date_from, date_to) -> bytes
def cartera_individual_email(partner_id, date_from, date_to) -> dict
def cartera_clientes_masiva() -> bytes
def cartera_proveedores_masiva() -> bytes
```

→ El agente Niko podrá: "envíame mi estado de cuenta", "envíale a Juan Pérez su cartera por correo", "dame la cartera total de clientes en Excel".

### Fase 2 — Reportes financieros (semana 2)

```python
# mcp-server-odoo/mcp_odoo/tools/financial.py
def libro_mayor(date_from, date_to, account_ids=None) -> bytes
def balance_comprobacion(date_from, date_to) -> bytes
def estado_resultados(date_from, date_to) -> bytes
def hoja_balance(date_to) -> bytes
def antiguedad_cartera(date_to, periods=[30,60,90,120]) -> bytes
```

→ El gerente puede pedir el cierre mensual por chat: "dame el P&L de marzo".

### Fase 3 — SRI mensual (semana 3)

```python
# mcp-server-odoo/mcp_odoo/tools/sri_declaraciones.py
def listar_declaraciones_pendientes() -> list[dict]
def crear_declaracion_mensual(year, month) -> int  # tax_form_id
def generar_xml_ats(tax_form_id) -> dict
def descargar_xml_ats(tax_form_id) -> bytes
def detalle_facturas_periodo(date_from, date_to, tipos=[...]) -> bytes
```

→ El contador puede pedir: "genera el XML del ATS de marzo y mándamelo".

### Fase 4 — Operacional (semana 4)

```python
# mcp-server-odoo/mcp_odoo/tools/operacional.py
def kardex_producto(product_id, date_from, date_to, location_id) -> bytes
def stock_a_reordenar(warehouse_id) -> list[dict]
def ventas_por_vendedor(date_from, date_to) -> bytes
def detalle_facturacion(date_from, date_to, type='sales') -> bytes
```

---

## 🔧 Patrón de implementación de cada tool

Siguiendo el patrón de `sri.py`:

```python
@mcp.tool()
async def cartera_individual_cliente(
    tenant_id: str,
    partner_id: int,
    date_from: str = None,
    date_to: str = None,
    format: str = "xlsx",
) -> dict:
    """Genera el estado de cuenta de un cliente.

    Returns:
        {"file_b64": "...", "filename": "cartera_X.xlsx", "size": 12345}
    """
    # 1. Validaciones
    if format not in ("xlsx", "pdf"):
        return {"error": "format debe ser 'xlsx' o 'pdf'"}

    # 2. Crear el wizard
    wizard_id = odoo_create("ins.partner.ledger", {
        "partner_ids": [(6, 0, [partner_id])],
        "date_from": date_from,
        "date_to": date_to,
    })

    # 3. Llamar el método de exportación
    method = "action_xlsx" if format == "xlsx" else "action_pdf"
    result = odoo_call_method("ins.partner.ledger", method, [wizard_id])

    # 4. Verificar y devolver
    file_data = odoo_read("ins.partner.ledger", wizard_id, ["xlsx_file", "filename"])
    return {
        "file_b64": file_data["xlsx_file"],
        "filename": file_data["filename"],
    }
```

**Reglas críticas** (heredadas de `sri.py`):

1. **Validar inputs antes de gastar XML‑RPC**
2. **Devolver siempre un dict** con `error` o el resultado
3. **Leer el resultado** después de crear el wizard (los métodos retornan `None` por XML‑RPC)
4. **NO reescribir lógica** — siempre invocar métodos existentes del módulo Odoo
5. **Documentar el método de Odoo** con su ruta y línea exacta para futuro debug

---

## 📊 Estadísticas del análisis

| Origen | Candidatos | Más fuertes |
|---|---|---|
| `working/common` (Ecuador) | 13 | Cartera individual, Libro Mayor, ATS XML, Trial Balance |
| `addons/` (Odoo core) | 13 | Aged Partner Balance, Sale Report, Stock Quantity |
| `working/ferreteria` | 5 | Etiquetas Zebra, Cambio masivo de precios |
| **TOTAL** | **31** candidatos analizados |

---

## 🚫 Procesos descartados (por petición del usuario)

- `collection.session.action_payec_session_close` (cierre de caja)
- `collection.session.action_registra_efectivo`
- `collection.session.action_registra_fondo_caja`
- Toda la familia POS / sesiones diarias

---

**Generado**: 2026‑04‑07 · **Patrón base**: `mcp-server-odoo/mcp_odoo/tools/sri.py`
