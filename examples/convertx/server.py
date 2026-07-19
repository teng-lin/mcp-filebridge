"""ConvertX MCP server — wraps the unmodified ConvertX container behind an MCP
server that uses s3_filebridge (S3 presigned URLs) as the file side-channel.

    python server.py           # serve streamable-HTTP (deployment entrypoint)
    python server.py --smoke   # round-trip self-check against a running stack

Config is env-driven (see .env.example). ConvertX stays untouched; only its
unofficial HTTP contract is isolated in the ConvertX class below.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass

import boto3
import requests
from botocore.client import Config

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "python"))
from s3_filebridge import S3FileHelper


# ---- config (env with local-compose defaults) ---------------------------- #
def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"missing required env: {name}")
    return v

S3_ENDPOINT = _env("S3_ENDPOINT", "http://localhost:9100")            # server -> bucket (internal)
S3_PUBLIC_ENDPOINT = _env("S3_PUBLIC_ENDPOINT", S3_ENDPOINT)          # client -> bucket (presign host)
S3_KEY = _env("S3_ACCESS_KEY", "spikekey")
S3_SECRET = _env("S3_SECRET_KEY", "spikesecret")
BUCKET = _env("S3_BUCKET", "convertx")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:9400")    # this server's public URL (for /u/ links)
CONVERTX_URL = _env("CONVERTX_URL", "http://localhost:3300")
CX_EMAIL = _env("CONVERTX_EMAIL", "spike2@test.local")
CX_PW = _env("CONVERTX_PASSWORD", "spikepass123")
MCP_TOKEN = _env("MCP_BEARER_TOKEN", "")
MCP_HOST = _env("MCP_HOST", "127.0.0.1")
MCP_PORT = int(_env("MCP_PORT", "9400"))
ALLOW_EXTERNAL_BIND = _env("MCP_ALLOW_EXTERNAL_BIND", "") == "1"


def _s3(endpoint):
    return boto3.client("s3", endpoint_url=endpoint,
                        aws_access_key_id=S3_KEY, aws_secret_access_key=S3_SECRET,
                        region_name="us-east-1",
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))

s3 = _s3(S3_ENDPOINT)                                                 # get/put/head
presign_s3 = _s3(S3_PUBLIC_ENDPOINT) if S3_PUBLIC_ENDPOINT != S3_ENDPOINT else s3
files = S3FileHelper(s3, BUCKET, PUBLIC_BASE_URL, presign_s3=presign_s3)


def _ensure_bucket():
    try:
        s3.head_bucket(Bucket=BUCKET)
    except Exception:
        s3.create_bucket(Bucket=BUCKET)
        try:
            files.set_bucket_cors()
        except Exception:
            pass


# ---- ConvertX: the validated unofficial contract ------------------------- #
@dataclass
class ConvertXResult:
    filename: str
    content: bytes


class ConvertX:
    def __init__(self, base, email, pw):
        self._b, self._email, self._pw = base, email, pw

    def _session(self):
        s = requests.Session()
        creds = {"email": self._email, "password": self._pw}
        r = s.post(f"{self._b}/login", data=creds, allow_redirects=False)
        if "auth=" not in r.headers.get("set-cookie", ""):
            # First run against a fresh container: no account yet. Create it
            # (requires ACCOUNT_REGISTRATION=true) then log in.
            s.post(f"{self._b}/register", data=creds, allow_redirects=False)
            r = s.post(f"{self._b}/login", data=creds, allow_redirects=False)
        auth = re.search(r"auth=([^;]+)", r.headers["set-cookie"]).group(1)
        uid = json.loads(base64.urlsafe_b64decode(auth.split(".")[1] + "==="))["id"]
        r = s.get(f"{self._b}/", headers={"Cookie": f"auth={auth}"}, allow_redirects=False)
        job = re.search(r"jobId=([^;]+)", r.headers["set-cookie"]).group(1)
        s.headers["Cookie"] = f"auth={auth}; jobId={job}"
        return s, uid, job

    def conversions(self, file_type):
        s, _, _ = self._session()
        html = s.post(f"{self._b}/conversions", data={"fileType": file_type}).text
        return sorted(set(re.findall(r'value="([a-z0-9_]+,[a-z0-9_]+)"', html)))

    def pick_tool(self, source_ext, target):
        """The converter tool ConvertX uses for source_ext → target (e.g. epub→mobi
        is calibre, md→pdf is a latex/weasyprint tool). Raises with the valid targets
        if the pair isn't supported — so the caller never sends an invalid combo."""
        pairs = self.conversions(source_ext)
        tools = [p.split(",", 1)[1] for p in pairs if p.split(",", 1)[0] == target]
        if not tools:
            targets = sorted({p.split(",", 1)[0] for p in pairs})
            raise RuntimeError(f"ConvertX can't convert .{source_ext} to .{target}. "
                               f"Supported targets: {', '.join(targets)}")
        # Prefer calibre for e-books, otherwise the first (usually only) option.
        return "calibre" if "calibre" in tools else tools[0]

    def convert(self, filename, data, target, tool):
        s, _, job = self._session()
        s.post(f"{self._b}/upload", files={"file": (filename, data)})
        s.post(f"{self._b}/convert", data={"convert_to": f"{target},{tool}",
               "file_names": json.dumps([filename])})
        stem = filename.rsplit(".", 1)[0]
        for _ in range(60):
            r = s.post(f"{self._b}/progress/{job}")
            m = re.search(rf'href="(/download/[^"]+\.{re.escape(target)})"', r.text)
            if m:
                return ConvertXResult(f"{stem}.{target}", s.get(f"{self._b}{m.group(1)}").content)
            time.sleep(0.5)
        raise RuntimeError(f"conversion to .{target} did not complete")


