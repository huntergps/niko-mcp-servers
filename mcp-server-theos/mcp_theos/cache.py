"""In-process caches for hot paths.

Two layers, both keyed off a string and bounded by both **count** and
**TTL**. The implementation is deliberately tiny — no Redis, no
expiration thread, no LRU bookkeeping per get. Hot paths read directly;
the periodic janitor runs O(n) on the dict when its size crosses the
cap. Trade-off: maximum simplicity at the cost of slightly stale TTLs
under burst load. Good enough for what we have today.

Two caches live here:

* ``embedding_cache`` — hash(query) → list[float]. Used by
  ``mcp_theos.rag.embed_query`` so re-asking the same partner / product
  in quick succession does not re-hit Ollama. TTL 1 hour.

* ``response_cache`` — (tenant_id, method, path, query_hash) →
  (status, body). Used by VelneoClient.get to dedupe identical
  read-only calls within a short window. TTL 30 s. Skipped for
  writes (POST/PATCH/DELETE).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any


class _TTLCache:
    def __init__(self, *, ttl_s: float, max_entries: int):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if not entry:
            return None
        ts, value = entry
        if time.monotonic() - ts > self.ttl_s:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._store) >= self.max_entries:
            # Drop oldest 10 % to avoid linear scans on every set.
            cutoff = sorted(self._store.values(), key=lambda v: v[0])[
                int(self.max_entries * 0.1)
            ][0]
            self._store = {
                k: v for k, v in self._store.items() if v[0] >= cutoff
            }
        self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# 1 hour TTL, ~5 000 query embeddings before rotation
embedding_cache = _TTLCache(ttl_s=3600.0, max_entries=5000)

# 30 s TTL, ~2 000 Velneo responses before rotation
response_cache = _TTLCache(ttl_s=30.0, max_entries=2000)


def make_response_key(
    tenant_id: str,
    method: str,
    path: str,
    params: dict[str, Any] | None,
) -> str:
    """Stable cache key for a Velneo REST call."""
    payload = json.dumps(
        [tenant_id, method.upper(), path, params or {}],
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_embedding_key(model: str, text: str) -> str:
    return hashlib.sha256(
        f"{model}|{(text or '').strip().lower()}".encode("utf-8")
    ).hexdigest()
