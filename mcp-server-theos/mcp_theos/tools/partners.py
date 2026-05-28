"""Velneo customer identification + creation.

Customers live in TWO tables: ``ENT`` (master — RUC, email, phone,
address) and ``ENT_ERP_CLI`` (cliente extension — saldo, cupo, fiscal
classification). Both share the same ``ID``. The MCP tools join them
under the hood so the LLM sees a single ``partner`` object.

Identification (``identify_customer``) walks 4 paths in order, falling
back only if the prior path returned 0 rows. Each layer is fast on its
own and cheaper than the next:

  1. **Per-chat cache** — (tenant_id, channel_user_id) → recently
     identified partner. 5-minute TTL. Skips the entire ERP lookup
     when the agent re-asks about the same customer within the same
     turn or two. Cuts the 4× identify_customer loop we saw in the
     "klein tour" Mepriga incident down to 1.
  2. **Exact ERP match** — CIF / email / phone / NAME / NOM_COM via
     ``filter[FIELD]=``. RUC/cédula go through CIF; CIF is tried as
     typed, then with the SRI "001" suffix added or stripped to cover
     the common Ecuadorian shortcut (10 digits typed for a 13-digit
     empresa RUC). Phone numbers get stripped of ``+593``, spaces,
     dashes and the leading 0 before lookup. Name lookup is queried
     in PARALLEL against NAME and NOM_COM (the commercial name often
     differs from the legal one — "Pollos Bachita" vs "BERTHA
     MOREIRA HNOS S.A.").
  3. **Velneo WORDS index** — ``filter[words]=<token>`` on ENT. Uses
     the same denormalized text index the ERP UI uses for its own
     partner search. Much faster than RAG (no embedding call) and
     accurate when the typo is small ("KLEIN" → "KLEINTURS").
  4. **pgvector RAG** — ``tenant_<slug>.partner_embeddings`` via
     bge-m3 embedding. Last-resort fuzzy. RAG hits carry
     ``_match_via='rag'`` and ``_similarity`` so the LLM must
     disambiguate with the user before billing.
"""

from __future__ import annotations

import re
import time
from typing import Any

from mcp_theos.velneo_http import VelneoClient, VelneoError


# ---------------------------------------------------------------------------
# Per-chat partner cache (A5)
#
# Keyed by (tenant_id, channel_user_id). Each entry remembers the partner
# the agent already resolved this turn so a follow-up "y muéstrame las
# facturas de ese cliente" doesn't trigger a fresh ERP round-trip. The
# cache is intentionally in-process (no Redis) — the niko backend keeps
# multiple replicas warm enough that the hit rate is high, and the data
# is non-sensitive enough that losing it on restart costs only a few
# extra Velneo calls.
# ---------------------------------------------------------------------------

_PARTNER_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_PARTNER_CACHE_TTL_S = 300.0  # 5 minutes


def _cache_get(tenant_id: str, channel_user_id: str) -> dict[str, Any] | None:
    if not tenant_id or not channel_user_id:
        return None
    key = (tenant_id, channel_user_id)
    entry = _PARTNER_CACHE.get(key)
    if not entry:
        return None
    ts, snapshot = entry
    if time.monotonic() - ts > _PARTNER_CACHE_TTL_S:
        _PARTNER_CACHE.pop(key, None)
        return None
    return snapshot


def _cache_set(tenant_id: str, channel_user_id: str, snapshot: dict[str, Any]) -> None:
    if not tenant_id or not channel_user_id:
        return
    _PARTNER_CACHE[(tenant_id, channel_user_id)] = (time.monotonic(), snapshot)


# ---------------------------------------------------------------------------
# Normalizers (A2 CIF prefix, A4 phone)
# ---------------------------------------------------------------------------


def _cif_variants(raw: str) -> list[str]:
    """Yield CIF candidates to try, in order, deduplicated.

    Ecuadorian RUC/cédula gotchas this covers:

    * Empresa RUC = cédula del representante (10 digits) + "001"
      (3-digit "subdivisión"). Users frequently type only the 10 digits;
      we retry with "001" appended.
    * A user may type "1790581802001" (full RUC) when ENT stored only
      the 10-digit cédula — strip "001" and retry.
    * Persona cédula = 10 digits. No padding needed.
    """
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return []
    out = [s]
    if len(s) == 10 and s.isdigit():
        out.append(s + "001")
    elif len(s) == 13 and s.isdigit() and s.endswith("001"):
        out.append(s[:-3])
    # Dedup preserving order.
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


