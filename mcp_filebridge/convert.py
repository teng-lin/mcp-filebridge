"""Convert-on-upload route + its pure helpers, kept out of gateway.py so they're importable and
testable without the proxy/OAuth machinery. The ticket + md_key logic MUST stay byte-compatible
with the JS twin in _convert.mjs (JS mints/derives, Python verifies/derives)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Max-Age": "600",
}


def sign_key() -> bytes:
    """HMAC key, read at call time so tests (and env changes) take effect. Shared with the TS backend."""
    return (os.environ.get("MCP_UPLOAD_SIGNING_KEY") or os.environ.get("MCP_BEARER_TOKEN") or "dev-key").encode()


def convert_max_bytes() -> int:
    return int(os.environ.get("CONVERT_MAX_BYTES", str(50 * 1024 * 1024)))


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint_ticket(src_key: str, *, now_sec: int | None = None, ttl: int = 300) -> str:
    """Mirror of _convert.mjs mintTicket — used by tests (the TS backend mints in production)."""
    now = int(time.time()) if now_sec is None else now_sec
    payload = _b64url(json.dumps({"exp": now + ttl, "k": src_key}, separators=(",", ":")).encode())
    sig = _b64url(hmac.new(sign_key(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def ticket_payload(token: str, *, now_sec: int | None = None):
    """Return the verified, unexpired ticket payload dict, or None."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    expected = _b64url(hmac.new(sign_key(), payload_b64.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig_b64):
        return None
    try:
        data = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    now = time.time() if now_sec is None else now_sec
    return data if float(data.get("exp", 0)) >= now else None


def md_key_for(src_key: str) -> str:
    """Deterministic .md key from src_key — MUST match _convert.mjs mdKeyFor so to_markdown reuses it."""
    parts = (src_key or "").split("/")
    if len(parts) >= 3 and parts[1]:
        stem = os.path.splitext(parts[2])[0] or "document"
        return f"md/{parts[1]}/{stem}.md"
    stem = os.path.splitext(parts[-1])[0] or "document" if parts else "document"
    return f"md/_/{stem}.md"


def store_markdown(md: str, md_key: str) -> str | None:
    """PUT the .md to S3 at md_key (internal endpoint), return a presigned GET URL against the PUBLIC
    endpoint (browser-reachable) with an attachment disposition. None if S3 is unreachable."""
    try:
        from .s3_filebridge import make_client  # lazy: keeps this module importable without boto3

        key, secret = os.environ.get("S3_ACCESS_KEY"), os.environ.get("S3_SECRET_KEY")
        bucket = os.environ.get("S3_BUCKET", "markdownify")
        internal = make_client(os.environ.get("S3_ENDPOINT", "http://minio:9000"), key, secret)
        public = make_client(os.environ.get("S3_PUBLIC_ENDPOINT") or os.environ.get("S3_ENDPOINT"), key, secret)
        disp = f'attachment; filename="{os.path.basename(md_key)}"'
        internal.put_object(Bucket=bucket, Key=md_key, Body=md.encode("utf-8"), ContentType="text/markdown; charset=utf-8")
        return public.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": md_key, "ResponseContentDisposition": disp},
            ExpiresIn=int(os.environ.get("MD_DOWNLOAD_TTL", "86400")),
        )
    except Exception as exc:  # noqa: BLE001
        import sys
        sys.stderr.write(f"markdown S3 store failed (Copy-only fallback): {exc}\n")
        return None


def markitdown_convert(body: bytes, filename: str) -> str:
    """Bytes → markdown via markitdown, using the filename's extension for converter selection."""
    _, ext = os.path.splitext(filename)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "src" + (ext or ".bin"))
        with open(path, "wb") as fh:
            fh.write(body)
        from markitdown import MarkItDown  # lazy

        return MarkItDown().convert(path).text_content


async def convert_route(request: Request) -> Response:
    """POST bytes → convert on receipt → store at the src_key-derived md_key → return {markdown, download_url}."""
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=CORS)
    ticket = ticket_payload(request.path_params["token"])
    if ticket is None:
        return PlainTextResponse("invalid or expired upload ticket", status_code=403, headers=CORS)
    cap = convert_max_bytes()
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            return PlainTextResponse("file too large", status_code=413, headers=CORS)
    if not body:
        return PlainTextResponse("empty body", status_code=400, headers=CORS)
    filename = os.path.basename(request.query_params.get("filename", "upload.bin")) or "upload.bin"
    try:
        md = markitdown_convert(body, filename)
    except Exception as exc:  # noqa: BLE001 — surface the reason to the widget
        return PlainTextResponse(f"conversion failed: {exc}", status_code=500, headers=CORS)
    md_key = md_key_for(ticket.get("k", ""))
    payload = {"markdown": md, "filename": filename}
    download_url = store_markdown(md, md_key)
    if download_url:
        payload["download_url"] = download_url
    return JSONResponse(payload, headers=CORS)


def register_convert_route(app) -> None:
    """Insert the convert route at the front of a Starlette app's router."""
    app.router.routes.insert(0, Route("/u/convert/{token}", convert_route, methods=["POST", "OPTIONS"]))
