FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY mcp_theos/ mcp_theos/

# Image cache lives under /var/cache (volume-mounted in docker-compose
# so the cache survives container recreate). Create the directory now
# so the process can write to it even before the first save.
RUN mkdir -p /var/cache/mcp-theos/images

EXPOSE 8086

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8086/health || exit 1

CMD ["uvicorn", "mcp_theos.server:app", "--host", "0.0.0.0", "--port", "8086"]
