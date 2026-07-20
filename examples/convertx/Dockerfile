# ConvertX MCP server image. Build context is the REPO ROOT (so it can COPY the shared
# mcp_filebridge package and this example) — see docker-compose.yml build.context.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY examples/convertx/requirements.txt examples/convertx/requirements.txt
RUN pip install --no-cache-dir -r examples/convertx/requirements.txt

# The shared library (oauth + widget + s3_filebridge), installed editable; deps already
# came from requirements.txt, so --no-deps. Then the example itself.
COPY pyproject.toml /app/pyproject.toml
COPY mcp_filebridge/ /app/mcp_filebridge/
RUN pip install --no-cache-dir -e . --no-deps
COPY examples/convertx/server.py /app/examples/convertx/server.py

# Create the OAuth-state mount point owned by the runtime user, so a fresh named
# volume mounted at /data inherits app ownership (Docker seeds an empty volume from
# the image path) — otherwise it lands root-owned and state can't persist.
RUN mkdir -p /data && chown 10001:10001 /data

USER app
ENV MCP_HOST=0.0.0.0 MCP_PORT=9400
EXPOSE 9400

# Liveness: stdlib-only TCP connect to the listener (no curl/wget needed; never
# touches the bearer-gated /mcp route). Reads MCP_PORT from env at runtime.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,socket,sys; p=int(os.environ.get('MCP_PORT','9400')); s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',p))==0 else 1)"]

CMD ["python", "examples/convertx/server.py"]
