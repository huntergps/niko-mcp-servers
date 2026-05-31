"""Backfill + benchmark del almacén Parquet a partir del caché .jsonl.

Corre DENTRO del contenedor (lee /var/cache/mcp-theos, bind-mount host). No
habla con el ERP. Idempotente, no borra .jsonl ni Parquet existentes.

Uso:
  python -m mcp_theos.parquet_backfill <tenant_id>            # backfill + coverage
  python -m mcp_theos.parquet_backfill <tenant_id> --bench    # + benchmark duckdb vs jsonl
  python -m mcp_theos.parquet_backfill <tenant_id> <sucursal> # solo esa sucursal
"""
import sys
import time
from pathlib import Path

from mcp_theos import parquet_store as ps
from mcp_theos.sales_report import _cache_root, _read_jsonl, _cache_path


def _discover_sucursales(tenant_id: str) -> list[str]:
    base = _cache_root() / tenant_id.replace("/", "_")
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def _bench(tenant_id: str, sucursal: str) -> None:
    """Compara: agregar PVP por familia leyendo jsonl vs DuckDB sobre Parquet."""
    days = ps._iter_cached_days(tenant_id, sucursal)
    if not days:
        print("  (sin dias para benchmark)")
        return
    d_from, d_to = days[0], days[-1]
    n = len(days)

    # --- Camino actual: leer cada .jsonl y sumar en Python ---
    t0 = time.time()
    agg: dict = {}
    total = 0.0
    for day in days:
        for raw in _read_jsonl(_cache_path(tenant_id, sucursal, day)):
            try:
                pvp = float(raw.get("PVP_LINEA") or 0)
            except (TypeError, ValueError):
                pvp = 0.0
            fam = str(raw.get("INV_FAMI") or 0)
            agg[fam] = agg.get(fam, 0.0) + pvp
            total += pvp
    t_jsonl = time.time() - t0

    # --- Camino nuevo: DuckDB sobre Parquet ---
    t0 = time.time()
    con = ps.connect_range(tenant_id, sucursal, d_from, d_to)
    rows = con.execute(
        "SELECT CAST(INV_FAMI AS VARCHAR) AS fam, SUM(PVP_LINEA) AS pvp "
        "FROM movs GROUP BY 1"
    ).fetchall()
    total_pq = con.execute("SELECT SUM(PVP_LINEA) FROM movs").fetchone()[0] or 0.0
    con.close()
    t_duck = time.time() - t0

    speedup = (t_jsonl / t_duck) if t_duck else float("inf")
    diff = abs(total - total_pq)
    print(f"  BENCH {sucursal}: {n} dias ({d_from}..{d_to})")
    print(f"    jsonl+python: {t_jsonl:.3f}s  total_pvp={total:,.2f}")
    print(f"    duckdb+parquet: {t_duck:.3f}s  total_pvp={total_pq:,.2f}  "
          f"familias={len(rows)}")
    print(f"    speedup={speedup:.1f}x  diff_total={diff:.4f} "
          f"{'OK (identico)' if diff < 0.01 else 'WARN: difieren!'}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    tenant_id = sys.argv[1]
    do_bench = "--bench" in sys.argv
    pos = [a for a in sys.argv[2:] if not a.startswith("--")]
    sucursales = [pos[0]] if pos else _discover_sucursales(tenant_id)
    if not sucursales:
        print(f"No hay sucursales cacheadas para tenant {tenant_id}")
        sys.exit(1)

    print(f"Parquet dir: {ps.parquet_root()}")
    for suc in sucursales:
        t0 = time.time()
        res = ps.build_all(tenant_id, suc)
        cov = ps.coverage(tenant_id, suc)
        print(f"[{suc}] meses={res['months']} filas={res['rows']:,} "
              f"({time.time()-t0:.1f}s) | cobertura: "
              f"{cov['files']} parquet, {cov['days']} dias, "
              f"{cov['rows']:,} filas, {cov['min_day']}..{cov['max_day']}")
        if do_bench:
            _bench(tenant_id, suc)


if __name__ == "__main__":
    main()
