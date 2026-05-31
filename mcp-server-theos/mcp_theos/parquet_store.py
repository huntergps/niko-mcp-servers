"""Almacén columnar (Parquet + DuckDB) para reportes sobre años de historia.

Motivación
----------
El caché día-por-día (``sales_report._get_day_lines``) resolvió "no re-pegarle
al ERP", pero NO escala a años: agregar en Python ~1.5 s/día significa ~9 min
por año, muy por encima del timeout MCP (240 s). Para histórico de varios años
se necesita una capa que agregue miles de días en sub-segundo.

Diseño
------
* **Fuente de verdad:** los ``.jsonl`` del caché (días pasados, inmutables).
  Este módulo NO habla con el ERP — solo transforma lo ya cacheado.
* **Parquet particionado por mes:** un archivo por ``(tenant, sucursal, YYYY-MM)``
  en ``$MOV_PARQUET_DIR`` (default ``/var/cache/mcp-theos/parquet``). Columnar +
  comprimido: una consulta lee solo las columnas que usa y se salta meses
  enteros por las estadísticas internas del archivo.
* **DuckDB:** motor SQL embebido (sin servidor) que consulta los Parquet
  directo (``SELECT ... FROM '.../*.parquet'``). El ``SUM``/``GROUP BY`` corre
  en C++ vectorizado → años en sub-segundo.
* **Conserva el dato crudo** (las mismas columnas ``_KEEP_KEYS``): cualquier
  reporte/desglose futuro sale con una consulta nueva, sin recomputar nada.

Es ADITIVO: la ruta ``.jsonl`` actual sigue intacta. Este módulo agrega la capa
columnar al lado. La integración en los reportes se hace por separado,
comparando totales número-a-número contra la ruta vieja.

Persistencia: ``$MOV_PARQUET_DIR`` va al mismo bind-mount host que el caché
(``/data/niko/mcp-theos-cache`` → ``/var/cache/mcp-theos``), así que sobrevive
rebuilds y restarts. NUNCA se borra en pruebas.
"""
from __future__ import annotations

import json as _json
import os as _os
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone
from pathlib import Path as _Path
from typing import Any

# Mismo conjunto de columnas crudas que guarda el caché .jsonl. Lo importamos
# de sales_report para que las dos capas no se desincronicen.
from mcp_theos.sales_report import (
    _KEEP_KEYS,
    _cache_path as _jsonl_cache_path,
    _cache_root as _jsonl_cache_root,
    _read_jsonl,
)

# Columnas tipadas del Parquet. Derivamos tipos de los campos crudos:
#   numéricas las que se suman/cuentan; texto el resto.
_NUM_COLS = ("PVP_LINEA", "PRECIO_NETO_LINEA", "CAN")
_INT_COLS = ("VENT_FACT_VENT", "INV_FAMI", "INV_BODEGA", "PRODUCTOS")

# Columnas DERIVADAS (v2) — calculadas UNA vez al escribir el Parquet con las
# mismas funciones que usa summarize_sales, para que la agregación SQL sea un
# GROUP BY limpio sin tocar Python por fila:
#   DAY       día ISO de la partición (= día de captura del caché)
#   LINE_DAY  día ISO derivado de FECHA_CONTA + offset ECU (lo que usa por_dia)
#   HOUR_ECU  hora entera de FECHA_CONTA + offset ECU (-1 si no parseable)
#   EST_PTO   "establecimiento-pto" parseado del NAME ("(sin caja)" si no)
#   CIF       cif del cliente (parseado del NAME)
#   CLIENTE   nombre del cliente (parseado del NAME)
_DERIVED_COLS = ("DAY", "LINE_DAY", "HOUR_ECU", "EST_PTO", "CIF", "CLIENTE")
# El orden de columnas del Parquet es estable (sorted crudas + derivadas).
_COLS = tuple(sorted(_KEEP_KEYS)) + _DERIVED_COLS

_PARQUET_DIR_ENV = _os.environ.get(
    "MOV_PARQUET_DIR", "/var/cache/mcp-theos/parquet",
)
# v2: Parquet enriquecido con columnas derivadas. Bumpear invalida los v1
# (se regeneran del jsonl con build_all, sin tocar el caché jsonl).
_PARQUET_VERSION = "v2"

