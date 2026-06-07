"""WhatsApp/Telegram formatters for appointment / booking tools.

Same contract as ``mcp_odoo.formatters.whatsapp``:
  * never emit pipes ``|``, triple dashes ``---`` or tabs
  * ``*bold*`` / ``_italic_`` only
  * one blank line between items
  * prices via ``_fmt_total`` (BPE-safe thousands separator)

These formatters never raise — a render error must not break a successful
tool call. Caller (``_attach_display_text``) swallows exceptions anyway.
"""
from __future__ import annotations

from typing import Any

from mcp_odoo.formatters.whatsapp import _fmt_total


def _fmt_local_dt(raw: Any) -> str:
    """Render 'YYYY-MM-DD HH:MM' as 'DD-MM HH:MM'. Best-effort."""
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip()
    try:
        d, t = s.split(" ", 1)
        y, mo, da = d.split("-")
        return f"{da}-{mo} {t[:5]}"
    except Exception:  # noqa: BLE001
        return s


def format_services_list(result: dict | None) -> str:
    """Render ``list_services`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        return "💅 No pude cargar los servicios en este momento."
    services = result.get("services") or []
    if not services:
        return "💅 No tengo servicios registrados por ahora."

    lines: list[str] = ["💅 *Estos son nuestros servicios:*"]
    for s in services:
        name = (s.get("name") or "").strip() or "(sin nombre)"
        dur = (s.get("duration_label") or "").strip()
        price = s.get("price")
        bits = [f"*{name}*"]
        if price is not None:
            bits.append(_fmt_total(price))
        head = " · ".join(bits)
        if dur:
            head += f" · _{dur}_"
        lines.append(head)
    lines.append(
        "_Dime qué servicio te interesa y para qué día, y te muestro los "
        "horarios disponibles._"
    )
    return "\n\n".join(lines)


def format_availability(result: dict | None) -> str:
    """Render ``get_availability`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        if isinstance(result, dict):
            detail = (result.get("error_detail") or "").strip()
            if detail:
                return f"📅 {detail}"
        return "📅 No pude consultar la disponibilidad."

    service = (result.get("service") or "").strip()
    dur = (result.get("duration_label") or "").strip()
    days = result.get("days") or []

    if not days:
        head = f"📅 No encontré horarios libres para *{service}*" if service \
            else "📅 No encontré horarios libres"
        return head + " en los próximos días. ¿Quieres que revise más adelante?"

    title = f"📅 *Horarios disponibles para {service}*" if service \
        else "📅 *Horarios disponibles*"
    lines: list[str] = [title]
    if dur:
        lines.append(f"_Duración: {dur}_")

    for day in days:
        weekday = (day.get("weekday") or "").strip()
        date_iso = (day.get("date") or "").strip()
        date_disp = date_iso
        try:
            y, mo, da = date_iso.split("-")
            date_disp = f"{da}-{mo}"
        except Exception:  # noqa: BLE001
            pass
        header = f"*{weekday} {date_disp}*".strip()
        slots = day.get("slots") or []
        labels = [str(s.get("label") or "").strip() for s in slots if s.get("label")]
        if labels:
            lines.append(f"{header}: " + " · ".join(labels))

    lines.append(
        "_Dime la hora que prefieres (ej.: «el viernes a las 10:00») y la "
        "agendo._"
    )
    return "\n\n".join(lines)


def format_booking_confirmation(result: dict | None) -> str:
    """Render ``book_appointment`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        if isinstance(result, dict):
            detail = (result.get("error_detail") or "").strip()
            if detail:
                return f"⚠️ {detail}"
        return "⚠️ No pude agendar la cita."

    service = (result.get("service") or "").strip()
    weekday = (result.get("weekday") or "").strip()
    start_local = _fmt_local_dt(result.get("start_local"))
    dur = (result.get("duration_label") or "").strip()
    event_id = result.get("event_id")

    pending_deposit = bool(result.get("pending_deposit"))

    head = (
        "📝 *¡Tu cita quedó reservada (por confirmar)!*"
        if pending_deposit else "✅ *¡Tu cita quedó agendada!*"
    )
    lines: list[str] = [head]
    detail_bits: list[str] = []
    if service:
        detail_bits.append(f"Servicio: *{service}*")
    when = " ".join(b for b in [weekday, start_local] if b).strip()
    if when:
        detail_bits.append(f"Fecha: *{when}*")
    if dur:
        detail_bits.append(f"Duración: {dur}")
    if detail_bits:
        lines.append("\n".join(detail_bits))
    if event_id is not None:
        lines.append(f"_Referencia de tu cita: #{event_id}._")
    if pending_deposit:
        lines.append(
            "Tu cita queda *por confirmar* hasta que verifiquemos el "
            "anticipo del *50% (no reembolsable)* por transferencia. "
            "Te enviaré los datos de pago; cuando transfieras, envíanos "
            "el comprobante para confirmarla."
        )
    else:
        lines.append("_Te enviaremos un recordatorio antes de tu cita._")
    return "\n\n".join(lines)


