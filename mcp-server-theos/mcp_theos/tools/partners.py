"""Velneo customer identification + creation.

Customers live in TWO tables: ``ENT`` (master — RUC, email, phone,
address) and ``ENT_ERP_CLI`` (cliente extension — saldo, cupo, fiscal
classification). Both share the same ``ID``. The MCP tools join them
under the hood so the LLM sees a single ``partner`` object.
"""

from __future__ import annotations

from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError

_ENT_FIELDS = [
    "ID", "NAME", "NOM_COM", "PRS", "CIF",
    "MAIL_PRINCIPAL", "MAIL_ALTERNO",
    "TFN_PRI", "FAX_PRI", "DIR_PRI",
    "PAI", "IDI",
    "SIN_CREDITO", "SIN_CREDITO_RAZON",
    "EXTENSION_ENT_ERP_CLI",
    "OFF",
]

_ENT_ERP_CLI_FIELDS = [
    "ID", "NAME", "CIF",
    "SALDO", "SALDOP", "DEUDASC", "DEUDASCP",
    "CUPOC", "DISPONIBLE_CUPOC",
    # ``DEUDAS_VENCIDAS`` is intentionally absent — Mepriga's Velneo API
    # key rejects projection on that column with "No se retornan valores
    # del campo DEUDAS_VENCIDAS" (returned as an errors[] string), and
    # Velneo's all-or-nothing field semantics means the whole row would
    # be dropped. Use ``DIAS_VENCIDOS`` instead — it carries enough
    # signal for "moroso" detection.
    "DIAS_VENCIDOS",
    "FACTVENCIDAS", "NO_VENDER",
    "TIPO_CONTRIBUYENTE", "SRI_TIPO_IDENTIFICACION",
    "TIPO_CLIENTE", "DESCUENTOC",
    "OFF",
]


async def _read_ent_erp_cli(client: VelneoClient, ent_id: int) -> dict[str, Any] | None:
    try:
        resp = await client.get(
            "ENT_ERP_CLI",
            record_id=ent_id,
            fields=_ENT_ERP_CLI_FIELDS,
        )
    except VelneoError:
        return None
    return resp.rows[0] if resp.rows else None