_PHONE_NOISE_RE = re.compile(r"[\s\-\(\)\.]")


def _phone_variants(raw: str) -> list[str]:
    """Yield phone variants to try.

    Ecuadorian mobiles are usually stored as ``0991234567`` (10 digits
    with leading 0) but users type ``+593 99 123 4567``, ``593991234567``
    or ``0991234567`` interchangeably. We canonicalize to the digit-only
    form and try both the with-leading-0 and the +593 variants so an
    exact filter hits regardless of how the row was stored.
    """
    s = (raw or "").strip()
    if not s:
        return []
    digits = _PHONE_NOISE_RE.sub("", s).lstrip("+")
    if digits.startswith("593") and len(digits) > 3:
        local = digits[3:]
        local_with_0 = local if local.startswith("0") else "0" + local
        return [local_with_0, local, "+593" + local, "593" + local, s]
    if digits.startswith("0"):
        return [digits, digits[1:], "+593" + digits[1:], s]
    if digits and digits[0] in "9234567":  # mobile / landline w/o prefix
        return ["0" + digits, digits, "+593" + digits, s]
    return [s]

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


async def _enrich_row(client: VelneoClient, row: dict[str, Any]) -> dict[str, Any]:
    """Merge an ENT row with its ENT_ERP_CLI extension."""
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
    return merged


async def _ent_exact(
    client: VelneoClient, field: str, value: str, limit: int = 5,
) -> list[dict[str, Any]]:
    """Single exact-match query against ENT.<field>."""
    try:
        resp = await client.get(
            "ENT", params={field: value, "pagesize": limit},
            fields=_ENT_FIELDS,
        )
    except VelneoError:
        return []
    return resp.rows[:limit]


