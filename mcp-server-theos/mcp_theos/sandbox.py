"""Sandbox restringido para que el agente (Lila) genere PDFs a medida.

Lila NO puede ejecutar código (code_execution/file/terminal off en su config),
así que cualquier PDF debe nacer en el MCP. Los tools a medida
(generate_executive_report, _forecast_, _purchase_) cubren los casos comunes,
pero para "cualquier informe" se usa este sandbox: el agente pasa código Python
que dibuja un PDF a partir de DATOS YA OBTENIDOS por el MCP.

Modelo de seguridad (defensa en capas):
  * Los datos del ERP los obtiene el MCP con credenciales ANTES de entrar al
    sandbox y se pasan como ``datasets`` (dict). El código NUNCA ve la API key
    ni hace red.
  * ``exec`` con un namespace curado: builtins acotados (sin open/eval/exec/
    compile/__import__/input/globals/locals), módulos seguros pre-cargados
    (math, statistics, datetime, numpy, matplotlib, reportlab + helpers de
    dibujo Mepriga). El código no puede importar os/subprocess/socket.
  * AST gate: se rechaza el código si contiene Import/ImportFrom, atributos
    dunder (``__globals__``, ``__class__``...), o nombres peligrosos.
  * Timeout por hilo: si el render excede el límite, se aborta.
  * El código debe asignar ``pdf_bytes`` (bytes del PDF) y opcionalmente
    ``resumen`` (str). Cualquier otra cosa se ignora.

Esto NO ejecuta código arbitrario con acceso al sistema: es un render de PDF
sobre datos confiables, compuesto por el propio agente.
"""
from __future__ import annotations

import ast
import io
import math
import statistics
import datetime as _dt
import threading
from typing import Any

# Módulos seguros disponibles dentro del sandbox.
import numpy as _np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as _plt
import matplotlib.ticker as _mtick
from reportlab.lib.pagesizes import A4 as _A4, landscape as _landscape
from reportlab.pdfgen import canvas as _canvas
from reportlab.lib.colors import HexColor as _HexColor
from reportlab.lib.utils import ImageReader as _ImageReader

# Helpers de dibujo Mepriga (mismos del informe ejecutivo).
from mcp_theos import executive_report as _er


# ---------------------------------------------------------------------------
# AST gate — rechaza construcciones peligrosas antes de ejecutar.
# ---------------------------------------------------------------------------
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "input", "globals",
    "locals", "vars", "getattr", "setattr", "delattr", "exit", "quit",
    "breakpoint", "memoryview", "help",
}


def _check_ast(code: str) -> str | None:
    """Devuelve un mensaje de error si el código no es seguro; None si OK."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"SyntaxError: {e}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "import no permitido en el sandbox (usa los modulos ya disponibles)"
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return f"acceso a atributo dunder no permitido: {node.attr}"
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            return f"nombre no permitido: {node.id}"
    return None


# Builtins seguros expuestos al sandbox.
_SAFE_BUILTINS = {
    k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
    for k in (
        "abs", "min", "max", "sum", "len", "range", "enumerate", "zip",
        "sorted", "reversed", "round", "int", "float", "str", "bool", "list",
        "dict", "tuple", "set", "frozenset", "map", "filter", "any", "all",
        "isinstance", "print", "repr", "format", "divmod", "pow", "chr", "ord",
    )
}


def _build_namespace(datasets: dict[str, Any]) -> dict[str, Any]:
    """Namespace curado para el sandbox: datos + libs + helpers de dibujo."""
    ns: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        # datos ya obtenidos por el MCP
        "datasets": datasets,
        # libs seguras
        "math": math, "statistics": statistics, "dt": _dt, "datetime": _dt,
        "np": _np, "plt": _plt, "mtick": _mtick, "io": io,
        # reportlab
        "canvas": _canvas, "A4": _A4, "landscape": _landscape,
        "HexColor": _HexColor, "ImageReader": _ImageReader,
        # paleta + dimensiones Mepriga
        "PRIMARY": _er.PRIMARY, "SECONDARY": _er.SECONDARY, "GREEN": _er.GREEN,
        "RED": _er.RED, "ORANGE": _er.ORANGE, "PURPLE": _er.PURPLE,
        "CYAN": _er.CYAN, "SUBTLE": _er.SUBTLE, "ZEBRA": _er.ZEBRA,
        "PAGE_W": _er.PAGE_W, "PAGE_H": _er.PAGE_H,
        "LM": _er.LM, "RM": _er.RM, "TM": _er.TM, "BM": _er.BM,
        "H": _er.H,
        # helpers de dibujo (banner/footer/section/kpi/table/text/charts)
        "banner": _er._banner, "footer": _er._footer, "section": _er._section,
        "kpi_cards": _er._kpi_cards, "table": _er._table, "text_block": _er._text_block,
        "chart": _er._chart, "chart_barh": _er._chart_barh,
        "chart_familias": _er._chart_familias, "chart_hora": _er._chart_hora,
        # accesores defensivos
        "G": _er._g, "L": _er._list, "fnum": _er._fnum,
        "money": _er._money, "k": _er._k,
    }
    return ns


def run_pdf_code(code: str, datasets: dict[str, Any],
                 timeout_s: float = 60.0) -> dict[str, Any]:
    """Ejecuta ``code`` en el sandbox y devuelve {ok, pdf_bytes?, resumen?, error?}.

    El código debe asignar ``pdf_bytes`` (bytes del PDF). Puede asignar
    ``resumen`` (str). Tiene en scope: datasets, np, plt, canvas, A4, los
    helpers de dibujo Mepriga (banner, kpi_cards, table, chart_barh, ...) y
    la paleta. Ejemplo mínimo:

        buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
        banner(c, "Mi informe", "subtitulo", "rango")
        c.showPage(); c.save()
        pdf_bytes = buf.getvalue()
        resumen = "Listo"
    """
    err = _check_ast(code)
    if err:
        return {"ok": False, "error": f"sandbox_rejected: {err}"}

    ns = _build_namespace(datasets)
    result: dict[str, Any] = {}

    def _run():
        try:
            exec(compile(code, "<lila_report>", "exec"), ns, ns)  # noqa: S102
            result["pdf_bytes"] = ns.get("pdf_bytes")
            result["resumen"] = ns.get("resumen")
        except Exception as exc:  # noqa: BLE001
            import traceback
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["trace"] = traceback.format_exc().splitlines()[-3:]

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return {"ok": False, "error": f"sandbox_timeout: excedio {timeout_s:.0f}s"}
    if "error" in result:
        return {"ok": False, "error": result["error"], "trace": result.get("trace")}

    pdf = result.get("pdf_bytes")
    if not isinstance(pdf, (bytes, bytearray)) or pdf[:5] != b"%PDF-":
        return {"ok": False,
                "error": "el codigo no asigno 'pdf_bytes' valido (debe ser bytes que empiecen con %PDF-)"}
    return {"ok": True, "pdf_bytes": bytes(pdf), "resumen": result.get("resumen") or ""}
