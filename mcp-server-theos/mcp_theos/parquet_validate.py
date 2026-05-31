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


_MONEY_TOL = 0.01  # tolerancia: artefacto de no-asociatividad de punto flotante


def _deep_equal(a, b, path="", diffs=None):
    """Compara tolerando <=1 centavo en floats (float non-associativity).
    Acumula en ``diffs`` los caminos con diferencia REAL (>1 centavo o
    estructural). Devuelve (igual_bool, max_money_delta)."""
    if diffs is None:
        diffs = []
    maxd = 0.0
    if isinstance(a, float) or isinstance(b, float):
        try:
            delta = abs(float(a) - float(b))
        except (TypeError, ValueError):
            diffs.append((path, a, b)); return False, maxd
        maxd = delta
        if delta > _MONEY_TOL:
            diffs.append((path, a, b))
        return delta <= _MONEY_TOL, maxd
    if isinstance(a, dict) and isinstance(b, dict):
        ok = True
        for k in set(a) | set(b):
            e, d = _deep_equal(a.get(k), b.get(k), f"{path}.{k}", diffs)
            ok = ok and e; maxd = max(maxd, d)
        return ok, maxd
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append((f"{path}[len]", len(a), len(b))); return False, maxd
        ok = True
        for i, (x, y) in enumerate(zip(a, b)):
            e, d = _deep_equal(x, y, f"{path}[{i}]", diffs)
            ok = ok and e; maxd = max(maxd, d)
        return ok, maxd
    if a != b:
        diffs.append((path, a, b)); return False, maxd
    return True, maxd


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

        diffs: list = []
        ok, maxd = _deep_equal(_strip(r_pq), _strip(r_js), diffs=diffs)
        speedup = (t_js / t_pq) if t_pq else float("inf")
        if ok:
            print(f"IDENTICOS ✓ (tol<=1cent)  speedup parquet={speedup:.1f}x  "
                  f"max_delta=${maxd:.4f}  total_pvp={r_pq['totals']['pvp']:,.2f}")
        else:
            print(f"DIFIEREN ✗  speedup={speedup:.1f}x  max_delta=${maxd:.4f}  "
                  f"diffs_reales={len(diffs)}")
            for path, va, vb in diffs[:15]:
                print(f"  {path}: parquet={va!r}  jsonl={vb!r}")


if __name__ == "__main__":
    asyncio.run(main())
