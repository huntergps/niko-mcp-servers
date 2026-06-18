"""Tests for the RAG ↔ Supabase endpoint split (``RAG_REST_URL``).

The RAG embeddings store (``product_embeddings`` / ``partner_embeddings``
tables + the ``search_tenant_products`` / ``search_tenant_partners`` RPCs)
is being moved off Supabase onto its own Postgres/PostgREST stack.

These tests assert the routing contract:

  * RAG calls (the 2 RPCs + the 2 embedding tables) hit ``rag_rest_url``.
  * Everything else (verification_tokens, tenants, knowledge_facts,
    contact_profiles, …) keeps hitting ``supabase_url``.

When ``RAG_REST_URL`` is unset, ``rag_rest_url`` falls back to
``supabase_url`` (NO-OP) — also covered.

All network I/O is mocked through ``httpx.AsyncClient`` so we never leave
the process. We capture every URL the code touches and bucket it by host.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest


# ---------------------------------------------------------------------------
# Config: rag_rest_url default & override
# ---------------------------------------------------------------------------


class TestConfigRagRestUrl:
    def test_defaults_to_supabase_url_when_unset(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "http://supa-only:8000")
        monkeypatch.delenv("RAG_REST_URL", raising=False)
        from mcp_odoo import config as cfg

        importlib.reload(cfg)
        s = cfg.Settings()
        assert s.rag_rest_url == "http://supa-only:8000"

    def test_override_from_env(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "http://supa-only:8000")
        monkeypatch.setenv("RAG_REST_URL", "http://niko-rag-rest:3000")
        from mcp_odoo import config as cfg

        importlib.reload(cfg)
        s = cfg.Settings()
        assert s.rag_rest_url == "http://niko-rag-rest:3000"
        # The non-RAG URL is untouched by the override.
        assert s.supabase_url == "http://supa-only:8000"

    def teardown_method(self):
        # Restore the module to its env-default state for other tests.
        from mcp_odoo import config as cfg

        importlib.reload(cfg)


# ---------------------------------------------------------------------------
# Shared httpx mock that records every URL by host
# ---------------------------------------------------------------------------


def _make_recording_client(sink: dict[str, list[str]], rpc_payload=None):
    """Return a fake ``httpx.AsyncClient`` factory that records every URL it
    is asked to hit, keyed by hostname, and returns a benign 200 response.

    ``rpc_payload`` lets a test control what ``search_tenant_*`` returns so
    the calling code makes it past the empty-result branch when needed.
    """

    class _FakeResp:
        def __init__(self, url: str):
            self._url = url
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            if "/api/embed" in self._url:
                return {"embeddings": [[0.1, 0.2, 0.3]]}
            if rpc_payload is not None and "/rpc/" in self._url:
                return rpc_payload
            return []

    def _record(url: str):
        host = urlparse(url).netloc or url
        sink.setdefault(host, []).append(url)

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **kw):
            _record(url)
            return _FakeResp(url)

        async def get(self, url, *a, **kw):
            _record(url)
            return _FakeResp(url)

        async def patch(self, url, *a, **kw):
            _record(url)
            return _FakeResp(url)

    return _FakeClient


def _reload_transport_with(monkeypatch, *, supabase_url, rag_rest_url):
    """Reload config + transport so the module-level ``settings`` reflects the
    desired URLs, then return the transport module."""
    monkeypatch.setenv("SUPABASE_URL", supabase_url)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt-secret-32bytes-padding!!")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")
    monkeypatch.setenv("ODOO_ENCRYPTION_KEY", os.urandom(32).hex())
    if rag_rest_url is None:
        monkeypatch.delenv("RAG_REST_URL", raising=False)
    else:
        monkeypatch.setenv("RAG_REST_URL", rag_rest_url)

    from mcp_odoo import config as cfg

    importlib.reload(cfg)
    from mcp_odoo.transports import mcp_transport as t

    importlib.reload(t)
    return t


SUPA = "http://supa-host:8000"
RAG = "http://niko-rag-rest:3000"


def _hosts(sink: dict[str, list[str]]) -> set[str]:
    return set(sink.keys())


# ---------------------------------------------------------------------------
# Product search → RAG endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_product_search_hits_rag_url(monkeypatch):
    t = _reload_transport_with(monkeypatch, supabase_url=SUPA, rag_rest_url=RAG)
    sink: dict[str, list[str]] = {}
    # Empty RPC payload → ILIKE fallback also runs, so we exercise BOTH the
    # search_tenant_products RPC and the product_embeddings GET.
    fake_client = _make_recording_client(sink, rpc_payload=[])

    with (
        patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
        patch("httpx.AsyncClient", fake_client),
    ):
        await t._rag_search(
            query="laptop para oficina", top_k=10, offset=0, tenant_id="tenant-uuid",
        )

    rag_calls = sink.get(urlparse(RAG).netloc, [])
    assert any("/rpc/search_tenant_products" in u for u in rag_calls), sink
    assert any("/product_embeddings" in u for u in rag_calls), sink
    # The product RAG path must NEVER touch the supabase host.
    assert urlparse(SUPA).netloc not in sink, sink


# ---------------------------------------------------------------------------
# Partner search → RAG endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partner_search_hits_rag_url(monkeypatch):
    t = _reload_transport_with(monkeypatch, supabase_url=SUPA, rag_rest_url=RAG)
    sink: dict[str, list[str]] = {}
    fake_client = _make_recording_client(sink, rpc_payload=[])

    with (
        patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
        patch("httpx.AsyncClient", fake_client),
    ):
        await t._rag_search_partners(query="juan perez", top_k=5, tenant_id="tenant-uuid")

    rag_calls = sink.get(urlparse(RAG).netloc, [])
    assert any("/rpc/search_tenant_partners" in u for u in rag_calls), sink
    assert urlparse(SUPA).netloc not in sink, sink


# ---------------------------------------------------------------------------
# NO-OP: when RAG_REST_URL unset, RAG calls fall back to supabase_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_product_search_falls_back_to_supabase_when_rag_unset(monkeypatch):
    t = _reload_transport_with(monkeypatch, supabase_url=SUPA, rag_rest_url=None)
    sink: dict[str, list[str]] = {}
    fake_client = _make_recording_client(sink, rpc_payload=[])

    with (
        patch.object(t, "_get_tenant_slug", AsyncMock(return_value="tecnosmart")),
        patch("httpx.AsyncClient", fake_client),
    ):
        await t._rag_search(
            query="laptop", top_k=10, offset=0, tenant_id="tenant-uuid",
        )

    supa_calls = sink.get(urlparse(SUPA).netloc, [])
    assert any("/rpc/search_tenant_products" in u for u in supa_calls), sink
    # No separate RAG host exists in this config.
    assert urlparse(RAG).netloc not in sink, sink


# ---------------------------------------------------------------------------
# Non-RAG calls stay on supabase_url even when RAG_REST_URL is set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_tokens_stays_on_supabase(monkeypatch):
    t = _reload_transport_with(monkeypatch, supabase_url=SUPA, rag_rest_url=RAG)
    sink: dict[str, list[str]] = {}
    fake_client = _make_recording_client(sink)

    from mcp_odoo.config import settings as _s

    with patch("httpx.AsyncClient", fake_client):
        # _otp_generate(supabase_url, supabase_key, tenant_id, partner_id,
        # channel, channel_user_id, ...). The production caller passes
        # settings.supabase_url — we mirror that to prove OTP stays on Supabase.
        await t._otp_generate(
            _s.supabase_url, "key", "tenant-uuid", 42, "telegram", "u123",
        )

    supa_calls = sink.get(urlparse(SUPA).netloc, [])
    assert any("/verification_tokens" in u for u in supa_calls), sink
    assert urlparse(RAG).netloc not in sink, sink


def test_partner_create_sync_uses_rag_rest_url_for_embeddings(monkeypatch):
    """Source-level guard: the new-partner sync block reads ``rag_rest_url``
    for partner_embeddings (not supabase_url)."""
    t = _reload_transport_with(monkeypatch, supabase_url=SUPA, rag_rest_url=RAG)
    import inspect

    src = inspect.getsource(t)
    # The mixed update block must reference both URLs and split them.
    assert "rag_url = settings.rag_rest_url" in src
    assert 'f"{rag_url}/rest/v1/partner_embeddings?odoo_id=eq.' in src
    # contact_profiles in that same block must stay on supabase_url.
    assert 'f"{supabase_url}/rest/v1/contact_profiles' in src
