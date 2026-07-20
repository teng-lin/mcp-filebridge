"""Integration test for the Docling example's filebridge wiring (examples/docling/server.py).

Docling's own URL-fetch + parse is its tested behavior; what WE own is: wait for the upload,
presign a GET, hand Docling that URL, then store/preview/page the result. This test proves that
end to end against the live MinIO — including that the presigned URL Docling would receive is a
REAL, fetchable object — with only the Docling parse stubbed (so torch/models aren't needed)."""
import importlib.util
import socket
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")
import requests

ROOT = Path(__file__).resolve().parents[2]
DOCLING_SERVER = ROOT / "examples" / "docling" / "server.py"


def _minio_up() -> bool:
    try:
        socket.create_connection(("localhost", 9100), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.integration


@pytest.fixture
def mod(monkeypatch):
    if not _minio_up():
        pytest.skip("MinIO not on :9100")
    monkeypatch.setenv("S3_ENDPOINT", "http://localhost:9100")
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT", "http://localhost:9100")
    monkeypatch.setenv("S3_ACCESS_KEY", "spikekey")
    monkeypatch.setenv("S3_SECRET_KEY", "spikesecret")
    monkeypatch.setenv("S3_BUCKET", "pytest-docling")
    spec = importlib.util.spec_from_file_location("docling_server", DOCLING_SERVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_convert_fetches_the_presigned_url_and_pages(mod, monkeypatch):
    seen = {}

    def fake_docling(url):
        seen["url"] = url
        body = requests.get(url, timeout=10)          # prove the presigned GET is a REAL fetchable object
        assert body.status_code == 200
        return "# Docling\n\n" + ("para. " * 40 + "\n\n") * 60   # ~15KB of "markdown"

    monkeypatch.setattr(mod, "_docling_markdown", fake_docling)

    # 1) upload a document to the presigned PUT (what the widget does)
    offer = mod._mint_upload("report.txt")
    src_key = offer["src_key"]
    requests.put(offer["agent_upload"]["url"], data=b"hello docling", timeout=10)

    # 2) convert → Docling gets a presigned GET of that object; result is context-safe
    res = mod.convert(src_key)
    assert seen["url"].split("/")[2] == "localhost:9100"          # server-side (internal) presign
    assert res["md_key"].startswith("md/") and res["bytes"] > 10_000
    assert res["truncated"] is True and "markdown_preview" in res
    assert len(res["markdown_preview"]) <= mod.PREVIEW_BYTES      # never the whole doc
    assert res["download_url"].split("/")[2] == "localhost:9100"

    # 3) paging reads the stored .md
    pg = mod.read_markdown(res["md_key"], offset=0, limit=100)
    assert pg["returned"] == 100 and pg["total"] == res["bytes"] and pg["next_offset"] == 100

    # 4) reuse — a second convert must NOT re-run Docling (reads the stored md_key)
    seen.clear()
    res2 = mod.convert(src_key)
    assert "url" not in seen and res2["md_key"] == res["md_key"]


def test_md_key_derivation(mod):
    assert mod._md_key_for("src/abc/report.pdf") == "md/abc/report.md"
    assert mod._md_key_for("src/u/a.b.docx") == "md/u/a.b.md"
