"""Tests for the salon deposit / proof-of-payment tools (Odoo 19).

Coverage matrix:
  * helpers in ``mcp_odoo.tools.deposits`` (mocked odoo_search /
    odoo_read / odoo_create / odoo_call_method — no live Odoo required)
  * formatters in ``mcp_odoo.formatters.whatsapp_deposits``
  * MCP transport registration (the 4 tools must appear in tools/list)

Key contracts asserted:
  * register_deposit_proof attaches the receipt to the event AND creates a
    DRAFT inbound/customer account.payment on the bank journal AND attaches
    the receipt to the payment.
  * confirm_appointment strips the '[POR CONFIRMAR] ' prefix and posts the
    linked draft payment.
  * release_appointment unlinks the event (and drops its draft payment).
  * list_pending_deposit_appointments filters by the prefix '=like' domain
    and a create_date cutoff.
"""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


JWT_SECRET = "test-jwt-secret-for-testing-only-32bytes!"

MOCK_TENANT_CONFIG = {
    "tenant_id": "tenant-afrodita-test",
    "url": "http://fake-odoo:8069",
    "db": "afrodita",
    "user": "worker_api",
    "password": "secret",
}

CREDS = (
    MOCK_TENANT_CONFIG["tenant_id"],
    MOCK_TENANT_CONFIG["url"],
    MOCK_TENANT_CONFIG["db"],
    MOCK_TENANT_CONFIG["user"],
    MOCK_TENANT_CONFIG["password"],
)

# A 1x1 transparent PNG, base64.
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


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


# ---------------------------------------------------------------------------
# tools/list registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_four_tools_listed(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]
        for expected in (
            "register_deposit_proof",
            "confirm_appointment",
            "release_appointment",
            "list_pending_deposit_appointments",
        ):
            assert expected in names, f"{expected} missing from MCP_TOOLS"

    def test_input_schema_required_fields(self, client):
        body = _rpc(client, "tools/list")
        by_name = {t["name"]: t for t in body["result"]["tools"]}

        reg_req = set(by_name["register_deposit_proof"]["inputSchema"]["required"])
        assert reg_req == {
            "event_id", "partner_id", "image_base64", "filename", "amount",
        }
        assert by_name["confirm_appointment"]["inputSchema"]["required"] == [
            "event_id"
        ]
        assert by_name["release_appointment"]["inputSchema"]["required"] == [
            "event_id"
        ]
        assert by_name[
            "list_pending_deposit_appointments"
        ]["inputSchema"]["required"] == []


# ---------------------------------------------------------------------------
# register_deposit_proof
# ---------------------------------------------------------------------------

