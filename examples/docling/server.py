"""Docling MCP + s3_filebridge — convert a user's uploaded document to Markdown with IBM
[Docling](https://github.com/docling-project/docling): real layout analysis, table structure,
reading order, OCR — a step up from pandoc/markitdown.

The tight fit: Docling's ``DocumentConverter.convert(source)`` takes a **URL**, and fetches it
server-side. So the filebridge presigned URL IS the source — the inline widget uploads the user's
local file to S3, and Docling converts that URL directly. No broker route, no temp-file staging.

    upload_file (widget) → user PUTs the file to S3 → convert(src_key) presigns a GET and hands
    it to Docling → Markdown (context-safe preview + download link + md_key for paging).

Pure Python, reuses the mcp_filebridge library (S3 helper + OAuth + the auto-driving widget).
"""
import os
import sys
import time
from typing import Any

import uvicorn
from fastmcp import FastMCP

sys.path.insert(0, os.path.join(  # repo-root/python, so mcp_filebridge imports without `pip install`
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "python"))
from mcp_filebridge.s3_filebridge import S3FileHelper, make_client


def _env(k: str, d: str = "") -> str:
    return os.environ.get(k, d)


S3_ENDPOINT = _env("S3_ENDPOINT", "http://localhost:9100")
S3_PUBLIC_ENDPOINT = _env("S3_PUBLIC_ENDPOINT", S3_ENDPOINT)
S3_KEY, S3_SECRET = _env("S3_ACCESS_KEY", "spikekey"), _env("S3_SECRET_KEY", "spikesecret")
BUCKET = _env("S3_BUCKET", "docling")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:9500")
MCP_HOST, MCP_PORT = _env("MCP_HOST", "127.0.0.1"), int(_env("MCP_PORT", "9500"))
MCP_TOKEN = _env("MCP_BEARER_TOKEN")
ALLOW_EXTERNAL_BIND = _env("MCP_ALLOW_EXTERNAL_BIND") == "1"
PREVIEW_BYTES = int(_env("MD_PREVIEW_BYTES", "6000"))   # markdown chars into chat by default (~1.5K tokens)
FULL_CAP = int(_env("MD_FULL_CAP", "60000"))            # full=True inlines only up to ~15K tokens

s3 = make_client(S3_ENDPOINT, S3_KEY, S3_SECRET)                       # server-side get/put/head + docling's GET
presign_s3 = make_client(S3_PUBLIC_ENDPOINT, S3_KEY, S3_SECRET) if S3_PUBLIC_ENDPOINT != S3_ENDPOINT else s3
files = S3FileHelper(s3, BUCKET, PUBLIC_BASE_URL, presign_s3=presign_s3)  # widget PUT + download presign (public)


def _ensure_bucket() -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
    except Exception:
        try:
            s3.create_bucket(Bucket=BUCKET)
        except Exception:
            pass


def _mint_upload(filename: str) -> dict:
    """Offer for a source file — wired to the widget. src_key carries the real filename."""
    _ensure_bucket()
    safe = os.path.basename((filename or "").strip()) or "file"
    return files.offer_upload(filename=safe)


def _wait_for_key(key: str, timeout: int = 55) -> bool:
    for _ in range(timeout * 2):
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _md_key_for(src_key: str) -> str:
    """Deterministic .md key so a repeat convert reuses the prior Docling run instead of redoing it."""
    parts = (src_key or "").split("/")
    if len(parts) >= 3 and parts[1]:
        stem = os.path.splitext(parts[2])[0] or "document"
        return f"md/{parts[1]}/{stem}.md"
    return "md/_/document.md"


_CONVERTER = None


def _docling_markdown(url: str) -> str:
    """Fetch + convert a URL with Docling → Markdown. The converter loads models on first use, so
    keep it a lazily-built singleton (isolated here so tests can monkeypatch it)."""
    global _CONVERTER
    if _CONVERTER is None:
        from docling.document_converter import DocumentConverter

        _CONVERTER = DocumentConverter()
    return _CONVERTER.convert(url).document.export_to_markdown()


