"""Shared fixtures: sys.path wiring, container reachability, and an in-process
OAuth app builder for integration tests (no real port needed)."""
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "examples" / "convertx"))


def _reachable(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


MINIO_UP = _reachable("localhost", 9100)
CONVERTX_UP = _reachable("localhost", 3300)

STRONG_PW = "test-password-at-least-16-chars"
BASE_URL = "https://convertx.hantekllc.com"


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs live MinIO/ConvertX containers")


def make_oauth_app(monkeypatch, *, password=STRONG_PW, base_url=BASE_URL, bearer=None,
                   client_id=None, client_secret=None, allow_loopback=False):
    """Assemble a FastMCP app (one `ping` tool) wired with the real oauth.py
    provider, for driving the full OAuth flow via Starlette's TestClient."""
    import oauth
    from fastmcp import FastMCP

    env = {"MCP_OAUTH_PASSWORD": password, "MCP_OAUTH_BASE_URL": base_url}
    if client_id:
        env["MCP_OAUTH_CLIENT_ID"] = client_id
    if client_secret:
        env["MCP_OAUTH_CLIENT_SECRET"] = client_secret
    if allow_loopback:
        env["MCP_OAUTH_CIMD_ALLOW_LOOPBACK"] = "1"
    for k in ("MCP_OAUTH_PASSWORD", "MCP_OAUTH_BASE_URL", "MCP_OAUTH_CLIENT_ID",
              "MCP_OAUTH_CLIENT_SECRET", "MCP_OAUTH_CIMD_ALLOW_LOOPBACK", "MCP_OAUTH_STATE_PATH"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    provider = oauth.build_oauth_provider(oauth.get_oauth_config())
    auth = oauth.build_auth(bearer, provider)
    mcp = FastMCP(name="test", auth=auth)

    @mcp.tool
    def ping() -> str:
        return "pong"

    # FastMCP mounts the provider's OAuth routes (/authorize, /token, /login,
    # metadata) from auth= — no need to add them again.
    app = mcp.http_app(path="/mcp")
    return app, provider
