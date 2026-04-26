"""MCP Server Odoo — multi-tenant, multi-version.

Exposes Odoo tools via HTTP API. Each request must include a JWT
with tenant_id claim. The server loads tenant Odoo credentials
from Supabase (encrypted with AES-256).
"""

import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mcp_odoo.auth.encryption import decrypt_credentials
from mcp_odoo.auth.jwt_validator import validate_tenant_jwt
from mcp_odoo.config import settings
from mcp_odoo.tools import generic, inventory, sales, sri
from mcp_odoo.transports.mcp_transport import router as mcp_router


# --- Tenant credential cache (in-memory, decrypted on demand) ---
_tenant_cache: dict[str, dict] = {}


async def get_tenant_odoo_config(tenant: dict = Depends(validate_tenant_jwt)) -> dict:
    """Load Odoo connection config for the tenant.

    Reads encrypted credentials from Supabase, decrypts, caches in memory.
    """
    tenant_id = tenant["tenant_id"]

    if tenant_id in _tenant_cache:
        return _tenant_cache[tenant_id]

    # Load from Supabase
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.supabase_url}/rest/v1/tenants",
            params={"id": f"eq.{tenant_id}", "select": "odoo_url,odoo_db,odoo_user,odoo_password_encrypted"},
            headers={
                "apikey": settings.supabase_jwt_secret,  # Using JWT secret as anon key for internal calls
                "Authorization": f"Bearer {settings.supabase_jwt_secret}",
            },
        )

    if resp.status_code != 200 or not resp.json():
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")

    tenant_data = resp.json()[0]
    password = decrypt_credentials(tenant_data["odoo_password_encrypted"])

    config = {
        "tenant_id": tenant_id,
        "url": tenant_data["odoo_url"],
        "db": tenant_data["odoo_db"],
        "user": tenant_data["odoo_user"],
        "password": password,
    }
    _tenant_cache[tenant_id] = config
    return config


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _tenant_cache.clear()


