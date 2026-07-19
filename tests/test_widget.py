"""Tests for the inline MCP-Apps upload widget (widget.py).

The in-iframe render is host-gated and can't be tested here, but the render
GATES (the claude.ai domain, the CSP connect domain, the tool meta pointers) and
the tool wiring are pure and fully testable."""
import asyncio
import hashlib
import json

import pytest
from fastmcp import FastMCP

import widget

S3_PUBLIC = "https://s3.hantekllc.com"
BASE = "https://convertx.hantekllc.com"


def _stub_mint(filename):
    return {
        "src_key": f"src/uuid/{filename}",
        "agent_upload": {"url": f"https://s3.hantekllc.com/bucket/src/uuid/{filename}?X-Amz-Signature=x"},
        "human_upload": {"url": "https://convertx.hantekllc.com/u/abc123"},
    }


def _build(monkeypatch, disabled=False):
    if disabled:
        monkeypatch.setenv("MCP_UPLOAD_WIDGET", "0")
    else:
        monkeypatch.delenv("MCP_UPLOAD_WIDGET", raising=False)
    mcp = FastMCP(name="t")
    widget.register_upload_widget(mcp, files=None, s3_public_endpoint=S3_PUBLIC,
                                  public_base_url=BASE, mint_upload=_stub_mint)
    return mcp


# --- render gates (pure) --------------------------------------------------- #
def test_widget_domain_is_deterministic_sha256_gate():
    expected = hashlib.sha256(f"{BASE}/mcp".encode()).hexdigest()[:32] + ".claudemcpcontent.com"
    assert widget._widget_domain(BASE) == expected
    assert widget._widget_domain(BASE + "/") == expected  # trailing slash normalized


def test_widget_html_has_picker_and_direct_put():
    html = widget._WIDGET_HTML
    assert 'type="file"' in html
    assert 'method:"PUT"' in html                       # direct PUT to the presigned URL
    assert "ui/notifications/initialized" in html       # claude.ai render-gate signal
    assert "window.openai" in html                      # ChatGPT path


# --- registration ---------------------------------------------------------- #
def test_registers_resource_and_tool(monkeypatch):
    mcp = _build(monkeypatch)
    tools = {t.name for t in asyncio.run(mcp._list_tools())}
    resources = {str(r.uri) for r in asyncio.run(mcp._list_resources())}
    assert "upload_file" in tools
    assert widget._WIDGET_URI in resources


def test_tool_meta_points_at_widget_resource(monkeypatch):
    mcp = _build(monkeypatch)
    tool = next(t for t in asyncio.run(mcp._list_tools()) if t.name == "upload_file")
    assert tool.meta["ui/resourceUri"] == widget._WIDGET_URI       # claude.ai reads this
    assert tool.meta["openai/outputTemplate"] == widget._WIDGET_URI  # ChatGPT reads this


def test_resource_carries_render_domain_and_s3_csp(monkeypatch):
    mcp = _build(monkeypatch)
    res = next(r for r in asyncio.run(mcp._list_resources()) if str(r.uri) == widget._WIDGET_URI)
    blob = json.dumps(res.meta)  # serialize whatever nesting FastMCP used
    assert widget._widget_domain(BASE) in blob          # claude.ai render gate present
    assert "s3.hantekllc.com" in blob                   # CSP connect domain = the bucket, not the server


def test_disabled_flag_is_noop(monkeypatch):
    mcp = _build(monkeypatch, disabled=True)
    tools = {t.name for t in asyncio.run(mcp._list_tools())}
    resources = {str(r.uri) for r in asyncio.run(mcp._list_resources())}
    assert "upload_file" not in tools
    assert widget._WIDGET_URI not in resources


# --- the tool result the widget consumes ----------------------------------- #
def test_upload_file_returns_presigned_target(monkeypatch):
    mcp = _build(monkeypatch)
    result = asyncio.run(mcp.call_tool("upload_file", {"filename": "resume.docx", "target": "pdf"}))
    data = result.structured_content
    assert data["src_key"] == "src/uuid/resume.docx"   # output keeps the stem → resume.pdf
    assert data["target"] == "pdf"
    assert "s3.hantekllc.com" in data["upload_url"] and "X-Amz-Signature" in data["upload_url"]
    assert data["upload_link"] == "https://convertx.hantekllc.com/u/abc123"  # link fallback


def test_resource_serves_widget_html(monkeypatch):
    mcp = _build(monkeypatch)
    contents = asyncio.run(mcp._read_resource_mcp(widget._WIDGET_URI)) \
        if hasattr(mcp, "_read_resource_mcp") else None
    # fall back to reading via the resource object
    res = next(r for r in asyncio.run(mcp._list_resources()) if str(r.uri) == widget._WIDGET_URI)
    body = asyncio.run(res.read())
    text = body if isinstance(body, str) else body.decode() if isinstance(body, (bytes, bytearray)) else str(body)
    assert 'type="file"' in text
