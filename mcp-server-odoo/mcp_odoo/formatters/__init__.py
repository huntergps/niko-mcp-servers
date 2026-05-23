"""Channel-aware display formatters for MCP tool responses.

These formatters render plain text strings optimized for chat channels
(WhatsApp, Telegram) that do NOT render markdown tables. The structured
JSON returned by the tool is unchanged — formatters only build an
extra ``display_text`` string the LLM can copy verbatim to the user.

See ``whatsapp.py`` for the chat-friendly renderers.
"""
