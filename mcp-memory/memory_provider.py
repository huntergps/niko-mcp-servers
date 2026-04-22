"""Re-export SupabaseMemoryProvider from the Hermes plugin.

This module makes the provider importable by the MCP server without
duplicating code. It resolves the plugin from multiple possible locations:

1. Sibling directory (local dev): ../hermes-plugins/supabase-memory/plugin.py
2. Same directory (Docker build): ./plugin.py
"""

import importlib.util
import os

_candidates = [
    # Local development — plugin lives in sibling directory
    os.path.abspath(os.path.join(
        os.path.dirname(__file__),
        "..", "hermes-plugins", "supabase-memory", "plugin.py",
    )),
    # Docker — plugin.py copied alongside server.py
    os.path.abspath(os.path.join(os.path.dirname(__file__), "plugin.py")),
]

SupabaseMemoryProvider = None

for path in _candidates:
    if os.path.exists(path):
        spec = importlib.util.spec_from_file_location("supabase_memory_plugin", path)
        _mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mod)
        SupabaseMemoryProvider = _mod.SupabaseMemoryProvider
        break

if SupabaseMemoryProvider is None:
    raise ImportError(
        "Cannot find supabase_memory plugin.py. "
        f"Searched: {_candidates}"
    )

__all__ = ["SupabaseMemoryProvider"]
