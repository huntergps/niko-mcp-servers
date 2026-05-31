"""Valida que summarize_sales dé IDÉNTICO con Parquet vs sin Parquet (jsonl).

Corre dentro del contenedor. Llama summarize_sales dos veces sobre el mismo
rango: una con la capa Parquet activa y otra forzando el fallback jsonl
(MOV_PARQUET_DISABLE), y compara los dicts de respuesta campo por campo.

Uso:
  python -m mcp_theos.parquet_validate <tenant_id> <date_from> <date_to>
"""
import sys
import os
import json
import asyncio
import time

from mcp_theos.tenant_resolver import _resolve_by_id
from mcp_theos.velneo_http import VelneoClient
from mcp_theos.tools.admin_ops import _tenant_sucursal


# Campos volátiles que pueden diferir legítimamente entre corridas (stats de
# origen de datos, no datos de negocio). Se excluyen de la comparación.
_VOLATILE = {"cache_stats"}


def _strip(d):
    if isinstance(d, dict):
        return {k: _strip(v) for k, v in d.items() if k not in _VOLATILE}
    if isinstance(d, list):
        return [_strip(x) for x in d]
    return d


async def _run(client, df, dt, disable_pq):
    if disable_pq:
        os.environ["MOV_PARQUET_DISABLE"] = "1"
    else:
        os.environ.pop("MOV_PARQUET_DISABLE", None)
    from mcp_theos.sales_report import summarize_sales
    t0 = time.time()
    r = await summarize_sales(client, date_from=df, date_to=dt,
                              include_cross_tabs=True,
                              top_n_clientes=20, top_n_productos=50)
    return r, time.time() - t0


async def main():
    tenant_id, df, dt = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = await _resolve_by_id(tenant_id)
    async with VelneoClient(cfg) as client:
        suc = _tenant_sucursal(client)
        print(f"Rango {df}..{dt} sucursal {suc}")

        r_pq, t_pq = await _run(client, df, dt, disable_pq=False)
        print(f"[parquet] {t_pq:.2f}s  stats={r_pq.get('cache_stats')}")

        r_js, t_js = await _run(client, df, dt, disable_pq=True)
        print(f"[jsonl]   {t_js:.2f}s  stats={r_js.get('cache_stats')}")

        a = json.dumps(_strip(r_pq), sort_keys=True, ensure_ascii=False)
        b = json.dumps(_strip(r_js), sort_keys=True, ensure_ascii=False)

        if a == b:
            speedup = (t_js / t_pq) if t_pq else float("inf")
            print(f"IDENTICOS ✓   speedup parquet={speedup:.1f}x  "
                  f"total_pvp={r_pq['totals']['pvp']:,.2f}")
        else:
            print("DIFIEREN ✗ — buscando primer campo distinto...")
            da, db = _strip(r_pq), _strip(r_js)
            for k in sorted(set(da) | set(db)):
                va = json.dumps(da.get(k), sort_keys=True, ensure_ascii=False)
                vb = json.dumps(db.get(k), sort_keys=True, ensure_ascii=False)
                if va != vb:
                    print(f"  campo '{k}':")
                    print(f"    parquet: {va[:300]}")
                    print(f"    jsonl:   {vb[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