class TestRegisterDepositProof:
    def test_creates_attachment_and_draft_payment(self):
        from mcp_odoo.tools import deposits as dp

        created: list[tuple[str, dict]] = []

        def _fake_read(*a, **k):
            model = a[5]
            if model == "calendar.event":
                return [{"id": a[6][0], "name": "[POR CONFIRMAR] Manicura"}]
            if model == "res.partner":
                return [{"id": a[6][0], "name": "Cliente QA"}]
            return []

        def _fake_search(*a, **k):
            model = a[5]
            if model == "account.journal":
                # bank journal resolution
                assert a[6] == [["type", "=", "bank"]]
                return [{"id": 6, "name": "Bank", "code": "BNK1"}]
            return []

        def _fake_create(*a, **k):
            model = a[5]
            vals = a[6]
            created.append((model, vals))
            if model == "ir.attachment":
                return 100 + len([c for c in created if c[0] == "ir.attachment"])
            if model == "account.payment":
                return 555
            return 1

        with patch.object(dp, "odoo_read", side_effect=_fake_read), \
             patch.object(dp, "odoo_search", side_effect=_fake_search), \
             patch.object(dp, "odoo_create", side_effect=_fake_create):
            r = dp.register_deposit_proof(
                *CREDS,
                event_id=42,
                partner_id=7,
                image_base64=PNG_1X1,
                filename="comprobante.png",
                amount=10.0,
                mimetype="image/png",
            )

        assert r["success"] is True
        assert r["payment_id"] == 555
        assert r["amount"] == 10.0
        assert r["event_id"] == 42

        # First attachment is on calendar.event, payment created in draft,
        # second attachment is on account.payment.
        models = [m for m, _ in created]
        assert models == ["ir.attachment", "account.payment", "ir.attachment"]

        event_attach = created[0][1]
        assert event_attach["res_model"] == "calendar.event"
        assert event_attach["res_id"] == 42
        assert event_attach["datas"] == PNG_1X1
        assert event_attach["mimetype"] == "image/png"

        payment_vals = created[1][1]
        assert payment_vals["payment_type"] == "inbound"
        assert payment_vals["partner_type"] == "customer"
        assert payment_vals["partner_id"] == 7
        assert payment_vals["amount"] == 10.0
        assert payment_vals["journal_id"] == 6
        assert payment_vals["date"] == date.today().isoformat()
        assert payment_vals["memo"] == "Anticipo cita #42"
        # NOTE: l10n_ec_sri_payment_id must NOT be set on account.payment.
        assert "l10n_ec_sri_payment_id" not in payment_vals

        payment_attach = created[2][1]
        assert payment_attach["res_model"] == "account.payment"
        assert payment_attach["res_id"] == 555

    def test_event_not_found(self):
        from mcp_odoo.tools import deposits as dp

        with patch.object(dp, "odoo_read", side_effect=lambda *a, **k: []):
            r = dp.register_deposit_proof(
                *CREDS, event_id=42, partner_id=7,
                image_base64=PNG_1X1, filename="x.png", amount=10.0,
            )
        assert r["success"] is False
        assert r["error_code"] == "event_not_found"

    def test_no_bank_journal(self):
        from mcp_odoo.tools import deposits as dp

        def _fake_read(*a, **k):
            model = a[5]
            if model == "calendar.event":
                return [{"id": 42, "name": "[POR CONFIRMAR] x"}]
            if model == "res.partner":
                return [{"id": 7, "name": "Cliente"}]
            return []

        with patch.object(dp, "odoo_read", side_effect=_fake_read), \
             patch.object(dp, "odoo_search", side_effect=lambda *a, **k: []), \
             patch.object(dp, "odoo_create", side_effect=lambda *a, **k: 99):
            r = dp.register_deposit_proof(
                *CREDS, event_id=42, partner_id=7,
                image_base64=PNG_1X1, filename="x.png", amount=10.0,
            )
        assert r["success"] is False
        assert r["error_code"] == "no_bank_journal"

    def test_invalid_amount(self):
        from mcp_odoo.tools import deposits as dp
        r = dp.register_deposit_proof(
            *CREDS, event_id=42, partner_id=7,
            image_base64=PNG_1X1, filename="x.png", amount=0,
        )
        assert r["success"] is False
        assert r["error_code"] == "invalid_amount"


# ---------------------------------------------------------------------------
# confirm_appointment
# ---------------------------------------------------------------------------

