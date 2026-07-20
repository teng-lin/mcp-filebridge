"""OAuth-terminating gateway that PROXIES to the markdownify TS backend (stdio).

The gateway (Python) owns OAuth (reusing examples/convertx/oauth.py) and forwards
/mcp to the TS server — so a TypeScript MCP server works on claude.ai/ChatGPT
WITHOUT re-implementing OAuth in TS. Validated pattern (see the gw-spike):
tools/call flows client → gateway(auth) → proxy → backend → back.

The convert-on-upload route (POST /u/convert/<ticket>) lives in _convert.py — the widget
POSTs bytes there and the server converts on receipt (mirrors notebooklm's /files/ul), so
no widget-initiated tool call is needed (the bit claude.ai doesn't do).
"""
import os
import sys
from pathlib import Path

import uvicorn
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))  # so `mcp_filebridge` imports without `pip install`
from mcp_filebridge import oauth  # noqa: E402  (build_auth / get_oauth_config / build_oauth_provider)
from mcp_filebridge.convert import register_convert_route  # noqa: E402

HERE = Path(__file__).resolve().parent
BACKEND = str(HERE / "server.mjs")

# Spawn the TS backend over stdio, passing the S3 + public-URL env through to it.
backend = ProxyClient({"mcpServers": {"markdownify": {
    "command": "node", "args": [BACKEND], "env": {**os.environ}}}})

oauth_cfg = oauth.get_oauth_config()  # None unless MCP_OAUTH_PASSWORD + BASE_URL set → bearer-only
provider = oauth.build_oauth_provider(oauth_cfg) if oauth_cfg else None
auth = oauth.build_auth(os.environ.get("MCP_BEARER_TOKEN") or None, provider)

gw = create_proxy(backend, name="markdownify-gateway", auth=auth)


def main():
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8090"))
    modes = [m for m, on in (("bearer", os.environ.get("MCP_BEARER_TOKEN")), ("oauth", provider)) if on] or ["OPEN"]
    app = gw.http_app(path="/mcp")
    register_convert_route(app)
    sys.stderr.write(f"markdownify-gateway http://{host}:{port}/mcp  auth={'+'.join(modes)}  → TS backend  (+/u/convert)\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
