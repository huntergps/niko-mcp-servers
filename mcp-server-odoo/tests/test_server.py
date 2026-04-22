"""Tests for the FastAPI MCP Server endpoints."""

import jwt
import os
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient


JWT_SECRET = "test-jwt-secret-for-testing-only-32bytes!"

MOCK_CONFIG = {
    "tenant_id": "test-tenant-001",
    "url": "http://fake-odoo:8069",
    "db": "testdb",
    "user": "admin",
    "password": "admin",
}


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase:8000")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())


def make_tenant_jwt(tenant_id="test-tenant-001"):
    return jwt.encode(
        {"tenant_id": tenant_id, "role": "service"},
        JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def client():
    from importlib import reload
    from mcp_odoo import config
    reload(config)
    from mcp_odoo.server import app, get_tenant_odoo_config

    # Override the dependency so it never hits Supabase
    async def mock_config():
        return MOCK_CONFIG

    app.dependency_overrides[get_tenant_odoo_config] = mock_config
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


class TestHealthEndpoint:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuthMiddleware:
    def test_no_auth_override_still_works(self, client):
        """With dependency override, requests work without JWT."""
        with patch("mcp_odoo.tools.generic.odoo_pool") as mock_pool:
            mock_pool.execute.return_value = []
            response = client.post("/tools/odoo_search", json={
                "model": "res.partner", "domain": []
            })
            assert response.status_code == 200


class TestSearchEndpoint:
    def test_valid_search(self, client):
        with patch("mcp_odoo.tools.generic.odoo_pool") as mock_pool:
            mock_pool.execute.return_value = [{"id": 1, "name": "Test Partner"}]
            response = client.post(
                "/tools/odoo_search",
                json={"model": "res.partner", "domain": []},
            )
            assert response.status_code == 200
            assert response.json()["result"] == [{"id": 1, "name": "Test Partner"}]


class TestSRIEndpoint:
    def test_sri_import_validates_short_key(self, client):
        response = client.post(
            "/tools/odoo_sri_import",
            json={"access_key": "12345"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["result"]["success"] is False
        assert "49" in data["result"]["error"]

    def test_sri_import_rejects_non_numeric(self, client):
        response = client.post(
            "/tools/odoo_sri_import",
            json={"access_key": "abc" + "0" * 46},
        )
        assert response.status_code == 200
        assert response.json()["result"]["success"] is False
        assert "digitos" in response.json()["result"]["error"]

    def test_sri_import_valid_key_calls_odoo(self, client):
        # Build a valid 49-digit key with correct checksum
        base = "0" * 48
        key = base + "0"  # checksum of all zeros is 0

        with patch("mcp_odoo.tools.generic.odoo_pool") as mock_pool:
            mock_pool.execute.side_effect = [
                42,  # create returns record_id
                [{"id": 42, "estatus_import": "ORDER", "estatus_cola": "procesado",
                  "products_to_map_count": 0, "partner_id": [1, "PROVEEDOR"],
                  "total": 100.0, "subtotal": 89.29, "total_descuento": 0,
                  "reference": "001-001-000000001", "autorizacion": "1234",
                  "establecimiento": "001", "puntoemision": "001", "secuencial": "000000001",
                  "invoice_date": "2026-04-01", "fechaautorizacion": "2026-04-01",
                  "estado_sri": "AUTORIZADO", "orders_purchase_ids": [10],
                  "invoices_purchase_ids": [20], "mensajes": "", "email": "test@test.com"}],
            ]
            response = client.post(
                "/tools/odoo_sri_import",
                json={"access_key": key},
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["success"] is True
            assert result["status"] == "ORDER"
            assert result["total"] == 100.0


class TestStockEndpoint:
    def test_stock_rejects_too_many_products(self, client):
        response = client.post(
            "/tools/odoo_check_stock",
            json={"product_ids": list(range(100))},
        )
        assert response.status_code == 400
        assert "Max 50" in response.json()["error"]

    def test_stock_valid_request(self, client):
        with patch("mcp_odoo.tools.inventory.odoo_pool") as mock_pool:
            mock_pool.execute.return_value = [
                {"id": 1, "name": "Laptop ASUS", "default_code": "LAP001",
                 "list_price": 2450.0, "qty_available": 3, "virtual_available": 2}
            ]
            response = client.post(
                "/tools/odoo_check_stock",
                json={"product_ids": [1]},
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert len(result) == 1
            assert result[0]["qty_available"] == 3
            assert result[0]["list_price"] == 2450.0


class TestQuotationEndpoint:
    def test_create_quotation(self, client):
        with patch("mcp_odoo.tools.generic.odoo_pool") as mock_pool:
            mock_pool.execute.side_effect = [
                101,  # create returns order_id
                [{"id": 101, "name": "SO-2026-0001", "state": "draft",
                  "partner_id": [1, "Cliente"], "amount_untaxed": 2450.0,
                  "amount_tax": 367.5, "amount_total": 2817.5,
                  "order_line": [1], "date_order": "2026-04-01"}],
            ]
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [{"product_id": 10, "quantity": 1}],
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["success"] is True
            assert result["name"] == "SO-2026-0001"
            assert result["total"] == 2817.5