class TestConfirmAppointment:
    def test_strips_prefix_and_posts_payment(self):
        from mcp_odoo.tools import deposits as dp

        calls: list[tuple] = []

        def _fake_read(*a, **k):
            model = a[5]
            if model == "account.payment":
                # post-action state verification — payment is now posted.
                return [{"id": 555, "state": "posted"}]
            return [{"id": 42, "name": "[POR CONFIRMAR] Manicura - Cliente"}]

        def _fake_search(*a, **k):
            # account.payment lookup by memo + draft state
            assert a[5] == "account.payment"
            return [{"id": 555, "state": "draft", "amount": 10.0,
                     "memo": "Anticipo cita #42"}]

        def _fake_call(*a, **k):
            model, method = a[5], a[6]
            calls.append((model, method, a[7]))
            return True

        with patch.object(dp, "odoo_read", side_effect=_fake_read), \
             patch.object(dp, "odoo_search", side_effect=_fake_search), \
             patch.object(dp, "odoo_call_method", side_effect=_fake_call):
            r = dp.confirm_appointment(*CREDS, event_id=42)

        assert r["success"] is True
        assert r["new_name"] == "Manicura - Cliente"
        assert r["payment_posted"] is True

        # write (rename) then action_post.
        methods = [(m, meth) for m, meth, _ in calls]
        assert ("calendar.event", "write") in methods
        assert ("account.payment", "action_post") in methods

    def test_no_prefix_no_rename(self):
        from mcp_odoo.tools import deposits as dp

        calls: list[tuple] = []

        def _fake_read(*a, **k):
            return [{"id": 42, "name": "Manicura - Cliente"}]

        def _fake_call(*a, **k):
            calls.append((a[5], a[6]))
            return True

        with patch.object(dp, "odoo_read", side_effect=_fake_read), \
             patch.object(dp, "odoo_search", side_effect=lambda *a, **k: []), \
             patch.object(dp, "odoo_call_method", side_effect=_fake_call):
            r = dp.confirm_appointment(*CREDS, event_id=42)

        assert r["success"] is True
        assert r["new_name"] == "Manicura - Cliente"
        assert r["payment_posted"] is False
        # No rename write because the prefix was absent.
        assert ("calendar.event", "write") not in calls

    def test_post_failure_does_not_abort(self):
        from mcp_odoo.tools import deposits as dp

        def _fake_read(*a, **k):
            model = a[5]
            if model == "account.payment":
                # post failed, payment is still draft.
                return [{"id": 555, "state": "draft"}]
            return [{"id": 42, "name": "[POR CONFIRMAR] x"}]

        def _fake_search(*a, **k):
            return [{"id": 555, "state": "draft", "amount": 10.0,
                     "memo": "Anticipo cita #42"}]

        def _fake_call(*a, **k):
            model, method = a[5], a[6]
            if model == "account.payment" and method == "action_post":
                raise RuntimeError("posting blocked")
            return True

        with patch.object(dp, "odoo_read", side_effect=_fake_read), \
             patch.object(dp, "odoo_search", side_effect=_fake_search), \
             patch.object(dp, "odoo_call_method", side_effect=_fake_call):
            r = dp.confirm_appointment(*CREDS, event_id=42)

        assert r["success"] is True
        assert r["payment_posted"] is False

    def test_event_not_found(self):
        from mcp_odoo.tools import deposits as dp
        with patch.object(dp, "odoo_read", side_effect=lambda *a, **k: []):
            r = dp.confirm_appointment(*CREDS, event_id=42)
        assert r["success"] is False
        assert r["error_code"] == "event_not_found"


# ---------------------------------------------------------------------------
# release_appointment
# ---------------------------------------------------------------------------

class TestReleaseAppointment:
    def test_unlinks_event_and_drops_draft_payment(self):
        from mcp_odoo.tools import deposits as dp

        calls: list[tuple] = []

        def _fake_read(*a, **k):
            return [{"id": 42, "name": "[POR CONFIRMAR] x"}]

        def _fake_search(*a, **k):
            return [{"id": 555, "state": "draft", "amount": 10.0,
                     "memo": "Anticipo cita #42"}]

        def _fake_call(*a, **k):
            calls.append((a[5], a[6]))
            return True

        with patch.object(dp, "odoo_read", side_effect=_fake_read), \
             patch.object(dp, "odoo_search", side_effect=_fake_search), \
             patch.object(dp, "odoo_call_method", side_effect=_fake_call):
            r = dp.release_appointment(*CREDS, event_id=42, reason="expirada")

        assert r["success"] is True
        assert r["method"] == "unlink"
        assert ("account.payment", "unlink") in calls
        assert ("calendar.event", "unlink") in calls

    def test_falls_back_to_deactivate_when_unlink_denied(self):
        from mcp_odoo.tools import deposits as dp

        def _fake_read(*a, **k):
            return [{"id": 42, "name": "[POR CONFIRMAR] x"}]

        def _fake_call(*a, **k):
            model, method = a[5], a[6]
            if model == "calendar.event" and method == "unlink":
                raise RuntimeError("ACL denied")
            return True

        with patch.object(dp, "odoo_read", side_effect=_fake_read), \
             patch.object(dp, "odoo_search", side_effect=lambda *a, **k: []), \
             patch.object(dp, "odoo_call_method", side_effect=_fake_call):
            r = dp.release_appointment(*CREDS, event_id=42)

        assert r["success"] is True
        assert r["method"] == "deactivate"

    def test_event_not_found(self):
        from mcp_odoo.tools import deposits as dp
        with patch.object(dp, "odoo_read", side_effect=lambda *a, **k: []):
            r = dp.release_appointment(*CREDS, event_id=42)
        assert r["success"] is False
        assert r["error_code"] == "event_not_found"


