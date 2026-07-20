# Docling MCP server image. Build context is the REPO ROOT (so it can COPY the shared
# mcp_filebridge package). Docling pulls torch + layout/OCR models; the models download on the
# FIRST conversion and cache into the /data volume (HF_HOME) so restarts are fast.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY examples/docling/requirements.txt examples/docling/requirements.txt
RUN pip install --no-cache-dir -r examples/docling/requirements.txt

# The shared library (python/), installed editable; deps already from requirements.txt → --no-deps.
COPY python/ /app/python/
RUN pip install --no-cache-dir -e ./python --no-deps
COPY examples/docling/server.py /app/examples/docling/server.py

RUN mkdir -p /data && chown 10001:10001 /data
USER app
# HF_HOME → the models cache in the volume; MCP_OAUTH_STATE_PATH → OAuth registry survives redeploys.
ENV MCP_HOST=0.0.0.0 MCP_PORT=9500 HF_HOME=/data/hf MCP_OAUTH_STATE_PATH=/data/oauth_state.json \
    MCP_ALLOW_EXTERNAL_BIND=0
EXPOSE 9500

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os,socket,sys; p=int(os.environ.get('MCP_PORT','9500')); s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',p))==0 else 1)"]

CMD ["python", "examples/docling/server.py"]