# Offset horario Ecuador (UTC-5). Mismo que summarize_sales (VELNEO_TZ_OFFSET_HOURS).
try:
    _TZ_OFFSET = int(float(_os.environ.get("VELNEO_TZ_OFFSET_HOURS", "-5")))
except (TypeError, ValueError):
    _TZ_OFFSET = -5


def parquet_root() -> _Path:
    return _Path(_PARQUET_DIR_ENV)


def _safe(part: str) -> str:
    return (part or "default").replace("/", "_")


def month_path(tenant_id: str, sucursal: str, month: str) -> _Path:
    """Ruta del Parquet de un mes: .../<tenant>/<sucursal>/<YYYY-MM>.v1.parquet"""
    return (parquet_root() / _safe(tenant_id) / _safe(sucursal)
            / f"{month}.{_PARQUET_VERSION}.parquet")


def _today_ecu() -> _date:
    return (_datetime.now(_timezone.utc) - _timedelta(hours=5)).date()


# ---------------------------------------------------------------------------
# Coerción de tipos — las filas .jsonl traen todo como str/None.
# ---------------------------------------------------------------------------
def _to_float(v: Any) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _derive_day_hour(fecha_conta: Any) -> tuple[str, int]:
    """Replica EXACTA de la lógica de summarize_sales para LINE_DAY/HOUR_ECU.

    Parsea ``FECHA_CONTA`` (ISO con 'T') + offset ECU. Devuelve ("", -1) si no
    es parseable — igual que el bucle Python descarta esas líneas del por_dia.
    """
    fc = str(fecha_conta or "")
    if "T" not in fc:
        return "", -1
    try:
        dt = _datetime.strptime(
            fc.replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S"
        ) + _timedelta(hours=_TZ_OFFSET)
        return dt.date().isoformat(), dt.hour
    except ValueError:
        return "", -1


def _normalize_row(raw: dict[str, Any], day: str) -> dict[str, Any]:
    """Proyecta una fila cruda al esquema tipado del Parquet, con columnas
    derivadas (día/hora ECU + parseo de NAME) pre-calculadas."""
    # Import diferido para evitar ciclo a nivel de módulo.
    from mcp_theos.sales_report import _parse_invoice_name

    out: dict[str, Any] = {}
    for k in sorted(_KEEP_KEYS):
        v = raw.get(k)
        if k in _NUM_COLS:
            out[k] = _to_float(v)
        elif k in _INT_COLS:
            out[k] = _to_int(v)
        else:
            out[k] = "" if v is None else str(v)

    out["DAY"] = day
    line_day, hour = _derive_day_hour(raw.get("FECHA_CONTA"))
    out["LINE_DAY"] = line_day
    out["HOUR_ECU"] = hour

    parsed = _parse_invoice_name(raw.get("NAME") or "")
    est = parsed["establecimiento"]
    pto = parsed["pto_emision"]
    out["EST_PTO"] = f"{est}-{pto}" if est else "(sin caja)"
    out["CIF"] = parsed["cif"] or ""
    out["CLIENTE"] = parsed["cliente"] or (
        f"CIF {parsed['cif']}" if parsed["cif"] else "(sin cliente)"
    )
    return out


def _month_of(day: str) -> str:
    return day[:7]


# ---------------------------------------------------------------------------
# Conversión .jsonl -> Parquet (un archivo por mes).
# ---------------------------------------------------------------------------
def _iter_cached_days(tenant_id: str, sucursal: str) -> list[str]:
    """Lista los días que YA tienen .jsonl en el caché, ordenados."""
    base = _jsonl_cache_root() / _safe(tenant_id) / _safe(sucursal)
    if not base.exists():
        return []
    days = []
    suffix = ".jsonl"
    for p in base.iterdir():
        name = p.name
        if not name.endswith(suffix):
            continue
        # nombre: <YYYY-MM-DD>.<ver>.jsonl  -> el día es el primer token
        day = name.split(".")[0]
        if len(day) == 10 and day[4] == "-" and day[7] == "-":
            days.append(day)
    return sorted(set(days))