cx = ConvertX(CONVERTX_URL, CX_EMAIL, CX_PW)
_MIME = "application/octet-stream"


# ---- MCP tools ----------------------------------------------------------- #
def list_conversions(file_type: str) -> dict:
    """List the `format,tool` pairs ConvertX can convert `file_type` into."""
    return {"file_type": file_type, "targets": cx.conversions(file_type)}


def _mint_upload(filename: str) -> dict:
    """The upload offer for a source file — wired to the widget. The key carries the
    REAL filename so the converted output keeps the same stem (MyBook.epub → MyBook.mobi).
    Returns the full offer (agent PUT url + human /u/ link + src_key)."""
    _ensure_bucket()
    safe = os.path.basename((filename or "").strip()) or "file"
    return files.offer_upload(filename=safe)


# Block this long for the upload to land (like notebooklm-py's await_upload).
# Kept under typical host tool-call timeouts so the single convert() call returns.
_UPLOAD_WAIT_SECONDS = 55


def _wait_for_key(key: str, timeout: int = _UPLOAD_WAIT_SECONDS) -> bool:
    """Block until the object lands (the widget/agent upload may still be in flight).
    Returns True if it landed, False on timeout (caller decides how to report)."""
    for _ in range(timeout * 2):
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            return True
        except Exception:
            time.sleep(0.5)
    return False


_MIN_RESULT_BYTES = 64  # ConvertX error text ("Something went wrong") is ~21 bytes


def convert(src_key: str, target: str, tool: str = "auto", filename: str = "") -> dict:
    """Convert the file the user uploaded via `upload_file` (identified by `src_key`) to
    `target` (e.g. "pdf", "docx", "mobi"). The right converter is chosen automatically —
    do NOT guess a `tool`. `filename` is the real source filename (the widget passes it);
    it sets the output name so MyBook.epub → MyBook.mobi. Leave it empty and the name is
    taken from the upload key.

    Call this IMMEDIATELY after `upload_file` — do NOT wait for another user message. It
    BLOCKS until the user finishes uploading (up to ~55s) and then converts automatically,
    so the user only has to pick the file. If it reports the upload hasn't arrived yet,
    the user is still picking — just call convert again."""
    _ensure_bucket()
    if not _wait_for_key(src_key):  # blocks like await_upload; the upload may still be in flight
        raise RuntimeError(
            f"The upload for '{src_key.split('/')[-1]}' hasn't arrived yet — the user is likely "
            f"still choosing the file. Call convert again once they've uploaded.")
    data = s3.get_object(Bucket=BUCKET, Key=src_key)["Body"].read()
    # The widget knows the file the user actually picked; prefer that name (so the output
    # keeps the real stem) over the key's name, which was minted from the model's guess.
    name = os.path.basename(filename.strip()) or src_key.split("/")[-1]
    source_ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
    # Smart tool selection: ask ConvertX which converter handles source_ext → target.
    # Raises a helpful "supported targets: …" error for an unsupported pair.
    if not tool or tool == "auto":
        tool = cx.pick_tool(source_ext, target)
    result = cx.convert(name, data, target, tool)
    # A failed conversion still yields a downloadable stub (ConvertX's error text) —
    # don't pass that off as the result. Fail loudly with what went wrong.
    if len(result.content) < _MIN_RESULT_BYTES:
        raise RuntimeError(
            f"conversion .{source_ext} → .{target} (via {tool}) produced no real output "
            f"({len(result.content)} bytes: {result.content[:120]!r}). The source may be "
            f"invalid or the pair unsupported.")
    out_key = f"out/{uuid.uuid4()}/{result.filename}"
    s3.put_object(Bucket=BUCKET, Key=out_key, Body=result.content)
    return files.offer_download(key=out_key, filename=result.filename, mime=_MIME)


