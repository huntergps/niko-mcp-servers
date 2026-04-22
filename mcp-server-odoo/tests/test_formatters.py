"""Tests for tool response formatters.

The comma thousands separator in format_price_display() is load-bearing:
it keeps prices readable to BPE tokenizers that otherwise eat the leading
digit of 4+ digit runs. These tests lock the output format so a future
refactor does not regress the bug described in
niko/docs/LLM_MODEL_TESTS.md Test 3.
"""

from mcp_odoo.tools.formatters import format_price_display


def test_four_digit_price_uses_comma_separator():
    # The original bug: "$1383.03" -> Qwen tokenizer renders "$383.03".
    # With a comma separator the digit run is split at tokenization time.
    assert format_price_display(1383.03) == "USD 1,383.03"


def test_seven_digit_price_uses_two_commas():
    assert format_price_display(1_234_567.89) == "USD 1,234,567.89"


def test_small_price_still_gets_two_decimals():
    assert format_price_display(1) == "USD 1.00"
    assert format_price_display(0.5) == "USD 0.50"


def test_zero_is_rendered_not_consultar():
    # 0 is a real price (free sample, for example). Only None / non-numeric
    # should collapse to "consultar".
    assert format_price_display(0) == "USD 0.00"


def test_none_becomes_consultar():
    assert format_price_display(None) == "consultar"


def test_non_numeric_becomes_consultar():
    assert format_price_display("not-a-number") == "consultar"  # type: ignore[arg-type]


def test_custom_currency_code():
    assert format_price_display(1383, currency="EUR") == "EUR 1,383.00"


def test_no_dollar_sign_ever_emitted():
    # The "$" character is the one that triggers the tokenizer merge.
    # Make sure we never emit it regardless of input.
    for value in [1, 1383, 1_234_567.89, 0, 0.01]:
        assert "$" not in format_price_display(value)
