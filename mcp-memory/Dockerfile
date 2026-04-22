FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Build from this directory:
#   docker build -t mcp-memory .

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# MCP server code
COPY server.py .
COPY memory_provider.py .

# Vendored SupabaseMemoryProvider (originally in hermes-plugins/supabase-memory/plugin.py)
COPY plugin.py /app/plugin.py

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
