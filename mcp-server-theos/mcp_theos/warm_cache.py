"""Precalienta el caché de movimientos para una lista de días (o un rango).

Corre DENTRO del contenedor niko-mcp-theos. Escribe en /var/cache/mcp-theos
(bind-mount persistente del host). Idempotente: días ya cacheados se saltan al
instante. No borra nada.

Uso:
  python -m mcp_theos.warm_cache <tenant_id> <day1,day2,...>
  python -m mcp_theos.warm_cache <tenant_id> <date_from> <date_to>
  python -m mcp_theos.warm_cache <tenant_id> yesterday today   # keywords (hora ECU)
"""
import sys
import asyncio
import time
import datetime as dt

from mcp_theos.tenant_resolver import _resolve_by_id
from mcp_theos.velneo_http import VelneoClient
from mcp_theos.sales_report import _get_day_lines
from mcp_theos.tools.admin_ops import _tenant_sucursal


def _kw(tok):
    """Resuelve palabras clave de fecha en hora Ecuador (UTC-5)."""
    today = (dt.datetime.utcnow() - dt.timedelta(hours=5)).date()
    t = tok.strip().lower()
    if t in ("today", "hoy"):
        return today.isoformat()
    if t in ("yesterday", "ayer"):
        return (today - dt.timedelta(days=1)).isoformat()
    return tok


def _days_from_args(argv):
    # token unico o lista separada por comas, con soporte de keywords
    if len(argv) == 3:
        return [_kw(x) for x in argv[2].split(",")]
    if len(argv) == 4:
        a = dt.date.fromisoformat(_kw(argv[2]))
        b = dt.date.fromisoformat(_kw(argv[3]))
        out = []
        d = a
        while d <= b:
            out.append(d.isoformat())
            d += dt.timedelta(days=1)
        return out
    return [_kw(argv[2])]


async def main():
    tenant_id = sys.argv[1]
    days = _days_from_args(sys.argv)
    cfg = await _resolve_by_id(tenant_id)
    ok = cached = live = fail = 0
    async with VelneoClient(cfg) as client:
        suc = _tenant_sucursal(client)
        for day in days:
            t0 = time.time()
            try:
                r = await _get_day_lines(client, day=day, sucursal=suc)
                if r.get("success"):
                    ok += 1
                    if r.get("from_cache"):
                        cached += 1
                    else:
                        live += 1
                    print(f"OK {day} rows={len(r.get('rows') or [])} "
                          f"{'cache' if r.get('from_cache') else 'live'} "
                          f"{time.time()-t0:.1f}s", flush=True)
                else:
                    fail += 1
                    print(f"FAIL {day}: {r.get('error')}", flush=True)
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print(f"ERR {day}: {type(exc).__name__}: {exc}", flush=True)
    print(f"DONE ok={ok} (cache={cached} live={live}) fail={fail} "
          f"de {len(days)} dias", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
