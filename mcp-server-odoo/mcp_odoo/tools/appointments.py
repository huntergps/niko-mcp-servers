"""Appointment / booking tools for salon tenants (Odoo 19 ``appointment``).

Five read/write helpers that let a chat agent run a beauty salon's booking
flow end to end:

  * ``list_services``         → catalogue of ``appointment.type`` (name,
                                 duration, price from ``product_id.list_price``)
  * ``get_availability``      → free slots for a service over a date range
  * ``book_appointment``      → create a ``calendar.event`` for a customer
  * ``list_my_appointments``  → a customer's upcoming bookings
  * ``cancel_appointment``    → cancel one of the customer's bookings

Design notes
------------
* Pure functions: every helper receives the per-request Odoo connection
  tuple ``(tenant_id, url, db, user, password)`` plus typed args, exactly
  like ``mcp_odoo.tools.invoices``. The dispatch layer in
  ``mcp_transport.py`` resolves that tuple from the request headers
  (multi-tenant) and passes it down — we never open a connection here.
* All Odoo I/O goes through the shared ``odoo_search`` / ``odoo_read`` /
  ``odoo_create`` / ``odoo_call_method`` from ``mcp_odoo.tools.generic``.
* Odoo stores ``calendar.event`` datetimes in **UTC**. The salon operates
  in ``appointment.type.appointment_tz`` (e.g. 'Pacific/Galapagos'). We
  convert with ``zoneinfo.ZoneInfo`` at every boundary and surface local
  time to the customer.
* ``appointment.slot.weekday`` is an Odoo selection STRING where
  ``'1'..'7'`` map to Monday..Sunday. Python's ``date.weekday()`` is
  ``0..6`` (Monday=0). The mapping is ``odoo_str = str(py_weekday + 1)``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp_odoo.tools.generic import (
    odoo_call_method,
    odoo_create,
    odoo_read,
    odoo_search,
    valid_partner_fields,
)

logger = logging.getLogger("mcp_odoo.appointments")

# Candidate res.partner phone fields. Odoo 19 dropped ``res.partner.mobile``,
# so we never assume it exists — ``valid_partner_fields`` prunes the list to
# the columns the live model actually has before we build the search domain.
_PARTNER_PHONE_FIELDS = ["phone", "mobile"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TZ = "Pacific/Galapagos"
_SLOT_STEP_MINUTES = 30  # granularity of candidate start times
_MAX_DAYS_WITH_AVAIL = 3  # cap on days surfaced to the customer
_MAX_SLOTS_PER_DAY = 6  # cap on free slots per day surfaced

# Odoo appointment.slot.weekday ('1'=Mon .. '7'=Sun) → Spanish label.
_WEEKDAY_LABEL_ES = {
    "1": "Lunes",
    "2": "Martes",
    "3": "Miércoles",
    "4": "Jueves",
    "5": "Viernes",
    "6": "Sábado",
    "7": "Domingo",
}

# Python date.weekday() (Mon=0) → Spanish label, used to render dates.
_PY_WEEKDAY_LABEL_ES = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
    6: "Domingo",
}

_UTC = ZoneInfo("UTC")

# Fields read from appointment.type.
_TYPE_FIELDS = [
    "id",
    "name",
    "appointment_duration",
    "staff_user_ids",
    "appointment_tz",
    "min_schedule_hours",
    "product_id",
    # Owner decision: bookings inherit the reminders configured on the
    # appointment.type (m2m calendar.alarm), so the customer gets the
    # same email/SMS reminders the salon set up per service.
    "reminder_ids",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_call(
    tool: str, tenant_id: str, args: dict, result_summary: Any,
    error: str | None, duration_ms: int,
) -> None:
    """Lightweight structured log (mirrors invoices._log_call style)."""
    if error:
        logger.warning(
            "appointment_tool=%s tenant=%s args=%s error=%s ms=%d",
            tool, tenant_id, args, error, duration_ms,
        )
    else:
        logger.info(
            "appointment_tool=%s tenant=%s args=%s result=%s ms=%d",
            tool, tenant_id, args, result_summary, duration_ms,
        )


def _resolve_tz(appointment_tz: Any) -> ZoneInfo:
    """Return a ZoneInfo for the type's tz, falling back to DEFAULT_TZ."""
    name = appointment_tz if isinstance(appointment_tz, str) and appointment_tz.strip() else DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unknown tz string
        return ZoneInfo(DEFAULT_TZ)


