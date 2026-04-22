FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY mcp_odoo/ mcp_odoo/

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8085/health || exit 1

CMD ["uvicorn", "mcp_odoo.server:app", "--host", "0.0.0.0", "--port", "8085"]
