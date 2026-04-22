"""SRI Ecuador electronic document import tools.

These tools invoke the existing l10n_ec_sri_ece module methods via XML-RPC.
The business logic is ENTIRELY in the Odoo custom module — we only call existing methods.

Key insight (verified in source code):
- create() auto-calls get_doc() (line 124)
- If tipo_homologacion='auto' and all products map, homologar() auto-calls action_order_create() (line 1005)
- If state_purchase_orders='done', action_order_create() auto-confirms + generates invoice (line 1302-1307)
- So a single create() can execute the ENTIRE flow.

Pattern: always read() after create() to check the result (methods return None via XML-RPC).
"""

from mcp_odoo.tools.generic import odoo_create, odoo_read, odoo_search, odoo_write, odoo_call_method


def validate_sri_access_key(key: str) -> tuple[bool, str]:
    """Validate SRI access key format and modulo-11 checksum.

    The access key is 49 digits with a check digit at position 49.
    """
    if not key or not key.isdigit():
        return False, "La clave de acceso debe contener solo digitos"

    if len(key) != 49:
        return False, f"La clave de acceso debe tener 49 digitos, tiene {len(key)}"

    # Modulo 11 checksum validation (standard SRI Ecuador)
    coefficients = [2, 3, 4, 5, 6, 7]
    check_digit = int(key[48])
    digits = [int(d) for d in key[:48]]

    total = 0
    for i, digit in enumerate(reversed(digits)):
        total += digit * coefficients[i % len(coefficients)]

    remainder = total % 11
    expected = 11 - remainder
    if expected == 11:
        expected = 0
    elif expected == 10:
        expected = 1

    if check_digit != expected:
        return False, f"Digito verificador invalido: esperado {expected}, recibido {check_digit}"

    return True, "OK"


def sri_import_create(
    tenant_id: str, url: str, db: str, user: str, password: str,
    access_key: str,
    tipo_importacion: str = "sri",
    tipo_homologacion: str = "auto",
    state_purchase_orders: str = "done",
    ambient_id: int | None = None,
) -> dict:
    """Create an SRI electronic document import record.

    With tipo_homologacion='auto' and state_purchase_orders='done',
    a single create() executes the ENTIRE flow:
    create → get_doc (SRI SOAP) → parse XML → homologate → create PO → confirm → invoice

    Returns the import record state after execution (via read-after-write).
    """
    valid, message = validate_sri_access_key(access_key)
    if not valid:
        return {"success": False, "error": message}

    values = {
        "name": access_key,
        "tipo_importacion": tipo_importacion,
        "tipo_homologacion": tipo_homologacion,
        "state_purchase_orders": state_purchase_orders,
    }
    if ambient_id:
        values["ambient_id"] = ambient_id

    try:
        record_id = odoo_create(
            tenant_id, url, db, user, password,
            "l10n_ec_sri.edoc.import", values,
        )
    except Exception as e:
        error_msg = str(e)
        if "Ya existe un documento importado" in error_msg:
            return {"success": False, "error": "Documento ya importado previamente", "detail": error_msg}
        if "partner.email" in error_msg or "correo electronico" in error_msg.lower():
            return {"success": False, "error": "El proveedor no tiene correo electronico registrado", "detail": error_msg}
        return {"success": False, "error": f"Error al crear importacion: {error_msg}"}

    # Read-after-write: get the actual state (methods return None via XML-RPC)
    return sri_import_status(
        tenant_id, url, db, user, password, record_id,
    )


def sri_import_status(
    tenant_id: str, url: str, db: str, user: str, password: str,
    record_id: int,
) -> dict:
    """Read the current status of an SRI import record.

    Always call this after create() or any method call to know what happened.
    """
    fields = [
        "estatus_import", "estatus_cola", "products_to_map_count",
        "partner_id", "total", "subtotal", "total_descuento",
        "reference", "autorizacion", "establecimiento", "puntoemision", "secuencial",
        "invoice_date", "fechaautorizacion", "estado_sri",
        "orders_purchase_ids", "invoices_purchase_ids",
        "mensajes", "email",
    ]

    records = odoo_read(
        tenant_id, url, db, user, password,
        "l10n_ec_sri.edoc.import", [record_id], fields,
    )

    if not records:
        return {"success": False, "error": f"Record {record_id} not found"}

    record = records[0]
    return {
        "success": True,
        "record_id": record_id,
        "status": record.get("estatus_import"),
        "queue_status": record.get("estatus_cola"),
        "products_pending": record.get("products_to_map_count", 0),
        "partner": record.get("partner_id"),
        "total": record.get("total"),
        "subtotal": record.get("subtotal"),
        "discount": record.get("total_descuento"),
        "reference": record.get("reference"),
        "authorization": record.get("autorizacion"),
        "sri_status": record.get("estado_sri"),
        "invoice_date": str(record.get("invoice_date", "")),
        "purchase_orders": record.get("orders_purchase_ids", []),
        "invoices": record.get("invoices_purchase_ids", []),
        "messages": record.get("mensajes"),
    }


def sri_import_get_pending_lines(
    tenant_id: str, url: str, db: str, user: str, password: str,
    import_id: int,
) -> list[dict]:
    """Get detail lines that still need product mapping (homologation)."""
    return odoo_search(
        tenant_id, url, db, user, password,
        "l10n_ec_sri.edoc.import.details",
        [["compra_id", "=", import_id], ["product_tmpl_id", "=", False]],
        fields=[
            "product_code", "product_name", "product_qty",
            "price_unit", "total_sin_impuesto", "type",
        ],
    )


def sri_import_assign_product(
    tenant_id: str, url: str, db: str, user: str, password: str,
    line_id: int, product_tmpl_id: int,
) -> bool:
    """Assign a product template to an unmapped SRI import line."""
    return odoo_write(
        tenant_id, url, db, user, password,
        "l10n_ec_sri.edoc.import.details", [line_id],
        {"product_tmpl_id": product_tmpl_id},
    )


def sri_import_create_order(
    tenant_id: str, url: str, db: str, user: str, password: str,
    import_id: int,
) -> dict:
    """Manually trigger order creation (for manual homologation flow).

    Call this after all products have been mapped.
    """
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "l10n_ec_sri.edoc.import", "action_order_create", [import_id],
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

    return sri_import_status(
        tenant_id, url, db, user, password, import_id,
    )
