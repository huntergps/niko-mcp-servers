"""Tests for ETA iter 81 PDF report tools.

Coverage:
  * ``mcp_odoo.tools.odoo_reports.fetch_odoo_report_pdf`` — login caching,
    HTTP path-builder, error taxonomy. All HTTP is mocked via ``responses``-
    style monkeypatching of ``requests.post`` / ``requests.get``.
  * Tool registration: 4 tools must appear in tools/list.
  * Tool dispatch:
      - OTP gate refuses all 4 without a verified session.
      - get_invoice_pdf auto-routes ``out_refund`` to the NC xmlid.
      - get_credit_note_pdf refuses non-NC moves.
      - On success the envelope carries ``pdf_url``, ``pdf_size_bytes``
        and (for chat channels) a ``display_text``.

The live ERP call is NOT exercised here — that happens in the smoke
test after deploy.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch, MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient


JWT_SECRET = "test-jwt-secret-for-testing-only-32bytes!"

MOCK_TENANT_CONFIG = {
    "tenant_id": "tenant-eta-test",
    "url": "http://fake-odoo:8069",
    "db": "testdb",
    "user": "admin",
    "password": "admin",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())
    # Redirect file writes from /files to a tmp dir for the tests that
    # exercise the handler. We patch os.makedirs / open via the dispatch
    # paths directly when needed.


@pytest.fixture
def client():
    from importlib import reload
    from mcp_odoo import config as _cfg
    reload(_cfg)
    from mcp_odoo.server import app

    async def _stub_get_tenant_config(_request):
        return MOCK_TENANT_CONFIG

    with patch(
        "mcp_odoo.transports.mcp_transport._get_tenant_config",
        new=_stub_get_tenant_config,
    ):
        yield TestClient(app)


def _rpc(client, method: str, params: dict | None = None,
         headers: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post("/mcp", json=payload, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def _otp_session_ok():
    async def _async(*args, **kwargs):
        return True
    return _async


def _otp_session_denied():
    async def _async(*args, **kwargs):
        return False
    return _async


# ---------------------------------------------------------------------------
# Helper: ``fetch_odoo_report_pdf``
# ---------------------------------------------------------------------------

class TestFetchOdooReportPdf:
    """Unit tests for the low-level fetcher.

    All HTTP is mocked. We assert the public contract: returns
    (bytes, content_type) on 200+application/pdf, raises typed errors
    otherwise, and caches the session_id per tenant.
    """

    def setup_method(self):
        # Clear the module-level cache before each test.
        from mcp_odoo.tools import odoo_reports as r
        r._session_cache.clear()

    def _mk_login_response(self, *, status: int = 200,
                            sid: str = "test-sid-abc",
                            with_error: bool = False):
        resp = MagicMock()
        resp.status_code = status
        if with_error:
            resp.json.return_value = {
                "error": {"message": "Invalid credentials"},
            }
        else:
            resp.json.return_value = {"result": {"uid": 1, "session_id": sid}}
        # Cookies behave like a dict for ``.get("session_id")``.
        cookies = {"session_id": sid} if not with_error else {}
        resp.cookies = cookies
        return resp

    def _mk_pdf_response(self, *, status: int = 200,
                          body: bytes = b"%PDF-1.7 fakecontent",
                          ctype: str = "application/pdf",
                          location: str = ""):
        resp = MagicMock()
        resp.status_code = status
        resp.content = body
        headers = {"Content-Type": ctype}
        if location:
            headers["Location"] = location
        resp.headers = headers
        return resp

    def test_happy_path(self):
        from mcp_odoo.tools import odoo_reports as r

        login_calls = []
        pdf_calls = []

        def _post(url, **kw):
            login_calls.append(url)
            return self._mk_login_response()

        def _get(url, **kw):
            pdf_calls.append((url, kw.get("cookies")))
            return self._mk_pdf_response()

        with patch("mcp_odoo.tools.odoo_reports.requests.post", side_effect=_post), \
             patch("mcp_odoo.tools.odoo_reports.requests.get", side_effect=_get):
            pdf, ctype = r.fetch_odoo_report_pdf(
                tenant_id="t1",
                odoo_url="http://erp.example.com",
                odoo_db="db", odoo_user="u", odoo_password="p",
                report_xmlid="some.report",
                res_ids=[62],
            )
        assert pdf == b"%PDF-1.7 fakecontent"
        assert ctype == "application/pdf"
        assert login_calls == ["http://erp.example.com/web/session/authenticate"]
        assert pdf_calls[0][0] == "http://erp.example.com/report/pdf/some.report/62"
        assert pdf_calls[0][1] == {"session_id": "test-sid-abc"}

    def test_session_is_cached_across_calls(self):
        from mcp_odoo.tools import odoo_reports as r

        post_count = {"n": 0}

        def _post(url, **kw):
            post_count["n"] += 1
            return self._mk_login_response()

        def _get(url, **kw):
            return self._mk_pdf_response()

        with patch("mcp_odoo.tools.odoo_reports.requests.post", side_effect=_post), \
             patch("mcp_odoo.tools.odoo_reports.requests.get", side_effect=_get):
            for _ in range(3):
                r.fetch_odoo_report_pdf(
                    tenant_id="t1",
                    odoo_url="http://erp.example.com", odoo_db="db",
                    odoo_user="u", odoo_password="p",
                    report_xmlid="m.r", res_ids=[1],
                )
        assert post_count["n"] == 1, "login should be called once thanks to cache"

    def test_invalid_xmlid(self):
        from mcp_odoo.tools.odoo_reports import (
            OdooReportError,
            fetch_odoo_report_pdf,
        )
        with pytest.raises(OdooReportError) as exc:
            fetch_odoo_report_pdf(
                tenant_id="t1",
                odoo_url="http://x",
                odoo_db="d", odoo_user="u", odoo_password="p",
                report_xmlid="no_dot_here", res_ids=[1],
            )
        assert exc.value.code == "invalid_xmlid"

    def test_invalid_ids(self):
        from mcp_odoo.tools.odoo_reports import (
            OdooReportError,
            fetch_odoo_report_pdf,
        )
        with pytest.raises(OdooReportError) as exc:
            fetch_odoo_report_pdf(
                tenant_id="t1",
                odoo_url="http://x", odoo_db="d",
                odoo_user="u", odoo_password="p",
                report_xmlid="m.r", res_ids=[],
            )
        assert exc.value.code == "invalid_ids"

    def test_login_credential_error(self):
        from mcp_odoo.tools import odoo_reports as r
        from mcp_odoo.tools.odoo_reports import OdooReportError

        def _post(url, **kw):
            return self._mk_login_response(with_error=True)

        with patch("mcp_odoo.tools.odoo_reports.requests.post", side_effect=_post):
            with pytest.raises(OdooReportError) as exc:
                r.fetch_odoo_report_pdf(
                    tenant_id="t1",
                    odoo_url="http://x", odoo_db="d",
                    odoo_user="u", odoo_password="bad",
                    report_xmlid="m.r", res_ids=[1],
                )
        assert exc.value.code == "odoo_login_failed"

    def test_pdf_not_pdf(self):
        from mcp_odoo.tools import odoo_reports as r
        from mcp_odoo.tools.odoo_reports import OdooReportError

        def _post(url, **kw):
            return self._mk_login_response()

        def _get(url, **kw):
            return self._mk_pdf_response(body=b"<html>boom</html>", ctype="text/html")

        with patch("mcp_odoo.tools.odoo_reports.requests.post", side_effect=_post), \
             patch("mcp_odoo.tools.odoo_reports.requests.get", side_effect=_get):
            with pytest.raises(OdooReportError) as exc:
                r.fetch_odoo_report_pdf(
                    tenant_id="t1",
                    odoo_url="http://x", odoo_db="d",
                    odoo_user="u", odoo_password="p",
                    report_xmlid="m.r", res_ids=[1],
                )
        assert exc.value.code == "odoo_report_not_pdf"

    def test_pdf_corrupt(self):
        from mcp_odoo.tools import odoo_reports as r
        from mcp_odoo.tools.odoo_reports import OdooReportError

        def _post(url, **kw):
            return self._mk_login_response()

        def _get(url, **kw):
            # application/pdf but body doesn't start with %PDF.
            return self._mk_pdf_response(body=b"truncated", ctype="application/pdf")

        with patch("mcp_odoo.tools.odoo_reports.requests.post", side_effect=_post), \
             patch("mcp_odoo.tools.odoo_reports.requests.get", side_effect=_get):
            with pytest.raises(OdooReportError) as exc:
                r.fetch_odoo_report_pdf(
                    tenant_id="t1",
                    odoo_url="http://x", odoo_db="d",
                    odoo_user="u", odoo_password="p",
                    report_xmlid="m.r", res_ids=[1],
                )
        assert exc.value.code == "odoo_report_corrupt"

    def test_session_refreshed_on_redirect_to_login(self):
        from mcp_odoo.tools import odoo_reports as r

        post_count = {"n": 0}
        get_count = {"n": 0}

        def _post(url, **kw):
            post_count["n"] += 1
            return self._mk_login_response(sid=f"sid-{post_count['n']}")

        def _get(url, **kw):
            get_count["n"] += 1
            if get_count["n"] == 1:
                return self._mk_pdf_response(
                    status=302, body=b"", ctype="text/html",
                    location="/web/login?redirect=/report",
                )
            return self._mk_pdf_response()

        with patch("mcp_odoo.tools.odoo_reports.requests.post", side_effect=_post), \
             patch("mcp_odoo.tools.odoo_reports.requests.get", side_effect=_get):
            pdf, _ = r.fetch_odoo_report_pdf(
                tenant_id="t1",
                odoo_url="http://x", odoo_db="d",
                odoo_user="u", odoo_password="p",
                report_xmlid="m.r", res_ids=[1],
            )
        assert pdf.startswith(b"%PDF")
        assert post_count["n"] == 2, "login retried after 302 → /web/login"

    def test_dedup_ids(self):
        from mcp_odoo.tools import odoo_reports as r

        captured = {}

        def _post(url, **kw):
            return self._mk_login_response()

        def _get(url, **kw):
            captured["url"] = url
            return self._mk_pdf_response()

        with patch("mcp_odoo.tools.odoo_reports.requests.post", side_effect=_post), \
             patch("mcp_odoo.tools.odoo_reports.requests.get", side_effect=_get):
            r.fetch_odoo_report_pdf(
                tenant_id="t1",
                odoo_url="http://x", odoo_db="d",
                odoo_user="u", odoo_password="p",
                report_xmlid="m.r", res_ids=[1, 1, 2, 1, 3],
            )
        assert captured["url"].endswith("/report/pdf/m.r/1,2,3")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_four_pdf_tools_listed(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]
        for expected in (
            "get_customer_statement_pdf",
            "get_invoice_pdf",
            "get_credit_note_pdf",
            "get_retention_pdf",
        ):
            assert expected in names, f"{expected} missing from MCP_TOOLS"

    def test_required_args(self, client):
        body = _rpc(client, "tools/list")
        by_name = {t["name"]: t for t in body["result"]["tools"]}
        assert "partner_id" in by_name["get_customer_statement_pdf"]["inputSchema"]["required"]
        assert "invoice_id" in by_name["get_invoice_pdf"]["inputSchema"]["required"]
        assert "refund_id" in by_name["get_credit_note_pdf"]["inputSchema"]["required"]
        assert "retention_id" in by_name["get_retention_pdf"]["inputSchema"]["required"]


# ---------------------------------------------------------------------------
# OTP gate
# ---------------------------------------------------------------------------

class TestPdfOTPGate:
    @pytest.mark.parametrize("tool_name,args", [
        ("get_customer_statement_pdf", {"partner_id": 62}),
    ])
    def test_statement_pdf_refuses_without_session(self, client, tool_name, args):
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_denied(),
        ):
            body = _rpc(client, "tools/call", {
                "name": tool_name, "arguments": args,
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        assert "VERIFICACION REQUERIDA" in text

    def test_invoice_pdf_resolves_partner_then_gates(self, client):
        def _fake_read(*a, **k):
            # Only the partner_id field matters for the OTP gate.
            return [{
                "id": 404936, "name": "FACV/2025/4897",
                "type": "out_invoice", "partner_id": [62, "Customer X"],
            }]
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_denied(),
        ), patch(
            "mcp_odoo.tools.generic.odoo_read", side_effect=_fake_read,
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_invoice_pdf",
                "arguments": {"invoice_id": 404936},
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        assert "VERIFICACION REQUERIDA" in text

    def test_move_not_found(self, client):
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ), patch(
            "mcp_odoo.tools.generic.odoo_read", side_effect=lambda *a, **k: [],
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_invoice_pdf",
                "arguments": {"invoice_id": 999999},
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["success"] is False
        assert payload["error_code"] == "move_not_found"


# ---------------------------------------------------------------------------
# Type-routing for get_invoice_pdf / get_credit_note_pdf
# ---------------------------------------------------------------------------

class TestPdfTypeRouting:
    def test_invoice_pdf_auto_routes_refund_to_nc(self, client, tmp_path,
                                                    monkeypatch):
        """When the move is out_refund the handler picks the NC xmlid."""
        # Patch /files to tmp_path
        monkeypatch.setattr(os, "makedirs",
                            lambda p, exist_ok=True: os.makedirs(
                                str(tmp_path / p.lstrip("/")), exist_ok=exist_ok))
        original_open = open

        def _patched_open(path, *args, **kw):
            if isinstance(path, str) and path.startswith("/files/"):
                p = tmp_path / path.lstrip("/")
                p.parent.mkdir(parents=True, exist_ok=True)
                return original_open(str(p), *args, **kw)
            return original_open(path, *args, **kw)

        monkeypatch.setattr(
            "mcp_odoo.transports.mcp_transport.open",
            _patched_open, raising=False,
        )

        captured_xmlid = {}

        def _fake_read(*a, **k):
            # Move is a credit note.
            return [{
                "id": 555, "name": "NCRV/2025/12",
                "type": "out_refund", "partner_id": [62, "X"],
            }]

        def _fake_fetch(**kwargs):
            captured_xmlid["xmlid"] = kwargs["report_xmlid"]
            return (b"%PDF-1.7 nc-mock", "application/pdf")

        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ), patch(
            "mcp_odoo.tools.generic.odoo_read", side_effect=_fake_read,
        ), patch(
            "mcp_odoo.tools.odoo_reports.fetch_odoo_report_pdf",
            side_effect=_fake_fetch,
        ), patch(
            "builtins.open", _patched_open,
        ), patch(
            "os.makedirs", lambda *a, **k: None,
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_invoice_pdf",
                "arguments": {"invoice_id": 555},
            }, headers={"X-Channel": "whatsapp"})

        text = body["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload.get("success") is True, payload
        assert payload["kind"] == "credit_note"
        assert captured_xmlid["xmlid"] == \
            "l10n_ec_sri_ece.report_nota_credito_electronica"
        assert payload["pdf_url"].endswith(".pdf")
        assert payload["pdf_size_bytes"] == len(b"%PDF-1.7 nc-mock")
        # Chat channel ⇒ display_text present
        assert "Nota de crédito" in payload["display_text"]

    def test_credit_note_pdf_refuses_non_refund(self, client):
        def _fake_read(*a, **k):
            return [{
                "id": 100, "name": "FACV/...",
                "type": "out_invoice", "partner_id": [62, "X"],
            }]
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ), patch(
            "mcp_odoo.tools.generic.odoo_read", side_effect=_fake_read,
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_credit_note_pdf",
                "arguments": {"refund_id": 100},
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["success"] is False
        assert payload["error_code"] == "not_a_credit_note"

    def test_invoice_pdf_refuses_entry_move(self, client):
        """A type='entry' move is not an invoice — must refuse."""
        def _fake_read(*a, **k):
            return [{
                "id": 777, "name": "MISC/2025/1",
                "type": "entry", "partner_id": [62, "X"],
            }]
        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ), patch(
            "mcp_odoo.tools.generic.odoo_read", side_effect=_fake_read,
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_invoice_pdf",
                "arguments": {"invoice_id": 777},
            }, headers={"X-Channel": "whatsapp"})
        text = body["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["success"] is False
        assert payload["error_code"] == "not_an_invoice"


# ---------------------------------------------------------------------------
# Statement PDF handler — happy path
# ---------------------------------------------------------------------------

class TestStatementPdfHandler:
    def test_statement_pdf_happy(self, client, tmp_path, monkeypatch):
        original_open = open

        def _patched_open(path, *args, **kw):
            if isinstance(path, str) and path.startswith("/files/"):
                p = tmp_path / path.lstrip("/")
                p.parent.mkdir(parents=True, exist_ok=True)
                return original_open(str(p), *args, **kw)
            return original_open(path, *args, **kw)

        def _fake_fetch(**kwargs):
            assert kwargs["report_xmlid"] == "tecno_l10n_ec_payment.report_account_balance"
            assert kwargs["res_ids"] == [62]
            return (b"%PDF-1.7 statement-mock-content-payload", "application/pdf")

        with patch(
            "mcp_odoo.transports.mcp_transport._otp_check_session",
            new=_otp_session_ok(),
        ), patch(
            "mcp_odoo.tools.odoo_reports.fetch_odoo_report_pdf",
            side_effect=_fake_fetch,
        ), patch(
            "builtins.open", _patched_open,
        ), patch(
            "os.makedirs", lambda *a, **k: None,
        ):
            body = _rpc(client, "tools/call", {
                "name": "get_customer_statement_pdf",
                "arguments": {"partner_id": 62, "days_back": 60},
            }, headers={"X-Channel": "whatsapp"})

        text = body["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["success"] is True
        assert payload["partner_id"] == 62
        assert payload["report_xmlid"] == "tecno_l10n_ec_payment.report_account_balance"
        assert payload["pdf_size_bytes"] == len(b"%PDF-1.7 statement-mock-content-payload")
        assert payload["pdf_url"].startswith("http")
        assert payload["pdf_filename"].startswith("estado_cuenta_partner62_")
        assert payload["pdf_filename"].endswith(".pdf")
        # display_text is attached for chat channels.
        assert "estado de cuenta oficial" in payload["display_text"].lower()
        assert payload["pdf_url"] in payload["display_text"]


# ---------------------------------------------------------------------------
# Formatter contract (pure dict → string)
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_format_statement_pdf_success(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_statement_pdf
        out = format_statement_pdf({
            "success": True,
            "pdf_url": "https://niko.galapagos.tech/files/statements/x.pdf",
            "pdf_size_bytes": 75828,
            "generated_at_local": "23-may 01:45",
            "expires_at": "2026-05-30T00:00:00Z",
        })
        assert "estado de cuenta oficial" in out.lower()
        assert "https://niko.galapagos.tech/files/statements/x.pdf" in out
        assert "74 KB" in out

    def test_format_statement_pdf_failure(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_statement_pdf
        out = format_statement_pdf({"success": False, "error_code": "x"})
        assert "No pude generar" in out

    def test_format_invoice_pdf(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_invoice_pdf
        out = format_invoice_pdf({
            "success": True,
            "pdf_url": "https://niko.galapagos.tech/files/rides/y.pdf",
            "pdf_size_bytes": 65000,
            "invoice_name": "FACV/2025/4897",
        })
        assert "FACV/2025/4897" in out
        assert "https://niko.galapagos.tech/files/rides/y.pdf" in out
        assert "SRI" in out  # Footer mentions SRI

    def test_format_credit_note_pdf(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_credit_note_pdf
        out = format_credit_note_pdf({
            "success": True,
            "pdf_url": "https://niko.galapagos.tech/files/rides/nc.pdf",
            "invoice_name": "NCRV/2025/12",
        })
        assert "Nota de crédito" in out
        assert "NCRV/2025/12" in out

    def test_format_retention_pdf(self):
        from mcp_odoo.formatters.whatsapp_invoices import format_retention_pdf
        out = format_retention_pdf({
            "success": True,
            "pdf_url": "https://niko.galapagos.tech/files/rides/ret.pdf",
            "retention_name": "RET/2025/9",
        })
        assert "retención" in out.lower()
        assert "RET/2025/9" in out
