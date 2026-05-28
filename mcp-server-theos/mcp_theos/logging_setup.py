"""Structured JSON logging for the MCP server.

Single-line JSON per log record so the records are stream-friendly
(Loki / journalctl-to-json / Datadog tail / etc.). Each record carries
the usual ``ts / level / logger / message`` plus any extra fields the
caller attached via ``logger.info("...", extra={...})``.

Specifically, the MCP transport instruments each tool call with:

    tenant_id    — resolved Velneo tenant id
    tool         — the MCP tool name dispatched
    latency_ms   — how long the tool function took
    ok           — True / False (False on exceptions or success=False)
    error        — short error name when ok=False
    channel      — Telegram / Slack / etc., when known
    channel_user_id

So an operator can grep ``tool=identify_customer`` or
``tenant_id=d1507ef9-...`` straight out of the container's stdout.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


_BASE_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Any extras passed via logger.info("...", extra={...}).
        for k, v in record.__dict__.items():
            if k in _BASE_KEYS or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_structured_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger's StreamHandler.

    Idempotent — calling twice replaces the previous handler. We do
    NOT touch loggers that already have their own handlers (httpx,
    uvicorn) so their own formatting stays intact.
    """
    root = logging.getLogger()
    # Drop any previous handlers we installed.
    for h in list(root.handlers):
        if getattr(h, "_mcp_theos_json", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._mcp_theos_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
