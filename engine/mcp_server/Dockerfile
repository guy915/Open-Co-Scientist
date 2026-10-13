# Local MCP binds IPv4; the Railway sibling binds IPv6 for its private network.
# Digest pins keep base images reproducible.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY mcp_server/pyproject.toml mcp_server/README.md mcp_server/LICENSE mcp_server/NOTICE ./mcp_server/

# Editable root-package mapping needs each listed subpackage directory before source COPY.
RUN mkdir -p ./mcp_server/tools/lit_review

RUN pip install --no-cache-dir -e ./mcp_server

COPY mcp_server/ ./mcp_server/

EXPOSE 8888

ENV COSCIENTIST_MCP_PORT=8888
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8888', timeout=5.0, trust_env=False).raise_for_status()" || exit 1

CMD ["uvicorn", "mcp_server.server:app", "--host", "0.0.0.0", "--port", "8888"]