# ---------------------------------------------------------------------------
# list_pending_deposit_appointments
# ---------------------------------------------------------------------------

class TestListPendingDepositAppointments:
    def test_filters_by_prefix_and_age(self):
        from mcp_odoo.tools import deposits as dp

        captured_domain: list = []

        def _fake_search(*a, **k):
            assert a[5] == "calendar.event"
            captured_domain.extend(a[6])
            return [
                {
                    "id": 42,
                    "name": "[POR CONFIRMAR] Manicura - Cliente",
                    "start": "2026-06-07 15:00:00",
                    "create_date": "2026-06-07 12:00:00",
                    "partner_ids": [7, 9],
                },
            ]

        with patch.object(dp, "odoo_search", side_effect=_fake_search):
            r = dp.list_pending_deposit_appointments(
                *CREDS, older_than_minutes=120,
            )

        assert r["success"] is True
        assert r["count"] == 1
        appt = r["appointments"][0]
        assert appt["event_id"] == 42
        assert appt["partner_ids"] == [7, 9]

        # Domain must use the '=like' prefix filter + a create_date cutoff.
        assert ["name", "=like", "[POR CONFIRMAR] %"] in captured_domain
        cutoff_clauses = [
            c for c in captured_domain
            if isinstance(c, list) and c[0] == "create_date" and c[1] == "<"
        ]
        assert len(cutoff_clauses) == 1

    def test_empty(self):
        from mcp_odoo.tools import deposits as dp
        with patch.object(dp, "odoo_search", side_effect=lambda *a, **k: []):
            r = dp.list_pending_deposit_appointments(
                *CREDS, older_than_minutes=0,
            )
        assert r["success"] is True
        assert r["count"] == 0
        assert r["older_than_minutes"] == 0


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_deposit_received(self):
        from mcp_odoo.formatters.whatsapp_deposits import (
            format_deposit_received,
        )
        txt = format_deposit_received(
            {"success": True, "payment_id": 5, "amount": 10.0}
        )
        assert "comprobante" in txt.lower()
        assert "|" not in txt

    def test_deposit_received_error(self):
        from mcp_odoo.formatters.whatsapp_deposits import (
            format_deposit_received,
        )
        txt = format_deposit_received(
            {"success": False, "error_detail": "Monto inválido."}
        )
        assert "Monto inválido." in txt

    def test_appointment_confirmed(self):
        from mcp_odoo.formatters.whatsapp_deposits import (
            format_appointment_confirmed,
        )
        txt = format_appointment_confirmed({"success": True})
        assert "confirmada" in txt.lower()
        assert "|" not in txt

    def test_appointment_released(self):
        from mcp_odoo.formatters.whatsapp_deposits import (
            format_appointment_released,
        )
        txt = format_appointment_released({"success": True})
        assert "|" not in txt
        assert "agendar" in txt.lower()

    def test_pending_deposit_list(self):
        from mcp_odoo.formatters.whatsapp_deposits import (
            format_pending_deposit_list,
        )
        txt = format_pending_deposit_list({
            "success": True,
            "appointments": [
                {"event_id": 42, "name": "[POR CONFIRMAR] x"},
            ],
        })
        assert "#42" in txt
        assert "|" not in txt
