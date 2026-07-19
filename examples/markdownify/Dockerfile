# markdownify gateway image: Python OAuth gateway + Node TS backend + markitdown.
# Build context is the REPO ROOT (so it can COPY ts/ and examples/convertx/oauth.py).
FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv curl && rm -rf /var/lib/apt/lists/*

# Python deps in a venv (bookworm pip is externally-managed). markitdown[pdf,docx,pptx,xlsx]
# is the light set — the office/PDF formats ConvertX can't do to markdown. Add [all] for
# audio transcription + image OCR (heavy).
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir fastmcp==3.4.2 uvicorn "markitdown[pdf,docx,pptx,xlsx]" anyio requests boto3
ENV PATH=/venv/bin:$PATH MARKITDOWN_BIN=markitdown

WORKDIR /app
COPY ts/ /app/ts/
COPY examples/convertx/oauth.py /app/examples/convertx/oauth.py
COPY examples/markdownify/package.json examples/markdownify/package-lock.json /app/examples/markdownify/
RUN cd /app/examples/markdownify && (npm ci --omit=dev || npm install --omit=dev)
COPY examples/markdownify/server.mjs examples/markdownify/gateway.py /app/examples/markdownify/

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown 10001:10001 /data
USER app
ENV MCP_HOST=0.0.0.0 MCP_PORT=8090 MCP_OAUTH_STATE_PATH=/data/oauth_state.json
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import os,socket,sys; p=int(os.environ.get('MCP_PORT','8090')); s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',p))==0 else 1)"]

WORKDIR /app/examples/markdownify
CMD ["python", "gateway.py"]