async def identify_customer(
    client: VelneoClient,
    *,
    ruc: str | None = None,
    cedula: str | None = None,
    email: str | None = None,
    name: str | None = None,
    phone: str | None = None,
    channel_user_id: str | None = None,
) -> dict[str, Any]:
    """Look a customer up in ENT and merge ENT_ERP_CLI.

    See module docstring for the path order (cache → exact → WORDS → RAG).
    ``channel_user_id`` is injected by the transport from
    ``X-Channel-User-Id`` and used for the per-chat cache (A5) — the LLM
    does not need to pass it explicitly.
    """
    tenant_id = getattr(client.cfg, "tenant_id", "") or ""
    cuid = (channel_user_id or "").strip()

    # ── Cache hit ────────────────────────────────────────────────────
    # If the agent already resolved a partner this conversation and the
    # current call carries the SAME identifier (or no identifier at all
    # — meaning "use the one we already have"), short-circuit.
    cached = _cache_get(tenant_id, cuid)
    if cached is not None:
        same_id = (
            (ruc and str(cached.get("vat", "")) == str(ruc).strip()) or
            (cedula and str(cached.get("vat", "")) == str(cedula).strip()) or
            (email and str(cached.get("email", "")).lower() == str(email).strip().lower()) or
            (name and str(cached.get("name", "")).strip().lower() == str(name).strip().lower())
        )
        no_id = not any((ruc, cedula, email, name, phone))
        if same_id or no_id:
            return {
                **cached, "success": True, "found": True,
                "from_cache": True, "cache_ttl_s": _PARTNER_CACHE_TTL_S,
            }

    used: dict[str, Any] = {}
    matches: list[dict[str, Any]] = []
    notes: list[str] = []

    # ── Path 2a: CIF / RUC / cédula with prefix variants (A2) ────────
    identifier = (ruc or cedula or "").strip()
    if identifier:
        used["CIF"] = identifier
        for variant in _cif_variants(identifier):
            rows = await _ent_exact(client, "CIF", variant)
            if rows:
                used["CIF_variant_hit"] = variant
                matches = [await _enrich_row(client, r) for r in rows]
                break

    # ── Path 2b: email exact ─────────────────────────────────────────
    if not matches and email:
        used["MAIL_PRINCIPAL"] = email.strip()
        rows = await _ent_exact(client, "MAIL_PRINCIPAL", email.strip())
        matches = [await _enrich_row(client, r) for r in rows]

    # ── Path 2c: phone with normalization (A4) ───────────────────────
    if not matches and phone:
        used["TFN_PRI"] = phone.strip()
        for variant in _phone_variants(phone):
            rows = await _ent_exact(client, "TFN_PRI", variant)
            if rows:
                used["TFN_variant_hit"] = variant
                matches = [await _enrich_row(client, r) for r in rows]
                break

    # ── Path 2d: NAME + NOM_COM exact, in parallel (A6) ──────────────
    if not matches and name:
        n = name.strip()
        used["NAME"] = n
        import asyncio
        name_rows, com_rows = await asyncio.gather(
            _ent_exact(client, "NAME", n),
            _ent_exact(client, "NOM_COM", n),
            return_exceptions=False,
        )
        seen_ids: set[Any] = set()
        for r in [*name_rows, *com_rows]:
            rid = r.get("ID")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            enriched = await _enrich_row(client, r)
            if r in com_rows and r not in name_rows:
                enriched["_match_via"] = "nom_com"
            matches.append(enriched)

    # ── Path 3: Velneo WORDS index over ENT (A3) ─────────────────────
    # Fast and free — uses the index Velneo's UI uses for its own
    # partner search. Catches small typos and partial words ("KLEIN"
    # → "KLEINTURS Y REPRESENTACIONES").
    if not matches and name:
        # Use the longest token (typically the most distinctive).
        tokens = [t for t in re.split(r"\s+", name.strip()) if len(t) >= 3]
        token = max(tokens, key=len) if tokens else name.strip()
        try:
            resp = await client.get(
                "ENT", params={"words": token, "pagesize": 5},
                fields=_ENT_FIELDS,
            )
            rows = resp.rows[:5]
        except VelneoError:
            rows = []
        for r in rows:
            enriched = await _enrich_row(client, r)
            enriched["_match_via"] = "words"
            matches.append(enriched)
        if rows:
            used["WORDS_token"] = token

    # ── Path 4: pgvector RAG (last-resort fuzzy) ─────────────────────
    if not matches and name:
        try:
            from mcp_theos.rag import partner_matches_by_similarity
            schema = f"tenant_{client.cfg.slug}"
            sim_hits = await partner_matches_by_similarity(
                schema, name.strip(), limit=5,
            )
        except Exception as exc:  # noqa: BLE001 — RAG is best-effort
            sim_hits = []
            notes.append(f"RAG fallback failed: {type(exc).__name__}: {exc}")
        for hit in sim_hits:
            ent_id = hit.get("odoo_id")
            if not ent_id:
                continue
            try:
                ent_resp = await client.get(
                    "ENT", record_id=int(ent_id), fields=_ENT_FIELDS,
                )
            except VelneoError:
                continue
            if not ent_resp.rows:
                continue
            enriched = await _enrich_row(client, ent_resp.rows[0])
            enriched["_match_via"] = "rag"
            enriched["_similarity"] = float(hit.get("similarity") or 0)
            matches.append(enriched)
            if len(matches) >= 5:
                break

    # ── Compose response ─────────────────────────────────────────────
    if not matches and not any((ruc, cedula, email, name, phone)):
        return {"success": False, "error": "no identifier provided", "matches": []}

    out: dict[str, Any] = {
        "success": True,
        "found": bool(matches),
        "lookup": used,
        "count": len(matches),
        "matches": matches,
    }
    if notes:
        out["notes"] = notes

    # Auto-pick the top-level partner_id ONLY when the match path is
    # high-confidence (exact CIF/email/phone/NAME/NOM_COM). WORDS and
    # RAG hits require disambiguation: "KLEIN" might legitimately mean
    # KLEINTURS, but "JOSE PEREZ" returning a wrong Pérez would
    # silently mis-bill someone.
    fuzzy_paths = {"words", "rag"}
    is_fuzzy = bool(matches) and matches[0].get("_match_via") in fuzzy_paths
    if len(matches) == 1 and not is_fuzzy:
        m = matches[0]
        snapshot = {
            "partner_id": m.get("ID"),
            "id": m.get("ID"),
            "name": m.get("NAME") or "",
            "vat": m.get("CIF") or "",
            "email": m.get("MAIL_PRINCIPAL") or "",
            "has_erp_cli": m.get("has_erp_cli", False),
        }
        out.update(snapshot)
        # Cache the resolved partner for the rest of the chat session.
        _cache_set(tenant_id, cuid, {**snapshot, "matches": [m]})
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