def _fmt_duration(hours: float) -> str:
    """Render a float-hours duration as human Spanish (e.g. '1 h 15 min')."""
    try:
        total_min = int(round(float(hours) * 60))
    except (TypeError, ValueError):
        return ""
    if total_min <= 0:
        return ""
    h, mm = divmod(total_min, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h} h")
    if mm:
        parts.append(f"{mm} min")
    return " ".join(parts) if parts else "0 min"


def _float_hour_to_hm(value: float) -> tuple[int, int]:
    """Convert an Odoo float hour (e.g. 9.5) to (hour, minute)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return (0, 0)
    h = int(v)
    mm = int(round((v - h) * 60))
    if mm >= 60:  # guard rounding artefacts
        h += 1
        mm = 0
    return (h, mm)


def _parse_odoo_dt_utc(raw: Any) -> datetime | None:
    """Parse an Odoo 'YYYY-MM-DD HH:MM:SS' (UTC, naive) into aware UTC dt."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=_UTC)
        except ValueError:
            continue
    return None


def _flatten_m2o(value: Any) -> dict | None:
    """``[id, name]`` -> ``{"id": id, "name": name}``."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {"id": value[0], "name": value[1]}
    return None


def _resolve_appointment_type(
    creds: tuple, service: str,
) -> dict | None:
    """Resolve a service name to its appointment.type record (fuzzy ilike).

    Tries an exact (case-insensitive) ilike first; if multiple match,
    prefers the shortest name (most specific exact hit). Returns the read
    dict with ``_TYPE_FIELDS`` or ``None`` when nothing matches.
    """
    name = (service or "").strip()
    if not name:
        return None
    tenant_id, url, db, user, password = creds
    rows = odoo_search(
        tenant_id, url, db, user, password,
        "appointment.type", [["name", "ilike", name]],
        fields=_TYPE_FIELDS, limit=10, order="name",
    )
    if not rows:
        return None
    # Prefer an exact case-insensitive match if present.
    lowered = name.lower()
    exact = [r for r in rows if (r.get("name") or "").strip().lower() == lowered]
    if exact:
        return exact[0]
    # Else prefer the shortest name (most specific) to avoid grabbing a
    # longer unrelated service that merely contains the substring.
    rows.sort(key=lambda r: len(r.get("name") or ""))
    return rows[0]


def _read_work_windows(
    creds: tuple, type_id: int,
) -> dict[str, list[tuple[float, float]]]:
    """Read recurring appointment.slot windows grouped by Odoo weekday str.

    Returns ``{'1': [(9.0, 18.0)], ...}``. Only ``slot_type='recurring'``
    and non-allday windows are considered (allday slots have no usable
    start/end hour for our 30-min stepping).
    """
    tenant_id, url, db, user, password = creds
    slots = odoo_search(
        tenant_id, url, db, user, password,
        "appointment.slot",
        [
            ["appointment_type_id", "=", type_id],
            ["slot_type", "=", "recurring"],
        ],
        fields=["weekday", "start_hour", "end_hour", "allday"],
        limit=200,
    )
    windows: dict[str, list[tuple[float, float]]] = {}
    for s in slots:
        if s.get("allday"):
            continue
        wd = str(s.get("weekday") or "").strip()
        if wd not in _WEEKDAY_LABEL_ES:
            continue
        start_h = float(s.get("start_hour") or 0)
        end_h = float(s.get("end_hour") or 0)
        if end_h <= start_h:
            continue
        windows.setdefault(wd, []).append((start_h, end_h))
    return windows


def _read_busy_intervals(
    creds: tuple, staff_user_ids: list[int],
    range_from_utc: datetime, range_to_utc: datetime,
) -> list[tuple[datetime, datetime]]:
    """Read calendar.event busy intervals for the staff within the range.

    Returns a list of aware-UTC ``(start, stop)`` tuples. Overlap-query:
    any event that starts before ``range_to`` AND stops after
    ``range_from``.
    """
    if not staff_user_ids:
        return []
    tenant_id, url, db, user, password = creds
    from_iso = range_from_utc.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S")
    to_iso = range_to_utc.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S")
    events = odoo_search(
        tenant_id, url, db, user, password,
        "calendar.event",
        [
            ["user_id", "in", list(staff_user_ids)],
            ["start", "<=", to_iso],
            ["stop", ">=", from_iso],
        ],
        fields=["start", "stop"],
        limit=500,
        order="start",
    )
    busy: list[tuple[datetime, datetime]] = []
    for e in events:
        st = _parse_odoo_dt_utc(e.get("start"))
        sp = _parse_odoo_dt_utc(e.get("stop"))
        if st and sp and sp > st:
            busy.append((st, sp))
    return busy


def _overlaps(
    start: datetime, stop: datetime,
    busy: list[tuple[datetime, datetime]],
) -> bool:
    """True if ``[start, stop)`` intersects any busy interval."""
    for b_start, b_stop in busy:
        if start < b_stop and stop > b_start:
            return True
    return False


def _normalize_phone(raw: Any) -> str:
    """Basic phone normalization for comparison: strip spaces/dashes/parens.

    Keeps a leading ``+`` and digits only. Used to dedupe returning
    customers — we are NOT trying to be a full E.164 normalizer, just to
    ignore cosmetic separators ('099 000-0001' == '0990000001').
    """
    if not raw:
        return ""
    s = str(raw).strip()
    plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    return ("+" + digits) if plus else digits


def _resolve_or_create_partner(
    creds: tuple,
    partner_id: int | None,
    customer_name: str | None,
    customer_phone: str | None,
) -> dict:
    """Resolve the booking customer to a ``res.partner`` id.

    Resolution order:
      1. A valid ``partner_id`` → used verbatim (existing customer).
      2. Otherwise ``customer_phone`` is mandatory. We look up an existing
         res.partner whose phone matches (OR over the phone fields that
         EXIST on this Odoo — Odoo 19 dropped ``res.partner.mobile``).
         Phones are compared after basic normalization to dedupe returning
         clients. On a match the existing partner is reused.
      3. No match → create a minimal contact ``{'name', 'phone'}`` — SIN
         vat, SIN customer_rank, SIN SRI. This mirrors what Odoo's own web
         booking flow does (a contact, not a fiscal customer).

    Returns ``{"partner_id": int, "created": bool, "name": str,
    "phone": str}`` on success, or ``{"error_code", "error_detail"}`` on
    failure (e.g. ``phone_required``).
    """
    tenant_id, url, db, user, password = creds

    # 1. Existing partner_id wins.
    if isinstance(partner_id, int) and partner_id > 0:
        name = ""
        try:
            prows = odoo_read(
                tenant_id, url, db, user, password,
                "res.partner", [partner_id], ["name", "phone"],
            )
            if prows:
                name = prows[0].get("name") or ""
                phone = prows[0].get("phone") or ""
            else:
                phone = ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "book_appointment: partner read failed for %s: %s",
                partner_id, exc,
            )
            phone = ""
        return {
            "partner_id": int(partner_id),
            "created": False,
            "name": name,
            "phone": phone,
        }

    # 2. No partner_id → phone is mandatory.
    raw_phone = (customer_phone or "").strip()
    if not raw_phone:
        return {
            "error_code": "phone_required",
            "error_detail": (
                "Para agendar necesito tu número de celular (y tu nombre). "
                "¿Me lo compartes, por favor?"
            ),
        }
    norm_phone = _normalize_phone(raw_phone)

    # Which phone fields actually exist on this Odoo's res.partner?
    phone_fields = valid_partner_fields(
        tenant_id, url, db, user, password, list(_PARTNER_PHONE_FIELDS),
    )
    if not phone_fields:  # extremely defensive — phone always exists
        phone_fields = ["phone"]

    # Build an OR domain over the existing phone fields and dedupe by a
    # normalized comparison (Odoo stores the raw string, so we over-fetch
    # a few candidates and compare normalized values ourselves).
    domain: list = []
    for f in phone_fields:
        domain.append([f, "=", raw_phone])
    if len(domain) > 1:
        domain = ["|"] * (len(domain) - 1) + domain

    existing = None
    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "res.partner", domain,
            fields=["id", "name"] + phone_fields, limit=10, order="id",
        )
        for r in rows:
            for f in phone_fields:
                if _normalize_phone(r.get(f)) == norm_phone and norm_phone:
                    existing = r
                    break
            if existing:
                break
        # Fallback: exact-string domain may miss on spacing differences, so
        # if nothing matched but rows came back, still trust an exact-string
        # hit (Odoo already filtered to the raw value).
        if existing is None and rows:
            existing = rows[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "book_appointment: partner phone lookup failed: %s", exc,
        )

    if existing:
        return {
            "partner_id": int(existing["id"]),
            "created": False,
            "name": existing.get("name") or (customer_name or ""),
            "phone": raw_phone,
        }

    # 3. Create a minimal contact (NO vat, NO customer_rank, NO SRI).
    # We deliberately do NOT use the create_partner tool — that one is
    # bound to the SRI / fiscal flow. A booking only needs a contact.
    create_vals = {
        "name": (customer_name or "").strip() or "Cliente",
        "phone": raw_phone,
    }
    try:
        new_id = odoo_create(
            tenant_id, url, db, user, password,
            "res.partner", create_vals,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "error_code": "partner_create_failed",
            "error_detail": f"No pude registrar tu contacto: {exc}",
        }
    return {
        "partner_id": int(new_id),
        "created": True,
        "name": create_vals["name"],
        "phone": raw_phone,
    }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def list_services(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    query: str | None = None,
) -> dict:
    """List bookable salon services (``appointment.type``).

    Args
    ----
    query : str, optional
        Case-insensitive substring filter on the service name.

    Returns
    -------
    dict
        Envelope with ``success, count, services[]``. Each service has
        ``service_id, name, duration_hours, duration_label, price,
        currency``.
    """
    started = time.time()
    creds = (tenant_id, url, db, user, password)
    log_args = {"query": query}

    domain: list = []
    if query and str(query).strip():
        domain.append(["name", "ilike", str(query).strip()])

    try:
        rows = odoo_search(
            tenant_id, url, db, user, password,
            "appointment.type", domain,
            fields=_TYPE_FIELDS, limit=100, order="name",
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error listando servicios: {exc}"
        _log_call("list_services", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "search_failed",
                "error_detail": msg}

    # Resolve product prices in a single batch.
    product_ids: list[int] = []
    for r in rows:
        prod = _flatten_m2o(r.get("product_id"))
        if prod:
            product_ids.append(int(prod["id"]))
    price_lookup: dict[int, float] = {}
    if product_ids:
        try:
            prods = odoo_read(
                tenant_id, url, db, user, password,
                "product.product", sorted(set(product_ids)),
                ["list_price"],
            )
            price_lookup = {
                int(p["id"]): float(p.get("list_price") or 0) for p in prods
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_services: price lookup failed: %s", exc)

    services: list[dict] = []
    for r in rows:
        duration = float(r.get("appointment_duration") or 0)
        prod = _flatten_m2o(r.get("product_id"))
        price = price_lookup.get(int(prod["id"])) if prod else None
        services.append({
            "service_id": int(r["id"]),
            "name": r.get("name") or "",
            "duration_hours": round(duration, 2),
            "duration_label": _fmt_duration(duration),
            "price": round(price, 2) if price is not None else None,
            "currency": "USD",
        })

    result = {
        "success": True,
        "count": len(services),
        "services": services,
        "display_type": "list_data",
    }
    _log_call("list_services", tenant_id, log_args, {"count": len(services)},
              None, int((time.time() - started) * 1000))
    return result


def get_availability(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    service: str,
    date_from: str | None = None,
    days_ahead: int = 7,
) -> dict:
    """Compute free booking slots for a service over a date range.

    Args
    ----
    service : str
        Service name (resolved fuzzily to ``appointment.type``).
    date_from : str, optional
        Start date 'YYYY-MM-DD' (local salon tz). Defaults to today.
    days_ahead : int, default 7
        Number of days to scan from ``date_from`` (clamped 1..30).

    Returns
    -------
    dict
        Envelope with ``success, service, duration_label, timezone,
        days[]``. Each day: ``date, weekday, slots[]`` where each slot is
        ``{start_local, label}``. Limited to ~3 days / ~6 slots per day.
    """
    started = time.time()
    creds = (tenant_id, url, db, user, password)
    log_args = {"service": service, "date_from": date_from,
                "days_ahead": days_ahead}

    atype = _resolve_appointment_type(creds, service)
    if not atype:
        return {
            "success": False,
            "error_code": "service_not_found",
            "error_detail": (
                f"No encontré un servicio que coincida con '{service}'. "
                "Usa list_services para ver los servicios disponibles."
            ),
        }

    type_id = int(atype["id"])
    duration = float(atype.get("appointment_duration") or 0) or 0.5
    tz = _resolve_tz(atype.get("appointment_tz"))
    min_hours = float(atype.get("min_schedule_hours") or 0)
    staff_ids = [int(x) for x in (atype.get("staff_user_ids") or [])]

    try:
        days_ahead_int = max(1, min(int(days_ahead or 7), 30))
    except (TypeError, ValueError):
        days_ahead_int = 7

    now_local = datetime.now(tz)
    if date_from and str(date_from).strip():
        try:
            base_day = datetime.strptime(str(date_from).strip()[:10],
                                         "%Y-%m-%d").date()
        except ValueError:
            return {"success": False, "error_code": "invalid_date_from",
                    "error_detail": "date_from debe ser 'YYYY-MM-DD'."}
    else:
        base_day = now_local.date()

    # Earliest bookable local datetime, respecting min_schedule_hours.
    earliest_local = now_local + timedelta(hours=min_hours)

    try:
        windows = _read_work_windows(creds, type_id)
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo horario laboral: {exc}"
        _log_call("get_availability", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "slot_read_failed",
                "error_detail": msg}

    # Range bounds in UTC for the busy-event query.
    range_from_local = datetime.combine(
        base_day, datetime.min.time(), tzinfo=tz,
    )
    range_to_local = datetime.combine(
        base_day + timedelta(days=days_ahead_int),
        datetime.min.time(), tzinfo=tz,
    )
    try:
        busy = _read_busy_intervals(
            creds, staff_ids,
            range_from_local.astimezone(_UTC),
            range_to_local.astimezone(_UTC),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_availability: busy read failed: %s", exc)
        busy = []

    dur_delta = timedelta(hours=duration)
    step = timedelta(minutes=_SLOT_STEP_MINUTES)

    days_out: list[dict] = []
    for offset in range(days_ahead_int):
        day = base_day + timedelta(days=offset)
        odoo_wd = str(day.weekday() + 1)  # Mon=0 → '1'
        day_windows = windows.get(odoo_wd)
        if not day_windows:
            continue

        free_slots: list[dict] = []
        for start_h, end_h in sorted(day_windows):
            sh, smm = _float_hour_to_hm(start_h)
            eh, emm = _float_hour_to_hm(end_h)
            window_start = datetime(day.year, day.month, day.day, sh, smm,
                                    tzinfo=tz)
            window_end = datetime(day.year, day.month, day.day, eh, emm,
                                  tzinfo=tz)

            candidate = window_start
            while candidate + dur_delta <= window_end:
                cand_stop = candidate + dur_delta
                # Respect minimum lead time.
                if candidate < earliest_local:
                    candidate += step
                    continue
                # Discard if it overlaps a busy event (buffer = duration).
                cand_utc = candidate.astimezone(_UTC)
                cand_stop_utc = cand_stop.astimezone(_UTC)
                if _overlaps(cand_utc, cand_stop_utc, busy):
                    candidate += step
                    continue
                free_slots.append({
                    "start_local": candidate.strftime("%Y-%m-%d %H:%M"),
                    "label": candidate.strftime("%H:%M"),
                })
                if len(free_slots) >= _MAX_SLOTS_PER_DAY:
                    break
                candidate += step
            if len(free_slots) >= _MAX_SLOTS_PER_DAY:
                break

        if free_slots:
            days_out.append({
                "date": day.isoformat(),
                "weekday": _PY_WEEKDAY_LABEL_ES.get(day.weekday(), ""),
                "slots": free_slots,
            })
        if len(days_out) >= _MAX_DAYS_WITH_AVAIL:
            break

    result = {
        "success": True,
        "service": atype.get("name") or service,
        "service_id": type_id,
        "duration_hours": round(duration, 2),
        "duration_label": _fmt_duration(duration),
        "timezone": str(tz),
        "days": days_out,
        "display_type": "list_data",
    }
    _log_call("get_availability", tenant_id, log_args,
              {"days_with_avail": len(days_out)}, None,
              int((time.time() - started) * 1000))
    return result


def book_appointment(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    service: str,
    start_local: str,
    partner_id: int | None = None,
    customer_name: str | None = None,
    customer_phone: str | None = None,
) -> dict:
    """Book an appointment by creating a ``calendar.event``.

    Args
    ----
    service : str
        Service name (resolved to ``appointment.type``).
    start_local : str
        Local start time 'YYYY-MM-DD HH:MM' in the salon's tz.
    partner_id : int, optional
        ``res.partner.id`` of an already-identified customer. When given
        it is used verbatim.
    customer_name : str, optional
        Customer name used to create a minimal contact when there is no
        ``partner_id`` (defaults to 'Cliente' if missing).
    customer_phone : str, optional
        Customer cellphone. **Mandatory when there is no ``partner_id``** —
        it is used to look up a returning customer (dedupe) and, failing
        that, to create a minimal contact (name + phone, SIN cédula).

    The booking is created as **POR CONFIRMAR**: the salon requires a 50%
    non-refundable deposit before the slot is held. We prefix the
    calendar.event name with ``[POR CONFIRMAR] `` and return
    ``pending_deposit: true`` — we do NOT charge anything here.

    Re-validates that the slot is still free (no overlap) and falls within
    a working window before creating. Returns an envelope with the booking
    confirmation or a clear error.
    """
    started = time.time()
    creds = (tenant_id, url, db, user, password)
    log_args = {"service": service, "partner_id": partner_id,
                "start_local": start_local,
                "has_name": bool(customer_name),
                "has_phone": bool(customer_phone)}

    atype = _resolve_appointment_type(creds, service)
    if not atype:
        return {
            "success": False,
            "error_code": "service_not_found",
            "error_detail": (
                f"No encontré un servicio que coincida con '{service}'. "
                "Usa list_services para ver los servicios disponibles."
            ),
        }

    type_id = int(atype["id"])
    duration = float(atype.get("appointment_duration") or 0) or 0.5
    tz = _resolve_tz(atype.get("appointment_tz"))
    min_hours = float(atype.get("min_schedule_hours") or 0)
    staff_ids = [int(x) for x in (atype.get("staff_user_ids") or [])]
    if not staff_ids:
        return {
            "success": False,
            "error_code": "no_staff",
            "error_detail": "Este servicio no tiene un profesional asignado.",
        }
    staff_user_id = staff_ids[0]

    # Parse start_local.
    try:
        start_dt_local = datetime.strptime(
            str(start_local).strip()[:16], "%Y-%m-%d %H:%M",
        ).replace(tzinfo=tz)
    except ValueError:
        return {
            "success": False,
            "error_code": "invalid_start_local",
            "error_detail": "start_local debe ser 'YYYY-MM-DD HH:MM'.",
        }

    stop_dt_local = start_dt_local + timedelta(hours=duration)
    start_utc = start_dt_local.astimezone(_UTC)
    stop_utc = stop_dt_local.astimezone(_UTC)

    # ----- Re-validate: lead time -------------------------------------
    now_local = datetime.now(tz)
    if start_dt_local < now_local + timedelta(hours=min_hours):
        return {
            "success": False,
            "error_code": "too_soon",
            "error_detail": (
                "Ese horario no respeta la antelación mínima de "
                f"{_fmt_duration(min_hours) or f'{min_hours:g} h'}. "
                "Usa get_availability para ver horarios válidos."
            ),
        }

    # ----- Re-validate: inside a working window ------------------------
    try:
        windows = _read_work_windows(creds, type_id)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error_code": "slot_read_failed",
                "error_detail": f"Error leyendo horario: {exc}"}
    odoo_wd = str(start_dt_local.weekday() + 1)
    day_windows = windows.get(odoo_wd) or []
    start_hour_f = start_dt_local.hour + start_dt_local.minute / 60.0
    stop_hour_f = start_hour_f + duration
    inside = any(
        start_hour_f >= w_start and stop_hour_f <= w_end
        for w_start, w_end in day_windows
    )
    if not inside:
        return {
            "success": False,
            "error_code": "outside_hours",
            "error_detail": (
                "Ese horario está fuera del horario de atención para ese "
                "servicio. Usa get_availability para ver horarios libres."
            ),
        }

    # ----- Re-validate: no overlap ------------------------------------
    try:
        busy = _read_busy_intervals(creds, staff_ids, start_utc, stop_utc)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error_code": "busy_read_failed",
                "error_detail": f"Error verificando disponibilidad: {exc}"}
    if _overlaps(start_utc, stop_utc, busy):
        return {
            "success": False,
            "error_code": "slot_taken",
            "error_detail": (
                "Ese horario acaba de ocuparse. Usa get_availability para "
                "ver los horarios que siguen libres."
            ),
        }

    # ----- Resolve (or create) the customer partner -------------------
    # Done AFTER the slot validations so we never create a throwaway
    # contact for a slot that turns out to be taken / out of hours.
    resolved = _resolve_or_create_partner(
        creds, partner_id, customer_name, customer_phone,
    )
    if "error_code" in resolved:
        _log_call("book_appointment", tenant_id, log_args, None,
                  resolved["error_code"],
                  int((time.time() - started) * 1000))
        return {"success": False, **resolved}
    resolved_partner_id = int(resolved["partner_id"])
    partner_created = bool(resolved["created"])
    resolved_name = resolved.get("name") or ""
    resolved_phone = resolved.get("phone") or ""

    # ----- Resolve staff partner name ----------------------------------
    staff_partner_id: int | None = None
    try:
        urows = odoo_read(
            tenant_id, url, db, user, password,
            "res.users", [staff_user_id], ["partner_id"],
        )
        if urows:
            sp = _flatten_m2o(urows[0].get("partner_id"))
            if sp:
                staff_partner_id = int(sp["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("book_appointment: staff partner read failed: %s", exc)

    attendee_ids = [resolved_partner_id]
    if staff_partner_id and staff_partner_id != resolved_partner_id:
        attendee_ids.append(staff_partner_id)

    service_name = atype.get("name") or service
    base_name = f"{service_name} - {resolved_name}".strip(" -")
    # Owner decision: bookings stay POR CONFIRMAR until staff verify the
    # 50% non-refundable deposit. Prefix the event name so staff see it at
    # a glance in the calendar.
    event_name = f"[POR CONFIRMAR] {base_name}"

    # Inherit the reminders (email / SMS calendar.alarm) configured on the
    # appointment.type so the customer gets the same notifications the
    # salon set up for this specific service. Never hardcode alarms — each
    # service may define its own ``reminder_ids``.
    reminder_ids = [int(x) for x in (atype.get("reminder_ids") or [])
                    if isinstance(x, int)]

    values: dict[str, Any] = {
        "name": event_name,
        "start": start_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "stop": stop_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": staff_user_id,
        "partner_ids": [(6, 0, attendee_ids)],
        "appointment_type_id": type_id,
        "duration": duration,
    }
    if reminder_ids:
        values["alarm_ids"] = [(6, 0, reminder_ids)]

    try:
        event_id = odoo_create(
            tenant_id, url, db, user, password,
            "calendar.event", values,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error creando la cita: {exc}"
        _log_call("book_appointment", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "create_failed",
                "error_detail": msg}

    result = {
        "success": True,
        "event_id": int(event_id),
        "service": service_name,
        "partner_id": resolved_partner_id,
        "partner_created": partner_created,
        "customer_name": resolved_name,
        "phone": resolved_phone,
        "pending_deposit": True,
        "start_local": start_dt_local.strftime("%Y-%m-%d %H:%M"),
        "stop_local": stop_dt_local.strftime("%Y-%m-%d %H:%M"),
        "weekday": _PY_WEEKDAY_LABEL_ES.get(start_dt_local.weekday(), ""),
        "duration_hours": round(duration, 2),
        "duration_label": _fmt_duration(duration),
        "timezone": str(tz),
    }
    _log_call("book_appointment", tenant_id, log_args,
              {"event_id": int(event_id)}, None,
              int((time.time() - started) * 1000))
    return result


def list_my_appointments(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    partner_id: int,
) -> dict:
    """List a customer's upcoming appointments (``calendar.event``).

    Args
    ----
    partner_id : int
        ``res.partner.id`` of the customer.

    Returns
    -------
    dict
        Envelope with ``success, partner_id, count, appointments[]``. Each
        appointment: ``event_id, service, start_local, weekday,
        duration_label, timezone, state``. Only future events (start >=
        now) are returned, ordered by start ascending.
    """
    started = time.time()
    log_args = {"partner_id": partner_id}

    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id"}

    now_utc = datetime.now(_UTC)
    now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    try:
        events = odoo_search(
            tenant_id, url, db, user, password,
            "calendar.event",
            [
                ["partner_ids", "in", [partner_id]],
                ["start", ">=", now_iso],
            ],
            fields=["name", "start", "stop", "duration",
                    "appointment_type_id", "user_id"],
            limit=50,
            order="start asc",
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error listando tus citas: {exc}"
        _log_call("list_my_appointments", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "search_failed",
                "error_detail": msg}

    # Resolve tz per appointment.type when available; default otherwise.
    type_ids = sorted({
        int(_flatten_m2o(e.get("appointment_type_id"))["id"])
        for e in events
        if _flatten_m2o(e.get("appointment_type_id"))
    })
    tz_lookup: dict[int, str] = {}
    if type_ids:
        try:
            trows = odoo_read(
                tenant_id, url, db, user, password,
                "appointment.type", type_ids, ["appointment_tz"],
            )
            tz_lookup = {
                int(t["id"]): (t.get("appointment_tz") or "") for t in trows
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_my_appointments: tz lookup failed: %s", exc)

    appointments: list[dict] = []
    for e in events:
        start_utc = _parse_odoo_dt_utc(e.get("start"))
        if not start_utc:
            continue
        atype_ref = _flatten_m2o(e.get("appointment_type_id"))
        tz_name = tz_lookup.get(int(atype_ref["id"])) if atype_ref else None
        tz = _resolve_tz(tz_name)
        start_local = start_utc.astimezone(tz)
        duration = float(e.get("duration") or 0)
        appointments.append({
            "event_id": int(e["id"]),
            "service": atype_ref["name"] if atype_ref else (e.get("name") or ""),
            "start_local": start_local.strftime("%Y-%m-%d %H:%M"),
            "weekday": _PY_WEEKDAY_LABEL_ES.get(start_local.weekday(), ""),
            "duration_hours": round(duration, 2),
            "duration_label": _fmt_duration(duration),
            "timezone": str(tz),
            "state": "confirmada",
        })

    result = {
        "success": True,
        "partner_id": partner_id,
        "count": len(appointments),
        "appointments": appointments,
        "display_type": "list_data",
    }
    _log_call("list_my_appointments", tenant_id, log_args,
              {"count": len(appointments)}, None,
              int((time.time() - started) * 1000))
    return result


def cancel_appointment(
    tenant_id: str,
    url: str,
    db: str,
    user: str,
    password: str,
    event_id: int,
    partner_id: int,
) -> dict:
    """Cancel a customer's appointment after authorization check.

    Args
    ----
    event_id : int
        ``calendar.event.id`` to cancel.
    partner_id : int
        ``res.partner.id`` — must be among the event's attendees.

    Verifies the event belongs to the partner, then unlinks it (falling
    back to ``write active=False`` if the unlink is denied by ACL).
    """
    started = time.time()
    log_args = {"event_id": event_id, "partner_id": partner_id}

    if not isinstance(event_id, int) or event_id <= 0:
        return {"success": False, "error_code": "invalid_event_id"}
    if not isinstance(partner_id, int) or partner_id <= 0:
        return {"success": False, "error_code": "invalid_partner_id"}

    try:
        rows = odoo_read(
            tenant_id, url, db, user, password,
            "calendar.event", [event_id],
            ["name", "partner_ids", "start", "appointment_type_id"],
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Error leyendo la cita: {exc}"
        _log_call("cancel_appointment", tenant_id, log_args, None, msg,
                  int((time.time() - started) * 1000))
        return {"success": False, "error_code": "read_failed",
                "error_detail": msg}

    if not rows:
        return {
            "success": False,
            "error_code": "appointment_not_found",
            "error_detail": f"No existe la cita id={event_id}.",
        }

    event = rows[0]
    attendees = [int(x) for x in (event.get("partner_ids") or [])
                 if isinstance(x, int)]
    if partner_id not in attendees:
        # Authorization failure — never cancel someone else's booking.
        return {
            "success": False,
            "error_code": "not_authorized",
            "error_detail": (
                "Esa cita no está a tu nombre, así que no puedo "
                "cancelarla."
            ),
        }

    # Prefer unlink; fall back to deactivation if ACL blocks it.
    method_used = "unlink"
    try:
        odoo_call_method(
            tenant_id, url, db, user, password,
            "calendar.event", "unlink", [event_id],
        )
    except Exception as unlink_exc:  # noqa: BLE001
        logger.warning(
            "cancel_appointment: unlink failed (%s), trying active=False",
            unlink_exc,
        )
        try:
            odoo_call_method(
                tenant_id, url, db, user, password,
                "calendar.event", "write", [event_id], args=[{"active": False}],
            )
            method_used = "deactivate"
        except Exception as exc:  # noqa: BLE001
            msg = f"No pude cancelar la cita: {exc}"
            _log_call("cancel_appointment", tenant_id, log_args, None, msg,
                      int((time.time() - started) * 1000))
            return {"success": False, "error_code": "cancel_failed",
                    "error_detail": msg}

    result = {
        "success": True,
        "event_id": event_id,
        "partner_id": partner_id,
        "method": method_used,
        "service": (
            _flatten_m2o(event.get("appointment_type_id")) or {}
        ).get("name") or event.get("name") or "",
    }
    _log_call("cancel_appointment", tenant_id, log_args,
              {"method": method_used}, None,
              int((time.time() - started) * 1000))
    return result


__all__ = [
    "list_services",
    "get_availability",
    "book_appointment",
    "list_my_appointments",
    "cancel_appointment",
]