app = FastAPI(
    title="MCP Server Odoo",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(ConnectionError)
async def connection_error_handler(request: Request, exc: ConnectionError):
    return JSONResponse(status_code=502, content={"error": f"Odoo connection failed: {exc}"})


@app.exception_handler(TimeoutError)
async def timeout_error_handler(request: Request, exc: TimeoutError):
    return JSONResponse(status_code=504, content={"error": f"Odoo timeout: {exc}"})


# --- Health check ---

# --- MCP Standard Transport (StreamableHTTP) ---
app.include_router(mcp_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Generic tools ---

class SearchRequest(BaseModel):
    model: str
    domain: list
    fields: list[str] | None = None
    limit: int = 80
    offset: int = 0
    order: str | None = None


@app.post("/tools/odoo_search")
async def tool_search(req: SearchRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = generic.odoo_search(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.model, req.domain, req.fields, req.limit, req.offset, req.order,
    )
    return {"result": result}


class ReadRequest(BaseModel):
    model: str
    ids: list[int]
    fields: list[str] | None = None


@app.post("/tools/odoo_read")
async def tool_read(req: ReadRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = generic.odoo_read(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.model, req.ids, req.fields,
    )
    return {"result": result}


class CreateRequest(BaseModel):
    model: str
    values: dict


@app.post("/tools/odoo_create")
async def tool_create(req: CreateRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = generic.odoo_create(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.model, req.values,
    )
    return {"result": result}


class WriteRequest(BaseModel):
    model: str
    ids: list[int]
    values: dict


@app.post("/tools/odoo_write")
async def tool_write(req: WriteRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = generic.odoo_write(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.model, req.ids, req.values,
    )
    return {"result": result}


# --- Inventory tools ---

class StockCheckRequest(BaseModel):
    product_ids: list[int]
    warehouse_id: int | None = None


@app.post("/tools/odoo_check_stock")
async def tool_check_stock(req: StockCheckRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = inventory.odoo_check_stock(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.product_ids, req.warehouse_id,
    )
    return {"result": result}


# --- Sales tools ---

class QuotationRequest(BaseModel):
    partner_id: int
    lines: list[dict]
    notes: str = ""
    end_customer_name: str | None = None
    end_customer_phone: str | None = None
    end_customer_email: str | None = None
    salesperson_user_id: int | None = None


@app.post("/tools/odoo_create_quotation")
async def tool_create_quotation(req: QuotationRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sales.odoo_create_quotation(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.partner_id, req.lines, req.notes,
        end_customer_name=req.end_customer_name,
        end_customer_phone=req.end_customer_phone,
        end_customer_email=req.end_customer_email,
        salesperson_user_id=req.salesperson_user_id,
    )
    return {"result": result}


class ConfirmOrderRequest(BaseModel):
    order_id: int


@app.post("/tools/odoo_confirm_sale_order")
async def tool_confirm_order(req: ConfirmOrderRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sales.odoo_confirm_sale_order(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.order_id,
    )
    return {"result": result}


class PartnerSearchRequest(BaseModel):
    vat: str | None = None
    name: str | None = None
    phone: str | None = None


@app.post("/tools/odoo_search_partner")
async def tool_search_partner(req: PartnerSearchRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sales.odoo_search_partner(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.vat, req.name, req.phone,
    )
    return {"result": result}


class BalanceCheckRequest(BaseModel):
    partner_id: int


@app.post("/tools/odoo_check_balance")
async def tool_check_balance(req: BalanceCheckRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sales.odoo_check_balance(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.partner_id,
    )
    return {"result": result}


# --- B2B Sales Assistant (Sprint 2) ---


class LookupUserByEmailRequest(BaseModel):
    email: str


@app.post("/tools/odoo_lookup_user_by_email")
async def tool_lookup_user_by_email(
    req: LookupUserByEmailRequest,
    config: dict = Depends(get_tenant_odoo_config),
):
    result = sales.odoo_lookup_user_by_email(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.email,
    )
    return {"result": result}


# Sprint 2F — Generic ERP-agnostic policy & authorization (backend-only)


@app.post("/tools/odoo_get_discount_policy")
async def tool_get_discount_policy(
    config: dict = Depends(get_tenant_odoo_config),
):
    result = sales.odoo_get_discount_policy(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
    )
    return {"result": result}


class VerifySellerAuthorizationRequest(BaseModel):
    email: str


@app.post("/tools/odoo_verify_seller_authorization")
async def tool_verify_seller_authorization(
    req: VerifySellerAuthorizationRequest,
    config: dict = Depends(get_tenant_odoo_config),
):
    result = sales.odoo_verify_seller_authorization(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.email,
    )
    return {"result": result}


class ApplyDiscountRequest(BaseModel):
    order_id: int
    discount_pct: float
    line_id: int | None = None
    reason: str | None = None


@app.post("/tools/odoo_apply_discount")
async def tool_apply_discount(
    req: ApplyDiscountRequest,
    config: dict = Depends(get_tenant_odoo_config),
):
    result = sales.odoo_apply_discount(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.order_id, req.discount_pct,
        line_id=req.line_id, reason=req.reason,
    )
    return {"result": result}


class ListMyQuotationsRequest(BaseModel):
    salesperson_user_id: int
    state: list[str] | None = None
    limit: int = 20


@app.post("/tools/odoo_list_my_quotations")
async def tool_list_my_quotations(
    req: ListMyQuotationsRequest,
    config: dict = Depends(get_tenant_odoo_config),
):
    result = sales.odoo_list_my_quotations(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.salesperson_user_id,
        state=req.state, limit=req.limit,
    )
    return {"result": result}


class ScheduleVisitRequest(BaseModel):
    partner_id: int
    summary: str
    date_deadline: str
    salesperson_user_id: int
    note: str | None = None


@app.post("/tools/odoo_schedule_visit")
async def tool_schedule_visit(
    req: ScheduleVisitRequest,
    config: dict = Depends(get_tenant_odoo_config),
):
    result = sales.odoo_schedule_visit(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.partner_id, req.summary, req.date_deadline, req.salesperson_user_id,
        note=req.note,
    )
    return {"result": result}


# --- SRI tools ---

class SRIImportRequest(BaseModel):
    access_key: str
    tipo_importacion: str = "sri"
    tipo_homologacion: str = "auto"
    state_purchase_orders: str = "done"
    ambient_id: int | None = None


@app.post("/tools/odoo_sri_import")
async def tool_sri_import(req: SRIImportRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sri.sri_import_create(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.access_key, req.tipo_importacion, req.tipo_homologacion,
        req.state_purchase_orders, req.ambient_id,
    )
    return {"result": result}


class SRIStatusRequest(BaseModel):
    record_id: int


@app.post("/tools/odoo_sri_status")
async def tool_sri_status(req: SRIStatusRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sri.sri_import_status(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.record_id,
    )
    return {"result": result}


class SRIPendingLinesRequest(BaseModel):
    import_id: int


@app.post("/tools/odoo_sri_pending_lines")
async def tool_sri_pending_lines(req: SRIPendingLinesRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sri.sri_import_get_pending_lines(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.import_id,
    )
    return {"result": result}


class SRIAssignProductRequest(BaseModel):
    line_id: int
    product_tmpl_id: int


@app.post("/tools/odoo_sri_assign_product")
async def tool_sri_assign_product(req: SRIAssignProductRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sri.sri_import_assign_product(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.line_id, req.product_tmpl_id,
    )
    return {"result": result}


class SRICreateOrderRequest(BaseModel):
    import_id: int


@app.post("/tools/odoo_sri_create_order")
async def tool_sri_create_order(req: SRICreateOrderRequest, config: dict = Depends(get_tenant_odoo_config)):
    result = sri.sri_import_create_order(
        config["tenant_id"], config["url"], config["db"], config["user"], config["password"],
        req.import_id,
    )
    return {"result": result}
