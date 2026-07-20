"""End-to-end integration against a running markdownify stack.

Marked `integration` and skipped unless the gateway is reachable (defaults to the docker stack's
published http://localhost:8090) and a bearer token is available (env MCP_BEARER_TOKEN, else read
from examples/markdownify/.env). Exercises the full progressive-disclosure flow across both
languages: upload_file (TS) → POST /u/convert (Python gateway) → to_markdown reuse (TS) → paging."""
import asyncio
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytest.importorskip("fastmcp")
import requests
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

pytestmark = pytest.mark.integration

BASE = os.environ.get("MARKDOWNIFY_MCP_URL", "http://localhost:8090").rstrip("/")
MDFY = Path(__file__).resolve().parents[1] / "examples" / "markdownify"


def _reachable(url: str) -> bool:
    u = urlparse(url)
    try:
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=1).close()
        return True
    except OSError:
        return False


def _bearer():
    if os.environ.get("MCP_BEARER_TOKEN"):
        return os.environ["MCP_BEARER_TOKEN"]
    env = MDFY / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("MCP_BEARER_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


@pytest.fixture
def bearer():
    if not _reachable(BASE):
        pytest.skip(f"{BASE} not reachable — start the markdownify stack")
    b = _bearer()
    if not b:
        pytest.skip("no MCP_BEARER_TOKEN (env or examples/markdownify/.env)")
    return b


def _client(bearer):
    return Client(StreamableHttpTransport(f"{BASE}/mcp", headers={"Authorization": f"Bearer {bearer}"}))


def test_end_to_end_progressive_disclosure(bearer):
    async def flow():
        async with _client(bearer) as c:
            names = {t.name for t in await c.list_tools()}
            assert {"upload_file", "to_markdown", "read_markdown"} <= names

            sc = (await c.call_tool("upload_file", {"filename": "report.docx"})).structured_content
            assert sc["src_key"] and sc["convert_url"]

            # Widget path: POST bytes to the LOCAL gateway (the HMAC ticket is host-independent).
            token = sc["convert_url"].split("/u/convert/", 1)[1]
            docx = (MDFY / "report.docx").read_bytes()
            r = requests.post(f"{BASE}/u/convert/{token}?filename=report.docx", data=docx,
                              headers={"Content-Type": "application/octet-stream"}, timeout=60)
            assert r.status_code == 200 and "download_url" in r.json()

            # Chat path REUSES the widget's conversion (same md_key) and is preview-first.
            st = (await c.call_tool("to_markdown", {"src_key": sc["src_key"]})).structured_content
            assert st["md_key"].startswith("md/") and st["bytes"] > 0

            # Paging returns exactly the requested slice.
            pg = (await c.call_tool("read_markdown", {"md_key": st["md_key"], "offset": 0, "limit": 10})).structured_content
            assert pg["returned"] == 10 and pg["total"] == st["bytes"]
            return st

    st = asyncio.run(flow())
    assert st["bytes"] > 0


def test_big_file_preview_is_capped(bearer):
    """A large document must not dump its full markdown into the tool result."""
    async def flow():
        async with _client(bearer) as c:
            sc = (await c.call_tool("upload_file", {"filename": "big.txt"})).structured_content
            token = sc["convert_url"].split("/u/convert/", 1)[1]
            big = ("# Big\n\n" + ("filler paragraph. " * 30 + "\n\n") * 120).encode()  # ~70KB
            requests.post(f"{BASE}/u/convert/{token}?filename=big.txt", data=big,
                          headers={"Content-Type": "text/plain"}, timeout=60)
            res = await c.call_tool("to_markdown", {"src_key": sc["src_key"]})
            st = res.structured_content
            assert st["bytes"] > 20_000 and st["truncated"] is True
            # what actually enters the model context stays small regardless of doc size
            assert len(res.content[0].text) < 8_000
            return st

    st = asyncio.run(flow())
    assert st["truncated"] is True


def test_bad_ticket_rejected():
    if not _reachable(BASE):
        pytest.skip(f"{BASE} not reachable")
    r = requests.post(f"{BASE}/u/convert/not.a.valid.ticket", data=b"x", timeout=10)
    assert r.status_code == 403


def test_cors_preflight():
    if not _reachable(BASE):
        pytest.skip(f"{BASE} not reachable")
    r = requests.options(f"{BASE}/u/convert/whatever", timeout=10)
    assert r.status_code == 204 and r.headers.get("access-control-allow-origin") == "*"
