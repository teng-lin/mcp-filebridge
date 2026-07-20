"""OAuth-terminating gateway that PROXIES to the markdownify TS backend (stdio).

The gateway (Python) owns OAuth (reusing examples/convertx/oauth.py) and forwards
/mcp to the TS server — so a TypeScript MCP server works on claude.ai/ChatGPT
WITHOUT re-implementing OAuth in TS. Validated pattern (see the gw-spike):
tools/call flows client → gateway(auth) → proxy → backend → back.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

import uvicorn
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples" / "convertx"))
import oauth  # noqa: E402  (build_auth / get_oauth_config / build_oauth_provider)

HERE = Path(__file__).resolve().parent
BACKEND = str(HERE / "server.mjs")

# Spawn the TS backend over stdio, passing the S3 + public-URL env through to it.
backend = ProxyClient({"mcpServers": {"markdownify": {
    "command": "node", "args": [BACKEND], "env": {**os.environ}}}})

oauth_cfg = oauth.get_oauth_config()  # None unless MCP_OAUTH_PASSWORD + BASE_URL set → bearer-only
provider = oauth.build_oauth_provider(oauth_cfg) if oauth_cfg else None
auth = oauth.build_auth(os.environ.get("MCP_BEARER_TOKEN") or None, provider)

gw = create_proxy(backend, name="markdownify-gateway", auth=auth)

# --- convert-on-upload route (mirrors notebooklm's /files/ul: the widget POSTs bytes here and
# the SERVER converts on receipt, so no widget-initiated tool call is needed — that's the bit
# claude.ai doesn't do). Auth is a short-lived HMAC ticket minted by the TS backend's upload_file,
# signed with the same key (shared via env), so this isn't an open conversion endpoint.
_SIGN_KEY = (os.environ.get("MCP_UPLOAD_SIGNING_KEY") or os.environ.get("MCP_BEARER_TOKEN") or "dev-key").encode()
_CONVERT_MAX_BYTES = int(os.environ.get("CONVERT_MAX_BYTES", str(50 * 1024 * 1024)))
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Max-Age": "600",
}


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _ticket_payload(token: str):
    """Return the verified, unexpired ticket payload dict, or None."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return None
    expected = base64.urlsafe_b64encode(
        hmac.new(_SIGN_KEY, payload_b64.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    if not hmac.compare_digest(expected, sig_b64):
        return None
    try:
        data = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    return data if float(data.get("exp", 0)) >= time.time() else None


def _md_key_for(src_key: str) -> str:
    """Deterministic .md key from src_key — MUST match server.mjs mdKeyFor so to_markdown reuses it."""
    parts = (src_key or "").split("/")
    if len(parts) >= 3 and parts[1]:
        stem = os.path.splitext(parts[2])[0] or "document"
        return f"md/{parts[1]}/{stem}.md"
    return f"out/{secrets.token_hex(8)}/document.md"


_S3_BUCKET = os.environ.get("S3_BUCKET", "markdownify")


def _store_markdown(md: str, md_key: str) -> str | None:
    """PUT the .md to S3 at md_key (internal endpoint) and return a presigned GET URL against the
    PUBLIC endpoint (browser-reachable) with an attachment disposition, so the link downloads and
    to_markdown(src_key) reuses the same object. None if S3 is unreachable → widget falls back to Copy."""
    try:
        import boto3  # lazy: keeps the gateway importable without boto3
        from botocore.config import Config

        cfg = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        creds = dict(aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
                     aws_secret_access_key=os.environ.get("S3_SECRET_KEY"), region_name="us-east-1", config=cfg)
        internal = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:9000"), **creds)
        public = boto3.client("s3", endpoint_url=os.environ.get("S3_PUBLIC_ENDPOINT") or os.environ.get("S3_ENDPOINT"), **creds)
        disp = f'attachment; filename="{os.path.basename(md_key)}"'
        internal.put_object(Bucket=_S3_BUCKET, Key=md_key, Body=md.encode("utf-8"), ContentType="text/markdown; charset=utf-8")
        return public.generate_presigned_url(
            "get_object",
            Params={"Bucket": _S3_BUCKET, "Key": md_key, "ResponseContentDisposition": disp},
            ExpiresIn=int(os.environ.get("MD_DOWNLOAD_TTL", "86400")),
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"markdown S3 store failed (Copy-only fallback): {exc}\n")
        return None


async def convert_route(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    ticket = _ticket_payload(request.path_params["token"])
    if ticket is None:
        return PlainTextResponse("invalid or expired upload ticket", status_code=403, headers=_CORS)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _CONVERT_MAX_BYTES:
            return PlainTextResponse("file too large", status_code=413, headers=_CORS)
    if not body:
        return PlainTextResponse("empty body", status_code=400, headers=_CORS)
    filename = os.path.basename(request.query_params.get("filename", "upload.bin")) or "upload.bin"
    _, ext = os.path.splitext(filename)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "src" + (ext or ".bin"))
        with open(path, "wb") as fh:
            fh.write(body)
        try:
            from markitdown import MarkItDown  # lazy: keeps the gateway importable without markitdown

            md = MarkItDown().convert(path).text_content
        except Exception as exc:  # noqa: BLE001 — surface the reason to the widget
            return PlainTextResponse(f"conversion failed: {exc}", status_code=500, headers=_CORS)
    # Store at the deterministic md_key derived from src_key (in the ticket) so a later
    # to_markdown(src_key) reuses this exact object instead of converting again.
    md_key = _md_key_for(ticket.get("k", ""))
    payload = {"markdown": md, "filename": filename}
    download_url = _store_markdown(md, md_key)
    if download_url:
        payload["download_url"] = download_url
    return JSONResponse(payload, headers=_CORS)


def main():
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8090"))
    modes = [m for m, on in (("bearer", os.environ.get("MCP_BEARER_TOKEN")), ("oauth", provider)) if on] or ["OPEN"]
    app = gw.http_app(path="/mcp")
    app.router.routes.insert(0, Route("/u/convert/{token}", convert_route, methods=["POST", "OPTIONS"]))
    sys.stderr.write(f"markdownify-gateway http://{host}:{port}/mcp  auth={'+'.join(modes)}  → TS backend  (+/u/convert)\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