def build_month(tenant_id: str, sucursal: str, month: str,
                *, overwrite: bool = False) -> dict[str, Any]:
    """Escribe (o reescribe) el Parquet de un mes a partir de los .jsonl.

    Solo incluye días < hoy (los inmutables). El mes en curso se reescribe
    cada vez que se llama con ``overwrite`` (sus días siguen creciendo).
    Devuelve ``{success, month, days, rows, path}``.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    out_path = month_path(tenant_id, sucursal, month)
    if out_path.exists() and not overwrite:
        return {"success": True, "month": month, "skipped": True,
                "path": str(out_path)}

    today = _today_ecu()
    all_days = [d for d in _iter_cached_days(tenant_id, sucursal)
                if _month_of(d) == month and _date.fromisoformat(d) < today]
    if not all_days:
        return {"success": True, "month": month, "days": 0, "rows": 0,
                "path": None, "empty": True}

    cols: dict[str, list[Any]] = {c: [] for c in _COLS}
    n_rows = 0
    for day in all_days:
        path = _jsonl_cache_path(tenant_id, sucursal, day)
        for raw in _read_jsonl(path):
            row = _normalize_row(raw, day)
            for c in _COLS:
                cols[c].append(row[c])
            n_rows += 1

    # Esquema explícito → tipos estables en disco.
    fields = []
    for c in _COLS:
        if c in _NUM_COLS:
            fields.append(pa.field(c, pa.float64()))
        elif c in _INT_COLS:
            fields.append(pa.field(c, pa.int64()))
        else:
            fields.append(pa.field(c, pa.string()))
    schema = pa.schema(fields)
    table = pa.table({c: cols[c] for c in _COLS}, schema=schema)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(out_path)
    return {"success": True, "month": month, "days": len(all_days),
            "rows": n_rows, "path": str(out_path)}


def build_all(tenant_id: str, sucursal: str,
              *, overwrite_current_month: bool = True) -> dict[str, Any]:
    """Construye los Parquet de TODOS los meses con .jsonl cacheado.

    Meses pasados completos se saltan si ya existen (inmutables). El mes en
    curso se reescribe (sus días crecen). Idempotente y barato de re-correr.
    """
    days = _iter_cached_days(tenant_id, sucursal)
    months = sorted({_month_of(d) for d in days})
    current_month = _today_ecu().isoformat()[:7]
    results = []
    for m in months:
        ow = (m == current_month) and overwrite_current_month
        results.append(build_month(tenant_id, sucursal, m, overwrite=ow))
    total_rows = sum(r.get("rows", 0) for r in results)
    return {"success": True, "months": len(months),
            "rows": total_rows, "results": results}


# ---------------------------------------------------------------------------
# Consulta DuckDB sobre los Parquet.
# ---------------------------------------------------------------------------
def _glob(tenant_id: str, sucursal: str) -> str:
    base = parquet_root() / _safe(tenant_id) / _safe(sucursal)
    return str(base / f"*.{_PARQUET_VERSION}.parquet")


def query(tenant_id: str, sucursal: str, sql_where: str = "TRUE") -> "Any":
    """Devuelve una conexión DuckDB con la vista ``movs`` sobre los Parquet.

    ``sql_where`` se interpola directo en el ``CREATE VIEW`` — DuckDB NO acepta
    parámetros preparados (``?``) en un CREATE VIEW. Por eso quien construya el
    filtro debe usar literales ya validados (ver :func:`connect_range`, que
    valida las fechas como ISO antes de inyectarlas). No pasar entrada de
    usuario sin validar.

    Uso típico desde otro módulo:
        con = connect_range(tenant, suc, date_from, date_to)
        con.execute("SELECT INV_FAMI, SUM(PVP_LINEA) FROM movs GROUP BY 1")
    """
    import duckdb
    con = duckdb.connect(database=":memory:")
    con.execute(
        f"CREATE VIEW movs AS SELECT * FROM read_parquet('{_glob(tenant_id, sucursal)}') "
        f"WHERE {sql_where}"
    )
    return con


def _iso_or_raise(value: str) -> str:
    """Valida que ``value`` sea una fecha ISO (YYYY-MM-DD) y la devuelve.

    Defensa contra inyección: el resultado se interpola como literal SQL, así
    que solo permitimos una fecha bien formada.
    """
    day = (value or "")[:10]
    _date.fromisoformat(day)  # lanza ValueError si no es ISO
    return day


def connect_range(tenant_id: str, sucursal: str,
                  date_from: str, date_to: str) -> "Any":
    """Conexión DuckDB con la vista ``movs`` acotada al rango [from, to]."""
    d_from = _iso_or_raise(date_from)
    d_to = _iso_or_raise(date_to)
    return query(tenant_id, sucursal,
                 f"DAY >= '{d_from}' AND DAY <= '{d_to}'")


def covered_days(tenant_id: str, sucursal: str,
                 date_from: str, date_to: str) -> set[str]:
    """Días (ISO) del rango que YA están en Parquet, listos para servir.

    Devuelve un set vacío si no hay Parquet. Se usa para decidir, día por día,
    si la fila se lee de Parquet (rápido) o se cae al jsonl/ERP.
    """
    base = parquet_root() / _safe(tenant_id) / _safe(sucursal)
    if not base.exists() or not list(base.glob(f"*.{_PARQUET_VERSION}.parquet")):
        return set()
    try:
        d_from = _iso_or_raise(date_from)
        d_to = _iso_or_raise(date_to)
    except ValueError:
        return set()
    con = connect_range(tenant_id, sucursal, d_from, d_to)
    rows = con.execute("SELECT DISTINCT DAY FROM movs").fetchall()
    con.close()
    return {r[0] for r in rows}


def read_range_rows_by_day(tenant_id: str, sucursal: str,
                           date_from: str, date_to: str) -> dict[str, list[dict[str, Any]]]:
    """Lee del Parquet las filas del rango, agrupadas por día ISO.

    Cada fila vuelve como dict con las MISMAS claves ``_KEEP_KEYS`` (uppercase)
    que produce el caché jsonl, para que el consumidor (el bucle de
    ``summarize_sales``) no note la diferencia. Los tipos numéricos vuelven
    como float/int; el resto como str — el bucle ya hace su propia coerción.

    Esto es lo que hace viable agregar años: DuckDB lee el Parquet columnar en
    sub-segundo en vez de parsear cientos de miles de líneas jsonl en Python.
    """
    base = parquet_root() / _safe(tenant_id) / _safe(sucursal)
    if not base.exists() or not list(base.glob(f"*.{_PARQUET_VERSION}.parquet")):
        return {}
    try:
        d_from = _iso_or_raise(date_from)
        d_to = _iso_or_raise(date_to)
    except ValueError:
        return {}

    con = connect_range(tenant_id, sucursal, d_from, d_to)
    # Proyectamos exactamente las columnas crudas (sin DAY, que es de partición)
    # en el orden estable de _COLS para reconstruir los dicts.
    raw_cols = [c for c in _COLS if c != "DAY"]
    col_list = ", ".join(f'"{c}"' for c in raw_cols)
    cur = con.execute(f"SELECT DAY, {col_list} FROM movs")
    out: dict[str, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        day = row[0]
        rec = {raw_cols[i]: row[i + 1] for i in range(len(raw_cols))}
        out.setdefault(day, []).append(rec)
    con.close()
    return out


def coverage(tenant_id: str, sucursal: str) -> dict[str, Any]:
    """Diagnóstico: qué rango de días cubren los Parquet existentes."""
    import duckdb
    glob = _glob(tenant_id, sucursal)
    base = parquet_root() / _safe(tenant_id) / _safe(sucursal)
    files = sorted(base.glob(f"*.{_PARQUET_VERSION}.parquet")) if base.exists() else []
    if not files:
        return {"success": True, "files": 0, "days": 0, "rows": 0,
                "min_day": None, "max_day": None}
    con = duckdb.connect(database=":memory:")
    row = con.execute(
        f"SELECT COUNT(*) AS rows, COUNT(DISTINCT DAY) AS days, "
        f"MIN(DAY) AS min_day, MAX(DAY) AS max_day "
        f"FROM read_parquet('{glob}')"
    ).fetchone()
    con.close()
    return {"success": True, "files": len(files), "rows": row[0],
            "days": row[1], "min_day": row[2], "max_day": row[3]}
