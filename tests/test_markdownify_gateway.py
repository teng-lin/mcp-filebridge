"""Unit tests for the markdownify gateway's convert helpers (examples/markdownify/_convert.py).

Pure logic only — HMAC tickets, md_key derivation, and the convert route's control flow
(auth / size cap / CORS) with markitdown + S3 monkeypatched out."""
import json

import pytest

from mcp_filebridge import convert as cv

KEY = "unit-test-signing-key"


@pytest.fixture(autouse=True)
def _fixed_key(monkeypatch):
    monkeypatch.setenv("MCP_UPLOAD_SIGNING_KEY", KEY)
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)


# --- md_key derivation ----------------------------------------------------- #
def test_md_key_for_derives_expected():
    assert cv.md_key_for("src/abc-123/report.docx") == "md/abc-123/report.md"
    assert cv.md_key_for("src/u/a.b.pdf") == "md/u/a.b.md"      # only last ext stripped
    assert cv.md_key_for("src/u/noext") == "md/u/noext.md"


def test_md_key_for_deterministic_on_malformed():
    assert cv.md_key_for("garbage") == cv.md_key_for("garbage")
    assert cv.md_key_for("") == cv.md_key_for("")


# --- ticket HMAC ----------------------------------------------------------- #
def test_ticket_round_trip():
    t = cv.mint_ticket("src/u/f.pdf", now_sec=1000, ttl=300)
    p = cv.ticket_payload(t, now_sec=1100)
    assert p["k"] == "src/u/f.pdf" and p["exp"] == 1300


def test_ticket_rejects_expired():
    t = cv.mint_ticket("src/u/f.pdf", now_sec=1000, ttl=300)  # exp 1300
    assert cv.ticket_payload(t, now_sec=2000) is None


def test_ticket_rejects_tampered_sig():
    t = cv.mint_ticket("src/u/f.pdf", now_sec=1000)
    bad = t[:-2] + ("bb" if t.endswith("aa") else "aa")
    assert cv.ticket_payload(bad, now_sec=1000) is None


def test_ticket_rejects_forged_payload():
    t = cv.mint_ticket("src/u/f.pdf", now_sec=1000)
    sig = t.split(".", 1)[1]
    forged = cv._b64url(json.dumps({"exp": 9_999_999_999, "k": "src/evil/x"}).encode()) + "." + sig
    assert cv.ticket_payload(forged, now_sec=1000) is None


def test_ticket_rejects_wrong_key(monkeypatch):
    t = cv.mint_ticket("src/u/f.pdf", now_sec=1000)
    monkeypatch.setenv("MCP_UPLOAD_SIGNING_KEY", "a-different-key")
    assert cv.ticket_payload(t, now_sec=1000) is None


@pytest.mark.parametrize("bad", ["", "nodot", None, 123])
def test_ticket_rejects_malformed(bad):
    assert cv.ticket_payload(bad, now_sec=0) is None


# --- convert route (markitdown + S3 stubbed) ------------------------------- #
def _app():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    app = Starlette()
    cv.register_convert_route(app)
    return TestClient(app)


def test_convert_route_rejects_bad_ticket():
    r = _app().post("/u/convert/not.a.valid.ticket", content=b"x")
    assert r.status_code == 403


def test_convert_route_happy_path(monkeypatch):
    monkeypatch.setattr(cv, "markitdown_convert", lambda body, fn: "# md\n" + body.decode())
    monkeypatch.setattr(cv, "store_markdown", lambda md, key: "https://s3-markdownify.example/dl")
    t = cv.mint_ticket("src/u/hello.txt")
    r = _app().post(f"/u/convert/{t}?filename=hello.txt", content=b"hi")
    assert r.status_code == 200
    body = r.json()
    assert body["markdown"] == "# md\nhi"
    assert body["download_url"] == "https://s3-markdownify.example/dl"


def test_convert_route_413_on_oversize(monkeypatch):
    monkeypatch.setenv("CONVERT_MAX_BYTES", "10")
    monkeypatch.setattr(cv, "markitdown_convert", lambda body, fn: "x")
    t = cv.mint_ticket("src/u/big.txt")
    r = _app().post(f"/u/convert/{t}?filename=big.txt", content=b"0123456789ABCDEF")
    assert r.status_code == 413


def test_convert_route_400_on_empty(monkeypatch):
    monkeypatch.setattr(cv, "markitdown_convert", lambda body, fn: "x")
    t = cv.mint_ticket("src/u/empty.txt")
    r = _app().post(f"/u/convert/{t}?filename=empty.txt", content=b"")
    assert r.status_code == 400


def test_convert_route_cors_preflight():
    r = _app().options("/u/convert/whatever")
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]
