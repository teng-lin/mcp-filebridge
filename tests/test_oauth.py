"""Unit tests for oauth.py — config validation, SSRF guard, CIMD validation,
bearer verifier, auth composition, and static-client / DCR wiring."""
import asyncio

import pytest

import oauth

STRONG = "a-strong-password-16+"
BASE = "https://host.example"


# --- get_oauth_config ------------------------------------------------------ #
def _clear(monkeypatch):
    for k in ("MCP_OAUTH_PASSWORD", "MCP_OAUTH_BASE_URL", "MCP_OAUTH_CLIENT_ID",
              "MCP_OAUTH_CLIENT_SECRET", "MCP_OAUTH_REDIRECT_URIS", "MCP_OAUTH_STATE_PATH",
              "MCP_OAUTH_TRUST_PROXY"):
        monkeypatch.delenv(k, raising=False)


def test_config_none_when_unset(monkeypatch):
    _clear(monkeypatch)
    assert oauth.get_oauth_config() is None


def test_config_partial_fails(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", STRONG)  # base_url missing
    with pytest.raises(SystemExit):
        oauth.get_oauth_config()


def test_config_weak_password_fails(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "short")
    monkeypatch.setenv("MCP_OAUTH_BASE_URL", BASE)
    with pytest.raises(SystemExit):
        oauth.get_oauth_config()


def test_config_ok_and_client_defaults(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", STRONG)
    monkeypatch.setenv("MCP_OAUTH_BASE_URL", BASE)
    monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", "cid")
    cfg = oauth.get_oauth_config()
    assert cfg.client_id == "cid"
    # claude callback is allowlisted by default when a static client is set
    assert "https://claude.ai/api/mcp/auth_callback" in cfg.redirect_uris


@pytest.mark.parametrize("url", ["http://host", "https://host/path", "https://host?q=1", "ftp://host"])
def test_bare_origin_rejects(url):
    with pytest.raises(SystemExit):
        oauth._validate_bare_https_origin(url, "X")


@pytest.mark.parametrize("url", ["https://host", "https://host/"])
def test_bare_origin_accepts(url):
    oauth._validate_bare_https_origin(url, "X")  # no raise


# --- SSRF guard ------------------------------------------------------------ #
@pytest.mark.parametrize("url", [
    "https://127.0.0.1/c.json", "https://10.0.0.1/c.json",
    "https://169.254.169.254/c.json", "https://[::1]/c.json",
])
def test_ssrf_blocks_private(url):
    with pytest.raises(ValueError):
        oauth._ssrf_guard(url, allow_loopback=False)


def test_ssrf_allows_public_ip():
    oauth._ssrf_guard("https://1.1.1.1/c.json", allow_loopback=False)  # no raise


def test_ssrf_requires_https():
    with pytest.raises(ValueError):
        oauth._ssrf_guard("http://1.1.1.1/c.json", allow_loopback=False)


def test_ssrf_loopback_allowed_in_test_mode():
    oauth._ssrf_guard("http://127.0.0.1:9500/c.json", allow_loopback=True)  # no raise


# --- CIMD document validation (fetch mocked) ------------------------------- #
class _Resp:
    def __init__(self, body: bytes, status=200):
        self.status_code = status
        self.raw = self
        self._body = body

    def read(self, n, decode_content=True):
        return self._body[:n]


def _mock_get(monkeypatch, body: bytes, status=200):
    monkeypatch.setattr(oauth, "_ssrf_guard", lambda *a, **k: None)
    monkeypatch.setattr(oauth.requests, "get", lambda *a, **k: _Resp(body, status))


def test_cimd_happy(monkeypatch):
    import json
    doc = {"client_id": "https://c/x.json", "redirect_uris": ["https://c/cb"]}
    _mock_get(monkeypatch, json.dumps(doc).encode())
    meta = oauth._fetch_cimd("https://c/x.json", allow_loopback=False)
    assert meta["redirect_uris"] == ["https://c/cb"]


def test_cimd_id_must_match_url(monkeypatch):
    import json
    _mock_get(monkeypatch, json.dumps({"client_id": "https://evil/x", "redirect_uris": ["u"]}).encode())
    with pytest.raises(ValueError):
        oauth._fetch_cimd("https://c/x.json", allow_loopback=False)


def test_cimd_requires_redirect_uris(monkeypatch):
    import json
    _mock_get(monkeypatch, json.dumps({"client_id": "https://c/x.json"}).encode())
    with pytest.raises(ValueError):
        oauth._fetch_cimd("https://c/x.json", allow_loopback=False)


def test_cimd_non_200(monkeypatch):
    _mock_get(monkeypatch, b"nope", status=404)
    with pytest.raises(ValueError):
        oauth._fetch_cimd("https://c/x.json", allow_loopback=False)


# --- bearer verifier ------------------------------------------------------- #
def test_bearer_accepts_correct():
    p = oauth.BearerAuthProvider("secret-token")
    tok = asyncio.run(p.verify_token("secret-token"))
    assert tok is not None and tok.client_id == "convertx-mcp"


def test_bearer_rejects_wrong():
    p = oauth.BearerAuthProvider("secret-token")
    assert asyncio.run(p.verify_token("nope")) is None


# --- build_auth composition ------------------------------------------------ #
def test_build_auth_none():
    assert oauth.build_auth(None, None) is None


def test_build_auth_bearer_only():
    a = oauth.build_auth("tok", None)
    assert isinstance(a, oauth.BearerAuthProvider)


def test_build_auth_both_is_multiauth():
    from fastmcp.server.auth import MultiAuth
    prov = oauth.SelfHostedOAuthProvider(password=STRONG, base_url=BASE)
    assert isinstance(oauth.build_auth("tok", prov), MultiAuth)


# --- static client / DCR --------------------------------------------------- #
def test_static_client_registered_and_dcr_disabled():
    prov = oauth.SelfHostedOAuthProvider(
        password=STRONG, base_url=BASE, static_client_id="cid", static_client_secret="sec",
        redirect_uris=("https://claude.ai/api/mcp/auth_callback",))
    assert "cid" in prov.clients
    assert prov.client_registration_options.enabled is False
    assert asyncio.run(prov.get_client("cid")).client_id == "cid"


def test_open_dcr_when_no_static_client():
    prov = oauth.SelfHostedOAuthProvider(password=STRONG, base_url=BASE)
    assert prov.client_registration_options.enabled is True


def test_allow_dcr_keeps_registration_open_with_static_client():
    prov = oauth.SelfHostedOAuthProvider(
        password=STRONG, base_url=BASE, static_client_id="cid", allow_dcr=True)
    assert "cid" in prov.clients  # static client still pre-registered
    assert prov.client_registration_options.enabled is True  # DCR fallback on


def test_unknown_client_returns_none():
    prov = oauth.SelfHostedOAuthProvider(password=STRONG, base_url=BASE)
    assert asyncio.run(prov.get_client("does-not-exist")) is None
