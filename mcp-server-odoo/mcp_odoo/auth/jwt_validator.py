import jwt
from fastapi import HTTPException, Request

from mcp_odoo.config import settings


def validate_tenant_jwt(request: Request) -> dict:
    """Validate JWT from request and extract tenant_id.

    The JWT is minted by the Provisioner with claims:
    {tenant_id: uuid, role: "service"}
    Signed with Supabase JWT secret.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            options={"require": ["tenant_id"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Missing tenant_id in token")

    return {"tenant_id": tenant_id, "role": payload.get("role", "service")}
