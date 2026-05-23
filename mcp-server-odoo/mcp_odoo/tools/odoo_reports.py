"""Odoo PDF report fetcher — ETA iter 81.

Owner feedback (WhatsApp 2026-05-23): "obtener el email desde Odoo y
pasarlo a PDF así la información sería la correcta y oficial de Odoo".

This module reuses the OFFICIAL Odoo HTTP report endpoint
(``/report/pdf/<xmlid>/<csv_ids>``) so the documents we deliver to the
customer are byte-identical to the ones the Odoo cron mails out every
fortnight. Duplicating the rendering in Niko would cause numeric drift
the second a column changes in the QWeb template — keep the source of
truth on Odoo.

Authentication flow (verified against ERP Tecnosmart 2026-05-23):

  1. ``POST {odoo_url}/web/session/authenticate``
     body = ``{"params": {"db": ..., "login": ..., "password": ...}}``
     → response cookie ``session_id=<sid>; HttpOnly``.
  2. ``GET {odoo_url}/report/pdf/<xmlid>/<csv_ids>``
     with ``Cookie: session_id=<sid>`` → ``application/pdf``.

We cache the ``session_id`` per-tenant in a module-level dict. The
cookie lifetime is owned by Odoo (typically 12h) but we conservatively
re-login after 8h. On 401/403 the cache for that tenant is invalidated
and the call retried once.

The helper is fully synchronous + uses ``requests`` because the rest of
``mcp_odoo.tools.*`` is sync (xmlrpc) and we want a uniform call site.
The MCP transport handler can wrap it in ``run_in_executor`` if needed.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

logger = logging.getLogger("mcp_odoo.odoo_reports")


# ---------------------------------------------------------------------------
# Per-tenant session cache
# ---------------------------------------------------------------------------
# ``_session_cache[tenant_id] = (session_id, expires_at_epoch_seconds)``.
# Module-level dict; lost on container restart (login costs <500ms so the
# cold start penalty is acceptable). Guarded by a lock because the MCP
# transport may dispatch multiple report calls concurrently.
_session_cache: dict[str, tuple[str, float]] = {}
_session_lock = threading.Lock()

# 8 hours — well below Odoo's default 12h session.gc_timeout.
SESSION_TTL_SECONDS = 8 * 60 * 60

# Default timeouts (seconds). Reports can take several seconds to render
# on Odoo when the partner has hundreds of invoices, so the read timeout
# is generous.
_LOGIN_TIMEOUT = 10.0
_REPORT_TIMEOUT = 60.0


class OdooReportError(RuntimeError):
    """Raised when the Odoo report endpoint cannot fulfil the request."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _login(
    *,
    odoo_url: str,
    odoo_db: str,
    odoo_user: str,
    odoo_password: str,
) -> str:
    """Authenticate against Odoo's ``/web/session/authenticate`` endpoint.

    Returns the ``session_id`` cookie value. Raises ``OdooReportError``
    on transport / credentials failures.
    """
    base = (odoo_url or "").rstrip("/")
    if not base:
        raise OdooReportError("odoo_url_missing", "odoo_url no configurada")
    url = f"{base}/web/session/authenticate"
    payload = {
        "jsonrpc": "2.0",
        "params": {
            "db": odoo_db,
            "login": odoo_user,
            "password": odoo_password,
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=_LOGIN_TIMEOUT)
    except requests.RequestException as exc:  # network / DNS / TLS
        raise OdooReportError(
            "odoo_login_transport", f"No pude conectar a Odoo: {exc}",
        ) from exc

    if resp.status_code != 200:
        raise OdooReportError(
            "odoo_login_http",
            f"Odoo respondio HTTP {resp.status_code} al autenticar",
        )

    # Odoo returns 200 even on failed credentials — the body carries the
    # error. A successful login also returns a Set-Cookie header.
    try:
        body = resp.json()
    except ValueError as exc:
        raise OdooReportError(
            "odoo_login_body", f"Respuesta no JSON al autenticar: {exc}",
        ) from exc

    err = body.get("error")
    if err:
        # Typical error: invalid credentials, database not found.
        raise OdooReportError(
            "odoo_login_failed",
            str(err.get("data", {}).get("message") or err.get("message") or err),
        )

    sid = resp.cookies.get("session_id")
    if not sid:
        # Some Odoo proxies strip cookies but expose the uid in the
        # body — without the cookie we still cannot reuse the session.
        raise OdooReportError(
            "odoo_login_no_cookie",
            "Odoo no devolvio session_id (revise proxy/cookie domain).",
        )
    return sid


def _get_session(
    *,
    tenant_id: str,
    odoo_url: str,
    odoo_db: str,
    odoo_user: str,
    odoo_password: str,
    force_refresh: bool = False,
) -> str:
    """Return a valid ``session_id`` for the tenant, refreshing if needed.

    Thread-safe via ``_session_lock``. ``force_refresh=True`` skips the
    cache (used when a previous call returned 401/403).
    """
    now = time.time()
    with _session_lock:
        if not force_refresh:
            cached = _session_cache.get(tenant_id)
            if cached is not None:
                sid, expires_at = cached
                if expires_at > now:
                    return sid
        sid = _login(
            odoo_url=odoo_url,
            odoo_db=odoo_db,
            odoo_user=odoo_user,
            odoo_password=odoo_password,
        )
        _session_cache[tenant_id] = (sid, now + SESSION_TTL_SECONDS)
        return sid


def _invalidate_session(tenant_id: str) -> None:
    """Drop the cached session for the tenant. Next call will re-login."""
    with _session_lock:
        _session_cache.pop(tenant_id, None)


def fetch_odoo_report_pdf(
    *,
    tenant_id: str,
    odoo_url: str,
    odoo_db: str,
    odoo_user: str,
    odoo_password: str,
    report_xmlid: str,
    res_ids: list[int],
) -> tuple[bytes, str]:
    """Fetch the official Odoo PDF for ``report_xmlid`` over ``res_ids``.

    Returns ``(pdf_bytes, content_type)``. ``content_type`` is taken
    verbatim from Odoo's response header (typically
    ``application/pdf``). Raises ``OdooReportError`` on:

      * transport / network failure
      * authentication failure (after one retry with a fresh session)
      * Odoo returns a non-2xx status
      * Odoo returns a body whose content-type is not ``application/pdf``
        (Odoo serves an HTML error page when the xmlid is wrong or the
        user has no access — we refuse to forward HTML to the customer).

    The function is intentionally narrow: it does NOT touch Supabase,
    log calls, or persist the PDF. The MCP handler is responsible for
    saving the bytes to the shared volume and building the public URL.
    """
    if not report_xmlid or "." not in report_xmlid:
        raise OdooReportError(
            "invalid_xmlid",
            f"report_xmlid debe tener formato 'module.report_name' (recibido: {report_xmlid!r})",
        )
    if not res_ids:
        raise OdooReportError("invalid_ids", "res_ids vacio")
    # Normalise + de-dup ids while preserving order.
    seen: set[int] = set()
    ordered: list[int] = []
    for rid in res_ids:
        try:
            i = int(rid)
        except (TypeError, ValueError):
            raise OdooReportError(
                "invalid_ids",
                f"res_id no entero: {rid!r}",
            )
        if i <= 0:
            raise OdooReportError("invalid_ids", f"res_id <= 0: {i}")
        if i in seen:
            continue
        seen.add(i)
        ordered.append(i)

    base = (odoo_url or "").rstrip("/")
    csv_ids = ",".join(str(i) for i in ordered)
    pdf_url = f"{base}/report/pdf/{report_xmlid}/{csv_ids}"

    # First attempt with cached session; on 401/403 retry once after
    # invalidating the cache.
    for attempt in (1, 2):
        sid = _get_session(
            tenant_id=tenant_id,
            odoo_url=odoo_url,
            odoo_db=odoo_db,
            odoo_user=odoo_user,
            odoo_password=odoo_password,
            force_refresh=(attempt == 2),
        )
        try:
            resp = requests.get(
                pdf_url,
                cookies={"session_id": sid},
                timeout=_REPORT_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise OdooReportError(
                "odoo_report_transport",
                f"No pude generar el PDF en Odoo: {exc}",
            ) from exc

        # 302 to /web/login means the session expired underneath us.
        if resp.status_code in (302, 303):
            location = resp.headers.get("Location", "")
            if "/web/login" in location and attempt == 1:
                _invalidate_session(tenant_id)
                continue
            raise OdooReportError(
                "odoo_report_redirect",
                f"Odoo redirigio a {location!r} (revise credenciales).",
            )

        if resp.status_code in (401, 403):
            if attempt == 1:
                _invalidate_session(tenant_id)
                continue
            raise OdooReportError(
                "odoo_report_unauthorized",
                f"Odoo nego el acceso al reporte (HTTP {resp.status_code}).",
            )

        if resp.status_code != 200:
            # 404 = xmlid no existe en este Odoo / o el id no es valido
            # para el modelo del reporte; 500 = error de plantilla.
            raise OdooReportError(
                "odoo_report_http",
                f"Odoo HTTP {resp.status_code} al generar reporte {report_xmlid}.",
            )

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/pdf" not in ctype:
            # Odoo serves an HTML error page when the controller fails
            # silently. Surface a typed error instead of leaking HTML.
            raise OdooReportError(
                "odoo_report_not_pdf",
                f"Odoo devolvio {ctype!r} (esperado application/pdf). "
                f"Posible causa: el reporte no aplica a estos ids.",
            )

        body = resp.content
        if not body or not body.startswith(b"%PDF"):
            raise OdooReportError(
                "odoo_report_corrupt",
                "Odoo devolvio un PDF vacio o corrupto.",
            )
        return body, "application/pdf"

    # Unreachable — the loop always returns or raises.
    raise OdooReportError("odoo_report_unreachable", "loop ended unexpectedly")


def _coerce_str(value: Any) -> str:
    """Helper: best-effort coerce arbitrary Odoo field values to str."""
    if value is None or value is False:
        return ""
    return str(value)


__all__ = [
    "OdooReportError",
    "fetch_odoo_report_pdf",
    "_session_cache",  # exposed for tests
    "_invalidate_session",
    "SESSION_TTL_SECONDS",
]
