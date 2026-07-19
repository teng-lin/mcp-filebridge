"""Integration tests.

OAuth flows run fully in-process via Starlette's TestClient (no port, no
containers). The S3 + ConvertX round-trip needs the live compose stack and is
skipped when it isn't reachable.
"""
import base64
import hashlib
import json
import re
import secrets

import pytest
from starlette.testclient import TestClient

from conftest import CONVERTX_UP, MINIO_UP, STRONG_PW, BASE_URL, make_oauth_app

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"


def _pkce():
    v = secrets.token_urlsafe(48)
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def _run_dance(client, client_id, redirect_uri, *, client_secret=None):
    """authorize -> login -> token; returns the token JSON."""
    verifier, challenge = _pkce()
    r = client.get("/authorize", params={
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st"},
        follow_redirects=False)
    assert r.status_code == 302, r.text
    sid = re.search(r"sid=([^&]+)", r.headers["location"]).group(1)

    r = client.post("/login", data={"sid": sid, "password": STRONG_PW}, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith(redirect_uri)
    code = re.search(r"[?&]code=([^&]+)", loc).group(1)

    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier}
    if client_secret:
        form["client_secret"] = client_secret
    r = client.post("/token", data=form)
    assert r.status_code == 200, r.text
    return r.json()


def _mcp_initialize(client, access_token):
    return client.post("/mcp", headers={
        "Authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "c", "version": "1"}}})


def test_static_client_full_flow(monkeypatch):
    app, _ = make_oauth_app(monkeypatch, client_id="cid", client_secret="sec")
    with TestClient(app) as client:
        tok = _run_dance(client, "cid", CLAUDE_CB, client_secret="sec")
        assert tok.get("access_token") and tok.get("refresh_token")
        assert _mcp_initialize(client, tok["access_token"]).status_code == 200


def test_dcr_register_closed_with_static_client(monkeypatch):
    app, _ = make_oauth_app(monkeypatch, client_id="cid", client_secret="sec")
    with TestClient(app) as client:
        r = client.post("/register", json={"redirect_uris": [CLAUDE_CB]})
        assert r.status_code == 404


def test_mcp_requires_auth(monkeypatch):
    app, _ = make_oauth_app(monkeypatch, client_id="cid", client_secret="sec")
    with TestClient(app) as client:
        assert client.post("/mcp", json={}).status_code == 401


def test_metadata_advertises_cimd(monkeypatch):
    app, _ = make_oauth_app(monkeypatch, client_id="cid", client_secret="sec")
    with TestClient(app) as client:
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["client_id_metadata_document_supported"] is True
        assert meta.get("registration_endpoint") is None  # DCR closed


def test_wrong_password_rejected(monkeypatch):
    app, _ = make_oauth_app(monkeypatch, client_id="cid", client_secret="sec")
    with TestClient(app) as client:
        _, challenge = _pkce()
        r = client.get("/authorize", params={
            "client_id": "cid", "response_type": "code", "redirect_uri": CLAUDE_CB,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st"},
            follow_redirects=False)
        sid = re.search(r"sid=([^&]+)", r.headers["location"]).group(1)
        r = client.post("/login", data={"sid": sid, "password": "wrong"}, follow_redirects=False)
        assert r.status_code == 401  # form re-rendered with error, no redirect
        assert "location" not in r.headers


def test_cimd_full_flow(monkeypatch):
    # ChatGPT-style URL client_id; mock the CIMD fetch (validation covered in unit tests).
    import oauth
    cimd_url = "https://chatgpt.example/client.json"
    cb = "https://chatgpt.com/connector/oauth/testcb"
    monkeypatch.setattr(oauth, "_fetch_cimd",
                        lambda url, allow_loopback: {"client_id": url, "redirect_uris": [cb],
                                                     "client_name": "ChatGPT"})
    app, _ = make_oauth_app(monkeypatch, client_id="cid", client_secret="sec")
    with TestClient(app) as client:
        tok = _run_dance(client, cimd_url, cb)  # public client, no secret (PKCE)
        assert tok.get("access_token") and tok.get("refresh_token")
        assert _mcp_initialize(client, tok["access_token"]).status_code == 200


# ---------------------------------------------------------------------------- #
# S3 round-trip against live MinIO (published on :9100) — no ConvertX needed.
# ---------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.skipif(not MINIO_UP, reason="needs MinIO on :9100 (`make up` in examples/convertx)")
def test_s3_presigned_roundtrip_minio():
    import asyncio

    import boto3
    import requests
    from botocore.client import Config

    import s3_filebridge as fb

    s3 = boto3.client("s3", endpoint_url="http://localhost:9100",
                      aws_access_key_id="spikekey", aws_secret_access_key="spikesecret",
                      region_name="us-east-1",
                      config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    bucket = "pytest-s3fb"
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        s3.create_bucket(Bucket=bucket)
    h = fb.S3FileHelper(s3, bucket, "https://mcp.test")

    offer = h.offer_upload(filename="hello.txt")
    payload = b"round-trip through real MinIO"
    assert requests.put(offer["agent_upload"]["url"], data=payload).status_code == 200

    landed = asyncio.run(h.await_upload(offer["src_key"], timeout=10))
    assert landed["size_bytes"] == len(payload)

    dl = h.offer_download(key=offer["src_key"], filename="hello.txt", mime="text/plain")
    got = requests.get(dl["url"])
    assert got.status_code == 200 and got.content == payload


# ---------------------------------------------------------------------------- #
# Full S3 + ConvertX round-trip — needs ConvertX too (internal-only by default,
# so this runs inside the compose network, e.g. `make smoke`, not from the host).
# ---------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.skipif(not (MINIO_UP and CONVERTX_UP),
                    reason="needs MinIO (:9100) + ConvertX (:3300); ConvertX is internal-only — use `make smoke`")
def test_convertx_roundtrip(monkeypatch):
    import requests
    monkeypatch.setenv("S3_BUCKET", "pytest-convertx")
    import server  # module-level clients target localhost:9100 / :3300 by default

    offer = server.request_upload("sample.md")
    assert offer["status"] == "upload_required"
    put = requests.put(offer["agent_upload"]["url"], data=b"# Sample\n\nintegration test.\n")
    assert put.status_code == 200

    out = server.convert(offer["src_key"], "docx")
    assert out["status"] == "download_ready"
    got = requests.get(out["url"])
    assert got.status_code == 200
    assert got.content[:4] == b"PK\x03\x04"  # valid .docx (zip)
