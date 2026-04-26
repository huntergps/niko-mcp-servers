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
    """Tests for create_quotation / add_to_quotation.

    The handler chain for create_quotation is:
      1. odoo_read("res.partner", [partner_id])         - partner check
      2. (NEW) odoo_search("product.template", default_code IN [...])
                                                        - SKU → template_id
                                                        (only when product_code passed)
      3. odoo_search("product.product", product_tmpl_id IN [...])
                                                        - template → variant + uom
      4. odoo_create("sale.order", values)              - returns order_id
      5. odoo_read("sale.order", [order_id], ...)       - read header
      6. odoo_read("sale.order.line", [line_ids], ...)  - read lines

    All six go through odoo_pool.execute, so we mock side_effect in order.
    """

    @staticmethod
    def _mock_create_pool(extra_lookup_responses: list = None):
        """Build a mock with the canonical happy-path side_effect sequence.

        extra_lookup_responses is a list of additional responses inserted
        right after the partner check (used when the handler also runs the
        SKU lookup, which only happens when product_code is passed).
        """
        from unittest.mock import MagicMock
        mock_pool = MagicMock()
        sequence = [
            # 1) partner check
            [{"id": 1, "name": "Cliente", "vat": "1234567890"}],
        ]
        if extra_lookup_responses:
            sequence.extend(extra_lookup_responses)
        sequence.extend([
            # variant lookup
            [{"id": 50, "product_tmpl_id": [10, "Producto"], "uom_id": [1, "Units"]}],
            # create returns order_id
            101,
            # read sale.order header
            [{"id": 101, "name": "SO-2026-0001", "state": "draft",
              "partner_id": [1, "Cliente"], "amount_untaxed": 2450.0,
              "amount_tax": 367.5, "amount_total": 2817.5,
              "order_line": [1], "date_order": "2026-04-01",
              "share_link_so": ""}],
            # read sale.order.line
            [{"product_id": [50, "Producto"], "name": "Producto",
              "product_uom_qty": 1, "price_unit": 2450.0, "discount": 0,
              "price_subtotal": 2450.0, "price_tax": 367.5,
              "price_total": 2817.5}],
        ])
        mock_pool.execute.side_effect = sequence
        return mock_pool

    def test_create_quotation_with_legacy_product_id(self, client):
        """Legacy path: caller passes product_id (template_id) — still works."""
        with patch("mcp_odoo.tools.generic.odoo_pool", self._mock_create_pool()):
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

    def test_create_quotation_with_sku(self, client):
        """New path: caller passes product_code — handler resolves SKU → tmpl_id."""
        sku_lookup_response = [
            [{"id": 10, "default_code": "VID0581"}],  # SKU resolves to template 10
        ]
        with patch(
            "mcp_odoo.tools.generic.odoo_pool",
            self._mock_create_pool(extra_lookup_responses=sku_lookup_response),
        ):
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [{"product_code": "VID0581", "quantity": 1}],
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["success"] is True
            assert result["name"] == "SO-2026-0001"

    def test_create_quotation_sku_not_found(self, client):
        """Caller passes a SKU that does not exist → clear sku_not_found error."""
        from unittest.mock import MagicMock
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # partner check
            [{"id": 1, "name": "Cliente", "vat": "1234567890"}],
            # SKU lookup returns nothing (catalog miss)
            [],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [{"product_code": "FAKE0000", "quantity": 1}],
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["success"] is False
            assert result["error_code"] == "sku_not_found"
            assert "FAKE0000" in result["missing_skus"]

    def test_create_quotation_missing_identifier(self, client):
        """Line with neither product_code nor product_id → clear error."""
        from unittest.mock import MagicMock
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # partner check passes
            [{"id": 1, "name": "Cliente", "vat": "1234567890"}],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [{"quantity": 1}],  # no SKU, no id
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["success"] is False
            assert result["error_code"] == "missing_product_identifier"

    def test_create_quotation_mixed_legacy_and_new(self, client):
        """Two lines: one with product_code, one with product_id — both work."""
        from unittest.mock import MagicMock
        mock_pool = MagicMock()
        mock_pool.execute.side_effect = [
            # 1) partner check
            [{"id": 1, "name": "Cliente", "vat": "1234567890"}],
            # 2) SKU lookup (only the SKU line triggers this — but the
            #    legacy line bypasses it since it already has a template_id)
            [{"id": 10, "default_code": "VID0581"}],
            # 3) variant lookup for templates [10, 20]
            [
                {"id": 50, "product_tmpl_id": [10, "Producto A"], "uom_id": [1, "Units"]},
                {"id": 51, "product_tmpl_id": [20, "Producto B"], "uom_id": [1, "Units"]},
            ],
            # 4) create returns order_id
            101,
            # 5) read sale.order header
            [{"id": 101, "name": "SO-2026-0002", "state": "draft",
              "partner_id": [1, "Cliente"], "amount_untaxed": 100.0,
              "amount_tax": 15.0, "amount_total": 115.0,
              "order_line": [1, 2], "date_order": "2026-04-01",
              "share_link_so": ""}],
            # 6) read sale.order.line
            [
                {"product_id": [50, "Producto A"], "name": "Producto A",
                 "product_uom_qty": 1, "price_unit": 50.0, "discount": 0,
                 "price_subtotal": 50.0, "price_tax": 7.5, "price_total": 57.5},
                {"product_id": [51, "Producto B"], "name": "Producto B",
                 "product_uom_qty": 1, "price_unit": 50.0, "discount": 0,
                 "price_subtotal": 50.0, "price_tax": 7.5, "price_total": 57.5},
            ],
        ]
        with patch("mcp_odoo.tools.generic.odoo_pool", mock_pool):
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [
                        {"product_code": "VID0581", "quantity": 1},  # new
                        {"product_id": 20, "quantity": 1},           # legacy
                    ],
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["success"] is True
            assert len(result["lines"]) == 2

    def test_create_quotation_sku_preferred_when_both_passed(self, client):
        """If both product_code and product_id are given, product_code wins."""
        sku_lookup_response = [
            [{"id": 10, "default_code": "VID0581"}],  # SKU resolves to 10
        ]
        with patch(
            "mcp_odoo.tools.generic.odoo_pool",
            self._mock_create_pool(extra_lookup_responses=sku_lookup_response),
        ):
            # Pass mismatched product_id (999) — handler should ignore it
            # in favor of product_code (VID0581 → template 10).
            response = client.post(
                "/tools/odoo_create_quotation",
                json={
                    "partner_id": 1,
                    "lines": [
                        {"product_code": "VID0581", "product_id": 999, "quantity": 1},
                    ],
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["success"] is True