def _package(md: str, md_key: str, full: bool = False) -> dict:
    """Context-safe result: a preview + a clickable download link + a paging handle — never the
    whole document (a big Docling export would flood the model's context every turn)."""
    data = md.encode("utf-8")
    nbytes = len(data)
    dl = files.offer_download(key=md_key, filename=os.path.basename(md_key), mime="text/markdown")
    link = f"[⬇ Download {os.path.basename(md_key)}]({dl['url']})"
    if full and nbytes <= FULL_CAP:
        return {"markdown": md, "md_key": md_key, "bytes": nbytes, "truncated": False, "download_url": dl["url"]}
    preview = md[:PREVIEW_BYTES]
    truncated = len(preview) < len(md)
    note = (f"{link}  ·  preview: first {len(preview)} of {nbytes} bytes; "
            f"read_markdown(md_key=\"{md_key}\", offset={len(preview)}) for more"
            if truncated else f"{link}  ·  complete ({nbytes} bytes)")
    return {"markdown_preview": preview, "md_key": md_key, "bytes": nbytes, "truncated": truncated,
            "download_url": dl["url"], "note": note}


def convert(src_key: str, target: str = "", filename: str = "", full: bool = False) -> dict[str, Any]:
    """Convert the file the user uploaded via `upload_file` (identified by `src_key`) to Markdown
    with IBM Docling (layout, tables, reading order, OCR). Waits for the upload to land, then hands
    Docling a presigned URL for it. Reuses a prior conversion. Returns a context-safe preview + a
    clickable download link + `md_key` (use read_markdown to page). `target`/`filename` are accepted
    for widget compatibility and ignored. Call this right after upload_file — the widget auto-drives it.
    """
    md_key = _md_key_for(src_key)
    try:
        md = s3.get_object(Bucket=BUCKET, Key=md_key)["Body"].read().decode("utf-8")  # already converted
    except Exception:
        if not _wait_for_key(src_key):
            raise RuntimeError(f"upload for '{os.path.basename(src_key)}' not received yet — "
                               "call convert again once the file is uploaded.")
        # Docling fetches the URL SERVER-SIDE, so presign against the internal endpoint (fast, in-network).
        url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": src_key}, ExpiresIn=900)
        md = _docling_markdown(url)
        s3.put_object(Bucket=BUCKET, Key=md_key, Body=md.encode("utf-8"), ContentType="text/markdown; charset=utf-8")
    return _package(md, md_key, full=full)


def read_markdown(md_key: str, offset: int = 0, limit: int = 8000) -> dict[str, Any]:
    """Read a byte slice [offset, offset+limit) of an already-converted document (md_key from
    convert), with next_offset — page large Docling exports without loading them all into context."""
    data = s3.get_object(Bucket=BUCKET, Key=md_key)["Body"].read()
    total = len(data)
    offset = max(0, int(offset))
    end = min(offset + max(1, int(limit)), total)
    if offset >= total:
        return {"text": "", "offset": offset, "returned": 0, "total": total, "next_offset": None}
    return {"text": data[offset:end].decode("utf-8", "replace"), "offset": offset,
            "returned": end - offset, "total": total, "next_offset": end if end < total else None}


def build_server(auth=None) -> FastMCP:
    from mcp_filebridge.widget import register_upload_widget

    mcp = FastMCP(name="docling-filebridge", auth=auth)
    mcp.tool(convert)          # the widget auto-drives "convert" after upload
    mcp.tool(read_markdown)
    register_upload_widget(mcp, files, S3_PUBLIC_ENDPOINT, PUBLIC_BASE_URL, _mint_upload)
    return mcp


def main() -> None:
    from mcp_filebridge.oauth import build_auth, build_oauth_provider, get_oauth_config

    oauth_cfg = get_oauth_config()
    loopback = MCP_HOST in ("127.0.0.1", "::1", "localhost")
    if not loopback and not MCP_TOKEN and not oauth_cfg and not ALLOW_EXTERNAL_BIND:
        sys.exit("refusing to bind non-loopback without auth: set MCP_BEARER_TOKEN and/or "
                 "MCP_OAUTH_PASSWORD (+ MCP_OAUTH_BASE_URL), or MCP_ALLOW_EXTERNAL_BIND=1.")
    oauth = build_oauth_provider(oauth_cfg) if oauth_cfg else None
    auth = build_auth(MCP_TOKEN or None, oauth)

    mcp = build_server(auth=auth)
    app = mcp.http_app(path="/mcp")
    app.router.routes.extend(files.routes())  # /u/<sid> upload widget (public)
    modes = [m for m, on in (("bearer", MCP_TOKEN), ("oauth", oauth)) if on] or ["OPEN"]
    sys.stderr.write(f"docling-filebridge: http://{MCP_HOST}:{MCP_PORT}/mcp  auth={'+'.join(modes)}\n")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    main()