from fastmcp.server.middleware.middleware import Middleware


class _LogMiddleware(Middleware):
    """Log the MCP calls claude.ai/ChatGPT make, to diagnose widget rendering."""
    async def on_list_tools(self, context, call_next):
        sys.stderr.write("[mcp] list_tools\n"); sys.stderr.flush()
        return await call_next(context)

    async def on_list_resources(self, context, call_next):
        sys.stderr.write("[mcp] list_resources\n"); sys.stderr.flush()
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        sys.stderr.write(f"[mcp] call_tool {getattr(context.message, 'name', '?')}\n"); sys.stderr.flush()
        return await call_next(context)

    async def on_read_resource(self, context, call_next):
        sys.stderr.write(f"[mcp] read_resource {getattr(context.message, 'uri', '?')}\n"); sys.stderr.flush()
        return await call_next(context)


def build_server(auth=None):
    from fastmcp import FastMCP

    from widget import register_upload_widget
    mcp = FastMCP(name="convertx-filebridge", auth=auth)
    mcp.add_middleware(_LogMiddleware())
    # Step 1 (upload_file, from the widget module) → Step 2 (convert). list_conversions
    # is optional discovery. No separate "request_upload" tool — the link fallback is
    # folded into upload_file's result.
    mcp.tool(list_conversions)
    mcp.tool(convert)
    register_upload_widget(mcp, files, S3_PUBLIC_ENDPOINT, PUBLIC_BASE_URL, _mint_upload)
    return mcp


def serve():
    """Serve streamable-HTTP. Auth is handled by FastMCP: /mcp is gated by the
    bearer AND/OR self-hosted OAuth (claude.ai uses OAuth via MultiAuth). The
    OAuth routes (/authorize, /token, /register, metadata, /login) and the
    /u/<id> upload widget are public by design."""
    import uvicorn
    from oauth import build_auth, build_oauth_provider, get_oauth_config

    oauth_cfg = get_oauth_config()  # None unless MCP_OAUTH_PASSWORD + BASE_URL set
    loopback = MCP_HOST in ("127.0.0.1", "::1", "localhost")
    if not loopback and not MCP_TOKEN and not oauth_cfg and not ALLOW_EXTERNAL_BIND:
        sys.exit("refusing to bind non-loopback without auth: set MCP_BEARER_TOKEN "
                 "and/or MCP_OAUTH_PASSWORD (+ MCP_OAUTH_BASE_URL), or MCP_ALLOW_EXTERNAL_BIND=1.")

    oauth = build_oauth_provider(oauth_cfg) if oauth_cfg else None
    auth = build_auth(MCP_TOKEN or None, oauth)

    mcp = build_server(auth=auth)
    app = mcp.http_app(path="/mcp")
    app.router.routes.extend(files.routes())  # /u/<sid> widget (public)

    modes = [m for m, on in (("bearer", MCP_TOKEN), ("oauth", oauth)) if on] or ["OPEN"]
    sys.stderr.write(f"convertx-filebridge: http://{MCP_HOST}:{MCP_PORT}/mcp  auth={'+'.join(modes)}\n")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")


# ---- smoke self-check (against a running stack) -------------------------- #
def smoke():
    up = request_upload("spike.md")
    assert {"human_upload", "agent_upload"} <= up.keys(), up
    r = requests.put(up["agent_upload"]["url"], data=b"# Smoke\n\ndeployment check.\n")
    assert r.status_code == 200, r.status_code
    out = convert(up["src_key"], "docx")
    got = requests.get(out["url"])
    assert got.status_code == 200 and got.content[:4] == b"PK\x03\x04", "result not a valid .docx"
    print(f"ok: upload offer -> agent PUT -> convert -> download offer -> valid .docx "
          f"({len(got.content)} bytes)")


if __name__ == "__main__":
    smoke() if "--smoke" in sys.argv else serve()
