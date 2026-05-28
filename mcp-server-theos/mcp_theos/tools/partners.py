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
    "DEUDAS_VENCIDAS", "DIAS_VENCIDOS",
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

    return {
        "success": True,
        "lookup": used,
        "count": len(matches),
        "matches": matches,
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
        "partner_id": ent_id,
        "ent": ent_row,
        "ent_erp_cli": cli_row,
    }
