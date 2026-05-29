"""Minimal Telegram Bot API client for direct file delivery from tools.

Some tools (notably ``generate_sales_report``) need to push a generated
artifact straight to a Telegram chat instead of bouncing it through the
agent as base64.  This module wraps the ``sendDocument`` endpoint with a
small async helper so callers can do::

    from mcp_theos.telegram_delivery import send_document
    await send_document(chat_id="-5248384291", filename="x.xlsx", data=b"...")

Configuration:
    LILA_TELEGRAM_BOT_TOKEN   Bot token used for outbound delivery.
                              Falls back to TELEGRAM_BOT_TOKEN for compat
                              with environments where only one bot exists.

Notes:
- Telegram limits documents to 50 MB.  We raise ``DocumentTooLarge`` when
  the payload exceeds that limit so the caller can choose to fail loudly
  vs. silently truncate.
- Uses ``httpx`` because the rest of the MCP already depends on it; no
  extra deps.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MAX_DOC_SIZE_BYTES = 50 * 1024 * 1024
_WARN_DOC_SIZE_BYTES = 45 * 1024 * 1024
_API_BASE = "https://api.telegram.org"


class BotTokenMissing(RuntimeError):
    """Raised when no ``LILA_TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_BOT_TOKEN``
    is configured in the MCP container env. Caller should report the
    configuration error to the operator rather than silently swallowing."""


class DocumentTooLarge(ValueError):
    """File exceeds Telegram's 50 MB sendDocument limit."""


class TelegramAPIError(RuntimeError):
    """Telegram API replied with ``ok: false`` — payload included."""


def _get_bot_token() -> str:
    token = (
        os.environ.get("LILA_TELEGRAM_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    if not token:
        raise BotTokenMissing(
            "No Telegram bot token configured. Set LILA_TELEGRAM_BOT_TOKEN "
            "on the niko-mcp-theos container env so the MCP can deliver "
            "files directly to Telegram chats."
        )
    return token


async def send_document(
    *,
    chat_id: str,
    filename: str,
    data: bytes,
    caption: str | None = None,
    parse_mode: str | None = "HTML",
    message_thread_id: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Upload ``data`` as a Telegram document to ``chat_id``.

    Raises:
        BotTokenMissing:   No bot token configured in env.
        DocumentTooLarge:  Payload > 50 MB.
        TelegramAPIError:  Telegram replied ``ok: false`` (chat not found,
                           bot kicked, etc.). Payload included in args.
        httpx.HTTPError:   Network-level failures (timeout, DNS, etc.).
    """
    if len(data) > _MAX_DOC_SIZE_BYTES:
        raise DocumentTooLarge(
            f"file {filename} weighs {len(data) / 1024 / 1024:.1f} MB — "
            "Telegram's sendDocument limit is 50 MB"
        )
    if len(data) > _WARN_DOC_SIZE_BYTES:
        logger.warning(
            "telegram_delivery: %s is %.1f MB — close to Telegram's 50 MB limit",
            filename, len(data) / 1024 / 1024,
        )

    token = _get_bot_token()
    url = f"{_API_BASE}/bot{token}/sendDocument"

    fields: list[tuple[str, str | tuple[str, bytes, str]]] = [
        ("chat_id", str(chat_id)),
    ]
    if caption:
        fields.append(("caption", caption))
        if parse_mode:
            fields.append(("parse_mode", parse_mode))
    if message_thread_id:
        fields.append(("message_thread_id", str(message_thread_id)))

    files = {
        "document": (
            filename,
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }
    data_fields = dict(fields)

    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(url, data=data_fields, files=files)
    try:
        body = resp.json()
    except Exception:
        body = {"ok": False, "raw_status": resp.status_code,
                "raw_text": resp.text[:500]}

    if not body.get("ok"):
        # Don't leak the bot token if Telegram echoes the request URL.
        raise TelegramAPIError(
            f"Telegram rejected sendDocument: "
            f"{body.get('description') or json.dumps(body)[:300]}"
        )
    return body.get("result") or body


async def send_photo(
    *,
    chat_id: str,
    data: bytes,
    filename: str = "chart.png",
    caption: str | None = None,
    parse_mode: str | None = "HTML",
    message_thread_id: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Upload an image to a Telegram chat (Bot API ``sendPhoto``).

    Used by the dashboard-chart tool to surface the visual summary.
    Telegram caps inline-rendered photos at 10 MB; if the PNG is bigger
    the caller should fall back to ``send_document``.
    """
    if len(data) > 10 * 1024 * 1024:
        # Fallback: photos > 10 MB get sent as a file (no inline preview).
        return await send_document(
            chat_id=chat_id, filename=filename, data=data,
            caption=caption, parse_mode=parse_mode,
            message_thread_id=message_thread_id, timeout=timeout,
        )
    token = _get_bot_token()
    url = f"{_API_BASE}/bot{token}/sendPhoto"
    fields: list[tuple[str, str]] = [("chat_id", str(chat_id))]
    if caption:
        fields.append(("caption", caption))
        if parse_mode:
            fields.append(("parse_mode", parse_mode))
    if message_thread_id:
        fields.append(("message_thread_id", str(message_thread_id)))
    files = {"photo": (filename, data, "image/png")}
    data_fields = dict(fields)
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(url, data=data_fields, files=files)
    try:
        body = resp.json()
    except Exception:
        body = {"ok": False, "raw_status": resp.status_code,
                "raw_text": resp.text[:500]}
    if not body.get("ok"):
        raise TelegramAPIError(
            f"Telegram rejected sendPhoto: "
            f"{body.get('description') or json.dumps(body)[:300]}"
        )
    return body.get("result") or body


async def send_message(
    *,
    chat_id: str,
    text: str,
    parse_mode: str | None = "HTML",
    message_thread_id: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Send a plain text message to a Telegram chat.

    Used by background tasks that finish long after the originating
    ``tools/call`` returned — they need to notify the chat directly
    (success or failure) without going through the agent.
    """
    token = _get_bot_token()
    url = f"{_API_BASE}/bot{token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if message_thread_id:
        payload["message_thread_id"] = str(message_thread_id)
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(url, json=payload)
    try:
        body = resp.json()
    except Exception:
        body = {"ok": False, "raw_status": resp.status_code,
                "raw_text": resp.text[:500]}
    if not body.get("ok"):
        raise TelegramAPIError(
            f"Telegram rejected sendMessage: "
            f"{body.get('description') or json.dumps(body)[:300]}"
        )
    return body.get("result") or body
