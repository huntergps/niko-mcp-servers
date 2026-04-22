"""Shared display formatters for MCP tool responses.

Everything in this module exists because of one rule: DO NOT return a string
to the LLM that its tokenizer may corrupt. Qwen2.5 and Qwen3-AWQ merge
4+ digit prices like "1383.00" into a BPE token whose detokenized form
drops the leading digit (observed: "$1383.03" -> "$383.03"). See
niko/docs/LLM_MODEL_TESTS.md Test 3 for evidence.

The fix: render prices with a COMMA thousands separator so BPE cannot merge
the whole digit run into one token ("USD 1,383.00" tokenizes safely).

We also attach the raw numeric field alongside each display string so the
LLM can do arithmetic without re-parsing the formatted text.
"""

from __future__ import annotations


def format_price_display(price: float | int | None, currency: str = "USD") -> str:
    """Render ``price`` as a tokenizer-safe display string.

    Examples:
        format_price_display(1383.03)  -> "USD 1,383.03"
        format_price_display(0)        -> "USD 0.00"
        format_price_display(None)     -> "consultar"
        format_price_display(1)        -> "USD 1.00"

    Always uses a comma thousands separator and two decimals. The comma is
    load-bearing: it splits the digit run at tokenization time so no BPE
    merge can swallow the leading digit.
    """
    if price is None:
        return "consultar"
    try:
        amount = float(price)
    except (TypeError, ValueError):
        return "consultar"
    # ``{:,.2f}`` is locale-independent in Python (always comma + dot, 2 dp).
    return f"{currency} {amount:,.2f}"
