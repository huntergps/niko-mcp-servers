"""Tests for the salon appointment / booking tools (Odoo 19 ``appointment``).

Coverage matrix:
  * helper functions in ``mcp_odoo.tools.appointments`` (mocked
    odoo_search / odoo_read / odoo_create / odoo_call_method — no live
    Odoo required)
  * formatters in ``mcp_odoo.formatters.whatsapp_appointments`` (pure dict
    → string contract)
  * MCP transport registration (the 5 tools must appear in tools/list)

Key contracts asserted:
  * list_services filters by name
  * get_availability discards slots that overlap a busy event AND respects
    min_schedule_hours
  * book_appointment rejects an already-busy slot
  * book_appointment propagates alarm_ids from the type's reminder_ids
  * cancel_appointment rejects a partner that does not own the event
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

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

TZ = ZoneInfo("Pacific/Galapagos")


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


CREDS = ("t", "http://odoo", "db", "u", "p")

# A standard appointment.type record as read by the helpers.
TYPE_22 = {
    "id": 22,
    "name": "Manicura semipermanente",
    "appointment_duration": 1.25,
    "staff_user_ids": [6],
    "appointment_tz": "Pacific/Galapagos",
    "min_schedule_hours": 1.0,
    "product_id": [23, "Manicura semipermanente"],
    "reminder_ids": [6, 8],
}

# Mon-Sun 9:00-18:00 working windows for type 22.
SLOTS_22 = [
    {"weekday": str(wd), "start_hour": 9.0, "end_hour": 18.0, "allday": False}
    for wd in range(1, 8)
]


# ---------------------------------------------------------------------------
# tools/list registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_five_tools_listed(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]
        for expected in (
            "list_services",
            "get_availability",
            "book_appointment",
            "list_my_appointments",
            "cancel_appointment",
        ):
            assert expected in names, f"{expected} missing from MCP_TOOLS"

    def test_input_schema_required_fields(self, client):
        body = _rpc(client, "tools/list")
        by_name = {t["name"]: t for t in body["result"]["tools"]}
        assert by_name["get_availability"]["inputSchema"]["required"] == ["service"]
        book_req = set(by_name["book_appointment"]["inputSchema"]["required"])
        # partner_id is now optional (book con nombre+celular sin cédula).
        assert book_req == {"service", "start_local"}
        book_props = by_name["book_appointment"]["inputSchema"]["properties"]
        assert "customer_name" in book_props
        assert "customer_phone" in book_props
        cancel_req = set(by_name["cancel_appointment"]["inputSchema"]["required"])
        assert cancel_req == {"event_id", "partner_id"}
        assert by_name["list_my_appointments"]["inputSchema"]["required"] == ["partner_id"]


# ---------------------------------------------------------------------------
# list_services
# ---------------------------------------------------------------------------

class TestListServices:
    def test_basic_with_price(self):
        from mcp_odoo.tools import appointments as ap

        def _fake_search(*a, **k):
            assert a[5] == "appointment.type"
            return [TYPE_22]

        def _fake_read(*a, **k):
            assert a[5] == "product.product"
            return [{"id": 23, "list_price": 20.0}]

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             patch.object(ap, "odoo_read", side_effect=_fake_read):
            r = ap.list_services(*CREDS)
        assert r["success"] is True
        assert r["count"] == 1
        svc = r["services"][0]
        assert svc["name"] == "Manicura semipermanente"
        assert svc["price"] == 20.0
        assert svc["duration_label"] == "1 h 15 min"

    def test_query_filter_builds_domain(self):
        from mcp_odoo.tools import appointments as ap
        captured = {}

        def _fake_search(*a, **k):
            captured["domain"] = a[6]
            return []

        with patch.object(ap, "odoo_search", side_effect=_fake_search):
            ap.list_services(*CREDS, query="manicura")
        assert ["name", "ilike", "manicura"] in captured["domain"]


# ---------------------------------------------------------------------------
# get_availability
# ---------------------------------------------------------------------------

class TestGetAvailability:
    def test_service_not_found(self):
        from mcp_odoo.tools import appointments as ap
        with patch.object(ap, "odoo_search", side_effect=lambda *a, **k: []):
            r = ap.get_availability(*CREDS, service="inexistente")
        assert r["success"] is False
        assert r["error_code"] == "service_not_found"

    def test_min_schedule_hours_blocks_early_slots(self):
        """A slot earlier than now + min_schedule_hours must be excluded."""
        from mcp_odoo.tools import appointments as ap

        # Pick a date_from of "today" and force now() to a fixed local time
        # so the math is deterministic. now = 10:00 local, min lead = 1h →
        # earliest bookable 11:00. Window is 09:00-18:00.
        fixed_now = datetime(2026, 6, 10, 10, 0, tzinfo=TZ)  # a Wednesday

        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            if model == "calendar.event":
                return []  # nothing busy
            return []

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             patch.object(ap, "datetime", _FakeDateTime):
            r = ap.get_availability(
                *CREDS, service="Manicura", date_from="2026-06-10",
                days_ahead=1,
            )
        assert r["success"] is True
        day = r["days"][0]
        first_labels = [s["label"] for s in day["slots"]]
        # 09:00, 09:30, 10:00 must NOT appear (before 11:00 earliest).
        assert "09:00" not in first_labels
        assert "09:30" not in first_labels
        # 11:00 is the first valid candidate (10:00 + 1h lead).
        assert first_labels[0] == "11:00"

    def test_busy_event_discards_overlapping_slot(self):
        from mcp_odoo.tools import appointments as ap

        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)  # early, no lead block

        # Busy 09:00-10:15 local → in UTC (Galapagos = UTC-6) 15:00-16:15.
        busy_start_utc = datetime(2026, 6, 10, 9, 0, tzinfo=TZ).astimezone(
            ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
        busy_stop_utc = datetime(2026, 6, 10, 10, 15, tzinfo=TZ).astimezone(
            ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            if model == "calendar.event":
                return [{"start": busy_start_utc, "stop": busy_stop_utc}]
            return []

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             patch.object(ap, "datetime", _FakeDateTime):
            r = ap.get_availability(
                *CREDS, service="Manicura", date_from="2026-06-10",
                days_ahead=1,
            )
        labels = [s["label"] for s in r["days"][0]["slots"]]
        # 09:00 candidate (09:00-10:15) overlaps the busy block → excluded.
        assert "09:00" not in labels
        # 09:30 (09:30-10:45) still overlaps → excluded.
        assert "09:30" not in labels
        # The duration is 1.25h, so a slot at 10:30 (10:30-11:45) is clear.
        assert "10:30" in labels


# ---------------------------------------------------------------------------
# book_appointment
# ---------------------------------------------------------------------------

class TestBookAppointment:
    def _patch_now(self, fixed_now):
        from mcp_odoo.tools import appointments as ap

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)
        return patch.object(ap, "datetime", _FakeDateTime)

    def test_rejects_busy_slot(self):
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)

        # Book 11:00 local but there is a busy event 11:00-12:15.
        busy_start_utc = datetime(2026, 6, 10, 11, 0, tzinfo=TZ).astimezone(
            ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
        busy_stop_utc = datetime(2026, 6, 10, 12, 15, tzinfo=TZ).astimezone(
            ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            if model == "calendar.event":
                return [{"start": busy_start_utc, "stop": busy_stop_utc}]
            return []

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             self._patch_now(fixed_now):
            r = ap.book_appointment(
                *CREDS, service="Manicura", partner_id=99,
                start_local="2026-06-10 11:00",
            )
        assert r["success"] is False
        assert r["error_code"] == "slot_taken"

    def test_rejects_outside_hours(self):
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)

        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            return []

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             self._patch_now(fixed_now):
            # 19:00 is past the 18:00 close.
            r = ap.book_appointment(
                *CREDS, service="Manicura", partner_id=99,
                start_local="2026-06-10 19:00",
            )
        assert r["success"] is False
        assert r["error_code"] == "outside_hours"

    def test_success_propagates_alarm_ids(self):
        """book_appointment must set alarm_ids from type.reminder_ids."""
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)
        captured = {}

        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            if model == "calendar.event":
                return []
            return []

        def _fake_read(*a, **k):
            model = a[5]
            if model == "res.partner":
                return [{"id": 99, "name": "Doña Cliente"}]
            if model == "res.users":
                return [{"id": 6, "partner_id": [13, "Liceth"]}]
            return []

        def _fake_create(*a, **k):
            captured["model"] = a[5]
            captured["values"] = a[6]
            return 555

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_create", side_effect=_fake_create), \
             self._patch_now(fixed_now):
            r = ap.book_appointment(
                *CREDS, service="Manicura", partner_id=99,
                start_local="2026-06-10 11:00",
            )
        assert r["success"] is True
        assert r["event_id"] == 555
        vals = captured["values"]
        # Alarms inherited from the type's reminder_ids ([6, 8]).
        assert vals["alarm_ids"] == [(6, 0, [6, 8])]
        # Attendees include the customer AND the staff partner.
        attendee_cmd = vals["partner_ids"][0]
        assert attendee_cmd[0] == 6
        assert 99 in attendee_cmd[2]
        assert 13 in attendee_cmd[2]
        assert vals["appointment_type_id"] == 22
        assert vals["user_id"] == 6
        assert r["customer_name"] == "Doña Cliente"

    def test_rejects_invalid_start_format(self):
        from mcp_odoo.tools import appointments as ap

        def _fake_search(*a, **k):
            return [TYPE_22] if a[5] == "appointment.type" else []

        with patch.object(ap, "odoo_search", side_effect=_fake_search):
            r = ap.book_appointment(
                *CREDS, service="Manicura", partner_id=99,
                start_local="mañana a las 3",
            )
        assert r["success"] is False
        assert r["error_code"] == "invalid_start_local"


# ---------------------------------------------------------------------------
# list_my_appointments
# ---------------------------------------------------------------------------

class TestListMyAppointments:
    def test_basic(self):
        from mcp_odoo.tools import appointments as ap
        start_utc = "2026-06-12 16:00:00"  # 10:00 local Galapagos

        def _fake_search(*a, **k):
            if a[5] == "calendar.event":
                return [{
                    "id": 555, "name": "Manicura semipermanente - X",
                    "start": start_utc, "stop": "2026-06-12 17:15:00",
                    "duration": 1.25,
                    "appointment_type_id": [22, "Manicura semipermanente"],
                    "user_id": [6, "Liceth"],
                }]
            return []

        def _fake_read(*a, **k):
            if a[5] == "appointment.type":
                return [{"id": 22, "appointment_tz": "Pacific/Galapagos"}]
            return []

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             patch.object(ap, "odoo_read", side_effect=_fake_read):
            r = ap.list_my_appointments(*CREDS, partner_id=99)
        assert r["success"] is True
        assert r["count"] == 1
        appt = r["appointments"][0]
        assert appt["event_id"] == 555
        assert appt["service"] == "Manicura semipermanente"
        # 16:00 UTC → 10:00 local (UTC-6).
        assert appt["start_local"].endswith("10:00")

    def test_invalid_partner(self):
        from mcp_odoo.tools import appointments as ap
        r = ap.list_my_appointments(*CREDS, partner_id=0)
        assert r["error_code"] == "invalid_partner_id"


# ---------------------------------------------------------------------------
# cancel_appointment
# ---------------------------------------------------------------------------

class TestCancelAppointment:
    def test_rejects_foreign_partner(self):
        from mcp_odoo.tools import appointments as ap

        def _fake_read(*a, **k):
            # Event belongs to partners [42, 13] — NOT 99.
            return [{
                "id": 555, "name": "Manicura - Otra",
                "partner_ids": [42, 13], "start": "2026-06-12 16:00:00",
                "appointment_type_id": [22, "Manicura semipermanente"],
            }]

        called = {"unlink": False}

        def _fake_call(*a, **k):
            called["unlink"] = True
            return True

        with patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_call_method", side_effect=_fake_call):
            r = ap.cancel_appointment(*CREDS, event_id=555, partner_id=99)
        assert r["success"] is False
        assert r["error_code"] == "not_authorized"
        # Must NOT have attempted to unlink someone else's event.
        assert called["unlink"] is False

    def test_cancels_own_appointment(self):
        from mcp_odoo.tools import appointments as ap

        def _fake_read(*a, **k):
            return [{
                "id": 555, "name": "Manicura - Mía",
                "partner_ids": [99, 13], "start": "2026-06-12 16:00:00",
                "appointment_type_id": [22, "Manicura semipermanente"],
            }]

        with patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_call_method", side_effect=lambda *a, **k: True):
            r = ap.cancel_appointment(*CREDS, event_id=555, partner_id=99)
        assert r["success"] is True
        assert r["method"] == "unlink"
        assert r["service"] == "Manicura semipermanente"

    def test_falls_back_to_deactivate_when_unlink_denied(self):
        from mcp_odoo.tools import appointments as ap

        def _fake_read(*a, **k):
            return [{
                "id": 555, "name": "Manicura",
                "partner_ids": [99], "start": "2026-06-12 16:00:00",
                "appointment_type_id": [22, "Manicura semipermanente"],
            }]

        def _fake_call(*a, **k):
            method = a[6]
            if method == "unlink":
                raise Exception("AccessError: cannot unlink")
            return True

        with patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_call_method", side_effect=_fake_call):
            r = ap.cancel_appointment(*CREDS, event_id=555, partner_id=99)
        assert r["success"] is True
        assert r["method"] == "deactivate"

    def test_not_found(self):
        from mcp_odoo.tools import appointments as ap
        with patch.object(ap, "odoo_read", side_effect=lambda *a, **k: []):
            r = ap.cancel_appointment(*CREDS, event_id=999, partner_id=99)
        assert r["success"] is False
        assert r["error_code"] == "appointment_not_found"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _assert_no_markdown_tables(text: str) -> None:
    assert "|" not in text, f"pipe: {text!r}"
    assert "---" not in text, f"hr: {text!r}"
    assert "\t" not in text, f"tab: {text!r}"


class TestFormatters:
    def test_services_list(self):
        from mcp_odoo.formatters.whatsapp_appointments import format_services_list
        env = {
            "success": True, "count": 1,
            "services": [{
                "name": "Manicura semipermanente", "price": 20.0,
                "duration_label": "1 h 15 min",
            }],
        }
        text = format_services_list(env)
        _assert_no_markdown_tables(text)
        assert "Manicura semipermanente" in text
        assert "1 h 15 min" in text

    def test_availability_with_days(self):
        from mcp_odoo.formatters.whatsapp_appointments import format_availability
        env = {
            "success": True, "service": "Manicura semipermanente",
            "duration_label": "1 h 15 min",
            "days": [{
                "date": "2026-06-12", "weekday": "Viernes",
                "slots": [{"label": "10:00"}, {"label": "11:30"}],
            }],
        }
        text = format_availability(env)
        _assert_no_markdown_tables(text)
        assert "Viernes" in text
        assert "10:00" in text

    def test_availability_error_surfaces_detail(self):
        from mcp_odoo.formatters.whatsapp_appointments import format_availability
        env = {"success": False, "error_code": "service_not_found",
               "error_detail": "No encontré ese servicio."}
        text = format_availability(env)
        assert "No encontré ese servicio." in text

    def test_booking_confirmation(self):
        from mcp_odoo.formatters.whatsapp_appointments import (
            format_booking_confirmation,
        )
        env = {
            "success": True, "event_id": 555,
            "service": "Manicura semipermanente", "weekday": "Viernes",
            "start_local": "2026-06-12 10:00", "duration_label": "1 h 15 min",
        }
        text = format_booking_confirmation(env)
        _assert_no_markdown_tables(text)
        assert "agendada" in text
        assert "#555" in text

    def test_booking_error(self):
        from mcp_odoo.formatters.whatsapp_appointments import (
            format_booking_confirmation,
        )
        env = {"success": False, "error_code": "slot_taken",
               "error_detail": "Ese horario acaba de ocuparse."}
        text = format_booking_confirmation(env)
        assert "Ese horario acaba de ocuparse." in text

    def test_my_appointments(self):
        from mcp_odoo.formatters.whatsapp_appointments import format_my_appointments
        env = {
            "success": True, "count": 1,
            "appointments": [{
                "event_id": 555, "service": "Manicura semipermanente",
                "weekday": "Viernes", "start_local": "2026-06-12 10:00",
            }],
        }
        text = format_my_appointments(env)
        _assert_no_markdown_tables(text)
        assert "#555" in text
        assert "Manicura semipermanente" in text

    def test_cancellation(self):
        from mcp_odoo.formatters.whatsapp_appointments import format_cancellation
        text = format_cancellation({"success": True, "service": "Manicura"})
        assert "cancelé" in text
        assert "Manicura" in text

    def test_booking_pending_deposit(self):
        from mcp_odoo.formatters.whatsapp_appointments import (
            format_booking_confirmation,
        )
        env = {
            "success": True, "event_id": 777,
            "service": "Manicura semipermanente", "weekday": "Viernes",
            "start_local": "2026-06-12 10:00", "duration_label": "1 h 15 min",
            "pending_deposit": True,
        }
        text = format_booking_confirmation(env)
        _assert_no_markdown_tables(text)
        assert "por confirmar" in text.lower()
        assert "50%" in text
        assert "no reembolsable" in text.lower()
        assert "comprobante" in text.lower()

    def test_payment_info(self):
        from mcp_odoo.formatters.whatsapp_appointments import format_payment_info
        env = {
            "success": True,
            "bank": "Banco Guayaquil",
            "account_type": "Cuenta de Ahorros",
            "account_number": "18958304",
            "holder": "Liceth Alava Mendoza",
            "holder_id": "2300218159",
            "deposit_percent": 50,
            "refundable": False,
            "method": "transferencia",
            "pdf_filename": "afrodita_datos_pago.png",
        }
        text = format_payment_info(env)
        _assert_no_markdown_tables(text)
        assert "Banco Guayaquil" in text
        assert "18958304" in text
        assert "Liceth Alava Mendoza" in text
        assert "2300218159" in text
        assert "50%" in text
        assert "no reembolsable" in text.lower()
        assert "comprobante" in text.lower()


# ---------------------------------------------------------------------------
# book_appointment — contact resolution (sin cédula)
# ---------------------------------------------------------------------------

class TestBookAppointmentContact:
    def _patch_now(self, fixed_now):
        from mcp_odoo.tools import appointments as ap

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)
        return patch.object(ap, "datetime", _FakeDateTime)

    def _search_factory(self, partner_rows):
        """Return a fake odoo_search; res.partner returns ``partner_rows``."""
        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            if model == "calendar.event":
                return []
            if model == "res.partner":
                return list(partner_rows)
            return []
        return _fake_search

    def test_phone_required_when_no_partner(self):
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)
        with patch.object(ap, "odoo_search",
                          side_effect=self._search_factory([])), \
             self._patch_now(fixed_now):
            r = ap.book_appointment(
                *CREDS, service="Manicura",
                start_local="2026-06-10 11:00",
                customer_name="QA Prueba",
            )
        assert r["success"] is False
        assert r["error_code"] == "phone_required"

    def test_creates_minimal_contact(self):
        """No partner_id + phone with no match → create contact sin vat."""
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)
        captured = {}

        def _fake_read(*a, **k):
            model = a[5]
            if model == "res.users":
                return [{"id": 6, "partner_id": [13, "Liceth"]}]
            return []

        def _fake_create(*a, **k):
            model = a[5]
            vals = a[6]
            if model == "res.partner":
                captured["partner_vals"] = vals
                return 1001
            if model == "calendar.event":
                captured["event_vals"] = vals
                return 555
            return 0

        with patch.object(ap, "odoo_search",
                          side_effect=self._search_factory([])), \
             patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_create", side_effect=_fake_create), \
             patch.object(ap, "valid_partner_fields",
                          side_effect=lambda *a, **k: ["phone"]), \
             self._patch_now(fixed_now):
            r = ap.book_appointment(
                *CREDS, service="Manicura",
                start_local="2026-06-10 11:00",
                customer_name="QA Prueba", customer_phone="0990000001",
            )
        assert r["success"] is True
        assert r["partner_id"] == 1001
        assert r["partner_created"] is True
        assert r["pending_deposit"] is True
        # Minimal contact: name + phone only, NO vat / NO customer_rank.
        pv = captured["partner_vals"]
        assert pv["name"] == "QA Prueba"
        assert pv["phone"] == "0990000001"
        assert "vat" not in pv
        assert "customer_rank" not in pv
        # Event name prefixed [POR CONFIRMAR].
        assert captured["event_vals"]["name"].startswith("[POR CONFIRMAR] ")

    def test_reuses_existing_partner_by_phone(self):
        """A returning customer (phone match) is reused, never duplicated."""
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)
        created_models: list[str] = []

        def _fake_read(*a, **k):
            if a[5] == "res.users":
                return [{"id": 6, "partner_id": [13, "Liceth"]}]
            return []

        def _fake_create(*a, **k):
            created_models.append(a[5])
            return 555  # calendar.event

        # Existing partner whose phone matches after normalization.
        partner_rows = [{"id": 2002, "name": "QA Prueba",
                         "phone": "099 000-0001"}]

        with patch.object(ap, "odoo_search",
                          side_effect=self._search_factory(partner_rows)), \
             patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_create", side_effect=_fake_create), \
             patch.object(ap, "valid_partner_fields",
                          side_effect=lambda *a, **k: ["phone"]), \
             self._patch_now(fixed_now):
            r = ap.book_appointment(
                *CREDS, service="Manicura",
                start_local="2026-06-10 11:00",
                customer_name="QA Prueba", customer_phone="0990000001",
            )
        assert r["success"] is True
        assert r["partner_id"] == 2002
        assert r["partner_created"] is False
        # res.partner must NOT have been created — only the calendar.event.
        assert "res.partner" not in created_models

    def test_mobile_field_pruned_odoo19(self):
        """Odoo 19 lacks res.partner.mobile → it must not enter the domain."""
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)
        captured = {}

        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            if model == "calendar.event":
                return []
            if model == "res.partner":
                captured["domain"] = a[6]
                return []
            return []

        def _fake_read(*a, **k):
            if a[5] == "res.users":
                return [{"id": 6, "partner_id": [13, "Liceth"]}]
            return []

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_create", side_effect=lambda *a, **k: 1001), \
             patch.object(ap, "valid_partner_fields",
                          side_effect=lambda *a, **k: ["phone"]), \
             self._patch_now(fixed_now):
            ap.book_appointment(
                *CREDS, service="Manicura",
                start_local="2026-06-10 11:00",
                customer_name="QA", customer_phone="0990000001",
            )
        # Domain only references 'phone', never 'mobile'.
        flat = str(captured["domain"])
        assert "phone" in flat
        assert "mobile" not in flat

    def test_existing_partner_id_used_verbatim(self):
        """A valid partner_id is used as-is; no contact lookup/creation."""
        from mcp_odoo.tools import appointments as ap
        fixed_now = datetime(2026, 6, 10, 6, 0, tzinfo=TZ)
        created_models: list[str] = []

        def _fake_search(*a, **k):
            model = a[5]
            if model == "appointment.type":
                return [TYPE_22]
            if model == "appointment.slot":
                return SLOTS_22
            return []

        def _fake_read(*a, **k):
            model = a[5]
            if model == "res.partner":
                return [{"id": 99, "name": "Doña Cliente", "phone": "0987"}]
            if model == "res.users":
                return [{"id": 6, "partner_id": [13, "Liceth"]}]
            return []

        def _fake_create(*a, **k):
            created_models.append(a[5])
            return 555

        with patch.object(ap, "odoo_search", side_effect=_fake_search), \
             patch.object(ap, "odoo_read", side_effect=_fake_read), \
             patch.object(ap, "odoo_create", side_effect=_fake_create), \
             self._patch_now(fixed_now):
            r = ap.book_appointment(
                *CREDS, service="Manicura", partner_id=99,
                start_local="2026-06-10 11:00",
            )
        assert r["success"] is True
        assert r["partner_id"] == 99
        assert r["partner_created"] is False
        assert "res.partner" not in created_models


# ---------------------------------------------------------------------------
# get_payment_info
# ---------------------------------------------------------------------------

class TestGetPaymentInfo:
    def test_returns_pdf_filename_and_bank_data(self):
        from mcp_odoo.tools.billing import get_payment_info
        r = get_payment_info(*CREDS)
        assert r["success"] is True
        assert r["pdf_filename"] == "afrodita_datos_pago.png"
        assert r["bank"] == "Banco Guayaquil"
        assert r["account_number"] == "18958304"
        assert r["holder"] == "Liceth Alava Mendoza"
        assert r["holder_id"] == "2300218159"
        assert r["deposit_percent"] == 50
        assert r["refundable"] is False
        assert "18958304" in r["payment_text"]
        assert "comprobante" in r["payment_text"].lower()

    def test_registered_in_tools_list(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]
        assert "get_payment_info" in names


# ---------------------------------------------------------------------------
# get_location_info
# ---------------------------------------------------------------------------

class TestGetLocationInfo:
    def test_returns_pdf_filename_and_location_data(self):
        from mcp_odoo.tools.billing import get_location_info
        r = get_location_info(*CREDS)
        assert r["success"] is True
        assert r["pdf_filename"] == "afrodita_direccion.png"
        assert r["address"] == "Miraflores, junto a Danubios Boutique"
        assert r["hours"] == "9:00 am a 6:00 pm"
        assert r["phone"] == "0988294278"
        assert r["instagram"] == "@afroditastudio.stx"
        # Location text carries the owner-provided data verbatim.
        assert "Miraflores, junto a Danubios Boutique" in r["location_text"]
        assert "9:00 am a 6:00 pm" in r["location_text"]
        assert "0988294278" in r["location_text"]
        assert "@afroditastudio.stx" in r["location_text"]

    def test_registered_in_tools_list(self, client):
        body = _rpc(client, "tools/list")
        names = [t["name"] for t in body["result"]["tools"]]
        assert "get_location_info" in names

    def test_formatter_chat_safe(self):
        from mcp_odoo.formatters.whatsapp_appointments import (
            format_location_info,
        )
        env = {
            "success": True,
            "address": "Miraflores, junto a Danubios Boutique",
            "hours": "9:00 am a 6:00 pm",
            "phone": "0988294278",
            "instagram": "@afroditastudio.stx",
            "pdf_filename": "afrodita_direccion.png",
        }
        text = format_location_info(env)
        _assert_no_markdown_tables(text)
        assert "Miraflores, junto a Danubios Boutique" in text
        assert "9:00 am a 6:00 pm" in text
        assert "0988294278" in text
        assert "@afroditastudio.stx" in text
