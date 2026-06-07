"""WhatsApp/Telegram formatters for deposit / proof-of-payment tools.

Same contract as ``mcp_odoo.formatters.whatsapp_appointments``:
  * never emit pipes ``|``, triple dashes ``---`` or tabs
  * ``*bold*`` / ``_italic_`` only
  * one blank line between items

These formatters never raise — a render error must not break a successful
tool call. Caller (``_attach_display_text``) swallows exceptions anyway.
"""
from __future__ import annotations

from typing import Any


def format_deposit_received(result: dict | None) -> str:
    """Render ``register_deposit_proof`` envelope for chat channels.

    On success: a reassuring "we got your receipt, verifying" message.
    On failure: the friendly Spanish error detail.
    """
    if not isinstance(result, dict) or not result.get("success"):
        if isinstance(result, dict):
            detail = (result.get("error_detail") or "").strip()
            if detail:
                return f"⚠️ {detail}"
        return "⚠️ No pude registrar tu comprobante. ¿Lo puedes reenviar?"

    return (
        "📩 *¡Recibí tu comprobante!* Lo estamos verificando con el "
        "personal; te confirmo tu cita apenas lo validen. 💅"
    )


def format_appointment_confirmed(result: dict | None) -> str:
    """Render ``confirm_appointment`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        if isinstance(result, dict):
            detail = (result.get("error_detail") or "").strip()
            if detail:
                return f"⚠️ {detail}"
        return "⚠️ No pude confirmar la cita."

    return (
        "✅ *¡Tu cita quedó confirmada!* Verificamos tu anticipo. "
        "Te esperamos. 💅"
    )


def format_appointment_released(result: dict | None) -> str:
    """Render ``release_appointment`` envelope for chat channels."""
    if not isinstance(result, dict) or not result.get("success"):
        if isinstance(result, dict):
            detail = (result.get("error_detail") or "").strip()
            if detail:
                return f"⚠️ {detail}"
        return "⚠️ No pude liberar la cita."

    return (
        "🗓️ Liberamos ese horario porque no se confirmó el anticipo a "
        "tiempo. Cuando quieras, te ayudo a agendar de nuevo."
    )


def format_pending_deposit_list(result: dict | None) -> str:
    """Render ``list_pending_deposit_appointments`` envelope for chat.

    Mostly an internal/staff view, but kept chat-safe so it can be surfaced
    if ever needed.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return "📋 No pude consultar las citas por confirmar."
    appts = result.get("appointments") or []
    if not appts:
        return "📋 No hay citas por confirmar pendientes de anticipo."

    n = len(appts)
    head = (
        "📋 *1 cita por confirmar (sin anticipo):*" if n == 1
        else f"📋 *{n} citas por confirmar (sin anticipo):*"
    )
    lines: list[str] = [head]
    for a in appts:
        name = (a.get("name") or "").strip() or "(sin nombre)"
        event_id = a.get("event_id")
        row = f"*{name}*"
        if event_id is not None:
            row += f" · _#{event_id}_"
        lines.append(row)
    return "\n\n".join(lines)


__all__ = [
    "format_deposit_received",
    "format_appointment_confirmed",
    "format_appointment_released",
    "format_pending_deposit_list",
]
