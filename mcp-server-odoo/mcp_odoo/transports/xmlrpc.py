import xmlrpc.client
from collections import defaultdict
from threading import Semaphore
from typing import Any

from mcp_odoo.config import settings


class OdooXMLRPCPool:
    """Connection pool for Odoo XML-RPC, keyed by tenant_id.

    Each tenant gets max N concurrent connections with timeout.
    One slow Odoo instance does not block other tenants.
    """

    def __init__(self):
        self._semaphores: dict[str, Semaphore] = defaultdict(
            lambda: Semaphore(settings.max_connections_per_tenant)
        )
        self._uid_cache: dict[str, int] = {}

    def _authenticate(self, url: str, db: str, user: str, password: str) -> int:
        print(f"[XMLRPC DEBUG] _authenticate url={url!r} db={db!r} user={user!r}", flush=True)

        cache_key = f"{url}:{db}:{user}"
        if cache_key in self._uid_cache:
            return self._uid_cache[cache_key]

        common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common",
            allow_none=True,
        )
        uid = common.authenticate(db, user, password, {})
        if not uid:
            raise ConnectionError(f"Odoo authentication failed for {user}@{db}")

        self._uid_cache[cache_key] = uid
        return uid

    def execute(
        self,
        tenant_id: str,
        url: str,
        db: str,
        user: str,
        password: str,
        model: str,
        method: str,
        args: list | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        """Execute an Odoo XML-RPC call with tenant-scoped connection pooling."""
        sem = self._semaphores[tenant_id]

        acquired = sem.acquire(timeout=settings.xmlrpc_timeout)
        if not acquired:
            raise TimeoutError(
                f"Tenant {tenant_id}: all {settings.max_connections_per_tenant} "
                f"connections busy, timeout after {settings.xmlrpc_timeout}s"
            )

        try:
            uid = self._authenticate(url, db, user, password)
            proxy = xmlrpc.client.ServerProxy(
                f"{url}/xmlrpc/2/object",
                allow_none=True,
            )
            return proxy.execute_kw(
                db,
                uid,
                password,
                model,
                method,
                args or [],
                kwargs or {},
            )
        finally:
            sem.release()

    def clear_auth_cache(self, tenant_id: str = None):
        """Clear authentication cache. If tenant_id provided, clear only that tenant."""
        if tenant_id:
            keys_to_remove = [k for k in self._uid_cache if tenant_id in k]
            for k in keys_to_remove:
                del self._uid_cache[k]
        else:
            self._uid_cache.clear()


# Singleton pool
odoo_pool = OdooXMLRPCPool()