async def identify_customer(
    client: VelneoClient,
    *,
    ruc: str | None = None,
    cedula: str | None = None,
    email: str | None = None,
    name: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """Look a customer up in ENT and merge ENT_ERP_CLI."""
    identifier = (ruc or cedula or "").strip()
    params: dict[str, Any] = {"pagesize": 5}
    used: dict[str, Any] = {}
    if identifier:
        params["CIF"] = identifier
        used["CIF"] = identifier
    elif email:
        params["MAIL_PRINCIPAL"] = email.strip()
        used["MAIL_PRINCIPAL"] = email.strip()
    elif name:
        params["NAME"] = name.strip()
        used["NAME"] = name.strip()
    elif phone:
        params["TFN_PRI"] = phone.strip()
        used["TFN_PRI"] = phone.strip()
    else:
        return {"success": False, "error": "no identifier provided", "matches": []}

    resp = await client.get("ENT", params=params, fields=_ENT_FIELDS)
    matches: list[dict[str, Any]] = []
    for row in resp.rows[:5]:
        ent_id = row.get("ID")
        ext = await _read_ent_erp_cli(client, ent_id) if ent_id is not None else None
        merged = dict(row)
        if ext:
            for k, v in ext.items():
                if k == "ID":
                    continue
                merged.setdefault(k, v) if k in merged else merged.update({k: v})
            merged["has_erp_cli"] = True
        else:
            merged["has_erp_cli"] = False
        matches.append(merged)

    # The niko orchestrator persists the partner_id by scanning the
    # ToolMessage payload for ``partner_id`` / ``id`` at the top level
    # (mcp-odoo's shape). Mirror that contract so the LLM doesn't have
    # to re-identify next turn (which would also be blocked by the
    # ``tried_identify_before`` loop-breaker that removes
    # identify_customer once it has been called this session).
    out: dict[str, Any] = {
        "success": True,
        "found": bool(matches),
        "lookup": used,
        "count": len(matches),
        "matches": matches,
    }
    if len(matches) == 1:
        m = matches[0]
        out["partner_id"] = m.get("ID")
        out["id"] = m.get("ID")
        out["name"] = m.get("NAME") or ""
        out["vat"] = m.get("CIF") or ""
        out["email"] = m.get("MAIL_PRINCIPAL") or ""
        out["has_erp_cli"] = m.get("has_erp_cli", False)
    return out


async def update_partner(
    client: VelneoClient,
    *,
    partner_id: int,
    email: str | None = None,
    phone: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """Update a customer's contact info in ENT.

    Note (2026-05-28): Mepriga's ``niko_saas`` API key currently
    rejects PATCH / PUT against ENT with
    ``"405 El método PATCH no es válido para este API Key"``. Until
    the operator enables write access on Velneo's Seguridad → API
    key panel, this tool returns ``error_code=not_supported_yet``
    with a verbose message the LLM can verbalize to the customer
    ("no puedo actualizar tu email desde aquí, por favor escribe a
    soporte"). The interface stays in place so the moment write
    access is enabled, only the inner HTTP call needs to change.
    """
    fields: dict[str, Any] = {}
    if email is not None:
        fields["MAIL_PRINCIPAL"] = email.strip()
    if phone is not None:
        fields["TFN_PRI"] = phone.strip()
    if address is not None:
        fields["DIR_PRI"] = address.strip()
    if not fields:
        return {"success": False, "error": "nothing to update"}

    if not partner_id:
        return {"success": False, "error": "partner_id required"}

    # Best-effort attempt — if PATCH ever gets enabled, this works.
    try:
        resp = await client._client.patch(  # noqa: SLF001
            f"ENT/{int(partner_id)}",
            json=fields,
        )
        body = resp.json()
        errors = body.get("errors") or []
        first = errors[0] if isinstance(errors, list) and errors else None
        status = (
            first.get("status") if isinstance(first, dict)
            else (str(first) if first else "")
        )
        if status == "405" or "no es válido para este API Key" in str(first or ""):
            return {
                "success": False,
                "error_code": "not_supported_yet",
                "error": (
                    "Por el momento NO puedo actualizar la ficha del "
                    "cliente desde el chat (el ERP requiere permisos de "
                    "escritura que aún no están habilitados para el bot). "
                    "Dile al cliente: 'No puedo actualizar tus datos de "
                    "contacto desde aquí; por favor escribe al área de "
                    "atención al cliente o pásate por la oficina con tu "
                    "RUC y los nuevos datos.' NO digas 'lo intentaré' "
                    "porque no se puede."
                ),
            }
        # If Velneo accepts in the future, the row comes back like POST.
        if errors:
            return {
                "success": False,
                "error": f"velneo {status}: {first}",
            }
        # Re-read to confirm.
        check = await client.get(
            "ENT", record_id=int(partner_id),
            fields=["ID", "MAIL_PRINCIPAL", "TFN_PRI", "DIR_PRI"],
        )
        if check.rows:
            return {
                "success": True,
                "partner_id": int(partner_id),
                "updated_fields": list(fields.keys()),
                "ent": check.rows[0],
            }
        return {"success": True, "partner_id": int(partner_id),
                "updated_fields": list(fields.keys())}
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error_code": "transport_error",
            "error": f"{type(exc).__name__}: {exc}",
        }


async def create_partner(
    client: VelneoClient,
    *,
    name: str,
    cif: str,
    email: str | None = None,
    phone: str | None = None,
    address: str | None = None,
    is_person: bool = True,
    tipo_cliente: int | None = None,
) -> dict[str, Any]:
    """Create a customer in ENT + matching row in ENT_ERP_CLI (same ID).

    Velneo requires the two POSTs to use the same ``ID``; we read back
    the ENT row returned by the first call and reuse its ``ID`` for the
    extension row.
    """
    if not name or not cif:
        return {"success": False, "error": "name and cif are required"}

    ent_body: dict[str, Any] = {
        "NAME": name.strip(),
        "CIF": cif.strip(),
        "PRS": bool(is_person),
    }
    if email:
        ent_body["MAIL_PRINCIPAL"] = email.strip()
    if phone:
        ent_body["TFN_PRI"] = phone.strip()
    if address:
        ent_body["DIR_PRI"] = address.strip()

    try:
        ent_row = await client.post("ENT", ent_body)
    except VelneoError as exc:
        return {
            "success": False,
            "error": f"ENT create failed: velneo {exc.status} {exc.message}",
        }

    ent_id = ent_row.get("ID")
    if ent_id is None:
        return {
            "success": False,
            "error": "ENT created but Velneo response has no ID",
            "raw": ent_row,
        }

    cli_body: dict[str, Any] = {
        "ID": ent_id,
        "NAME": name.strip(),
        "CIF": cif.strip(),
    }
    if tipo_cliente is not None:
        cli_body["TIPO_CLIENTE"] = tipo_cliente

    try:
        cli_row = await client.post("ENT_ERP_CLI", cli_body)
    except VelneoError as exc:
        return {
            "success": False,
            "error": f"ENT_ERP_CLI create failed: velneo {exc.status} {exc.message}",
            "ent_id": ent_id,
            "rollback_required": True,
        }

    return {
        "success": True,
        "found": True,
        # ``partner_id`` / ``id`` at the top level so the niko
        # orchestrator persists the new partner immediately (see
        # _extract_partner_id_from_messages in orchestrator.py).
        "partner_id": ent_id,
        "id": ent_id,
        "name": (ent_row.get("NAME") or name or "").strip(),
        "vat": (ent_row.get("CIF") or cif or "").strip(),
        "email": (ent_row.get("MAIL_PRINCIPAL") or email or "").strip(),
        "ent": ent_row,
        "ent_erp_cli": cli_row,
    }