def format_payment_info(result: dict | None) -> str:
    """Render ``get_payment_info`` envelope for chat channels.

    Chat-safe (no pipes/tables): bank details + 50% non-refundable
    deposit reminder + send-proof instruction.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return "💳 No pude cargar los datos de pago en este momento."

    bank = (result.get("bank") or "").strip()
    account_type = (result.get("account_type") or "").strip()
    account_number = (result.get("account_number") or "").strip()
    holder = (result.get("holder") or "").strip()
    holder_id = (result.get("holder_id") or "").strip()

    lines: list[str] = ["💳 *Datos para tu anticipo*"]
    bank_bits: list[str] = []
    if bank:
        bank_bits.append(f"Banco: *{bank}*")
    if account_type:
        bank_bits.append(f"Tipo de cuenta: {account_type}")
    if account_number:
        bank_bits.append(f"N° de cuenta: *{account_number}*")
    if holder:
        bank_bits.append(f"Titular: {holder}")
    if holder_id:
        bank_bits.append(f"Cédula: {holder_id}")
    if bank_bits:
        lines.append("\n".join(bank_bits))

    lines.append(
        "El anticipo es del *50% (no reembolsable)* por transferencia. "
        "Cuando hagas el pago, envíanos el *comprobante* para confirmar "
        "tu cita."
    )
    return "\n\n".join(lines)


def format_location_info(result: dict | None) -> str:
    """Render ``get_location_info`` envelope for chat channels.

    Chat-safe (no pipes/tables): address + hours + contact + invitation
    to visit.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return "📍 No pude cargar nuestra ubicación en este momento."

    address = (result.get("address") or "").strip()
    hours = (result.get("hours") or "").strip()
    phone = (result.get("phone") or "").strip()
    instagram = (result.get("instagram") or "").strip()

    lines: list[str] = ["📍 *Nuestra ubicación — Afrodita Studio*"]
    info_bits: list[str] = []
    if address:
        info_bits.append(f"Dirección: *{address}*")
    if hours:
        info_bits.append(f"Horario de atención: {hours}")
    if phone:
        info_bits.append(f"Teléfono: {phone}")
    if instagram:
        info_bits.append(f"Instagram: {instagram}")
    if info_bits:
        lines.append("\n".join(info_bits))

    lines.append("_¡Te esperamos para consentirte! 💅_")
    return "\n\n".join(lines)


def format_my_appointments(result: dict | None) -> str:
    """Render ``list_my_appointments`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        return "📅 No pude consultar tus citas."
    appts = result.get("appointments") or []
    if not appts:
        return "📅 No tienes citas próximas agendadas."

    n = len(appts)
    head = "📅 *Tu próxima cita:*" if n == 1 else f"📅 *Tus {n} próximas citas:*"
    lines: list[str] = [head]
    for a in appts:
        service = (a.get("service") or "").strip() or "(servicio)"
        weekday = (a.get("weekday") or "").strip()
        start_local = _fmt_local_dt(a.get("start_local"))
        event_id = a.get("event_id")
        when = " ".join(b for b in [weekday, start_local] if b).strip()
        row = f"*{service}*"
        if when:
            row += f" · {when}"
        if event_id is not None:
            row += f" · _#{event_id}_"
        lines.append(row)
    lines.append(
        "_Si quieres cancelar una, dime el número de referencia (ej.: «cancela "
        "la #12»)._"
    )
    return "\n\n".join(lines)


def format_cancellation(result: dict | None) -> str:
    """Render ``cancel_appointment`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        if isinstance(result, dict):
            detail = (result.get("error_detail") or "").strip()
            if detail:
                return f"⚠️ {detail}"
        return "⚠️ No pude cancelar la cita."
    service = (result.get("service") or "").strip()
    if service:
        return f"✅ Listo, cancelé tu cita de *{service}*. ¿Quieres reagendar?"
    return "✅ Listo, cancelé tu cita. ¿Quieres reagendar?"


__all__ = [
    "format_services_list",
    "format_availability",
    "format_booking_confirmation",
    "format_my_appointments",
    "format_cancellation",
    "format_payment_info",
    "format_location_info",
]
