"""Self-hosted, password-gated OAuth 2.1 for the convertx MCP server.

claude.ai's connector UI speaks only OAuth (no bearer field), so to be reachable
from claude.ai the server must BE an OAuth authorization server. This runs a tiny
single-tenant AS gated by one password, composed with the bearer via MultiAuth
(Claude Code keeps the bearer; claude.ai uses OAuth — one endpoint, both clients).

Adapted almost verbatim from teng-lin/zlibrary-mcp's mcp/_oauth.py + _auth.py
(which credit notebooklm-py). The only changes: env-var names, and the three
tiny internal helpers (atomic write / bare-origin check / state path) inlined so
this file is self-contained. We do NOT hand-roll OAuth — InMemoryOAuthProvider
implements the full DCR/PKCE/token flow; we only add the password gate.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import anyio
from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from fastmcp.utilities.ui import create_secure_html_response
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
)
from mcp.server.auth.provider import AccessToken as MCPAccessToken
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

TOKEN_ENV = "MCP_BEARER_TOKEN"
OAUTH_PASSWORD_ENV = "MCP_OAUTH_PASSWORD"
OAUTH_BASE_URL_ENV = "MCP_OAUTH_BASE_URL"
TRUST_PROXY_ENV = "MCP_OAUTH_TRUST_PROXY"
STATE_PATH_ENV = "MCP_OAUTH_STATE_PATH"

MIN_PASSWORD_LEN = 16
MAX_CLIENTS = 100
MAX_PENDING = 20
PENDING_TTL_SECONDS = 300
MAX_LOGIN_ATTEMPTS = 3
THROTTLE_WINDOW_SECONDS = 60
THROTTLE_MAX_FAILURES = 5
MAX_THROTTLE_IPS = 2048
_CLIENT_ID = "convertx-mcp"


# --- inlined helpers (were zlibrary internals) ----------------------------- #
def _atomic_write_json(path: Path, data) -> None:
    encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(encoded)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


def _validate_bare_https_origin(url: str, env_name: str) -> None:
    p = urlparse(url)
    if p.scheme != "https":
        raise SystemExit(f"{env_name} must be an https:// URL (got {url!r}).")
    if not p.netloc:
        raise SystemExit(f"{env_name} must include a host (got {url!r}).")
    if p.path not in ("", "/") or p.query or p.fragment:
        raise SystemExit(f"{env_name} must be a BARE origin, no path (e.g. https://host). Got {url!r}.")


# --- bearer ---------------------------------------------------------------- #
class BearerAuthProvider(TokenVerifier):
    """Gate the HTTP transport on a single bearer token (constant-time compare)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.__digest = hashlib.sha256(token.encode("utf-8")).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            presented = token.encode("latin-1")
        except UnicodeEncodeError:
            return None
        if hmac.compare_digest(hashlib.sha256(presented).digest(), self.__digest):
            return AccessToken(token=_CLIENT_ID, client_id=_CLIENT_ID, scopes=[])
        return None


# --- oauth config ---------------------------------------------------------- #
class _Pending(NamedTuple):
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expiry: float
    attempts: int


@dataclass(frozen=True)
class OAuthConfig:
    password: str = field(repr=False)
    base_url: str
    state_path: Path | None = None
    trust_proxy: bool = False


def get_oauth_config() -> OAuthConfig | None:
    """Resolve OAuth config from env, or None when off. When EITHER of password /
    base_url is set, BOTH are required (fail closed); password must clear a
    strength bar; base_url must be a bare https origin."""
    password = os.environ.get(OAUTH_PASSWORD_ENV) or ""
    base_url = (os.environ.get(OAUTH_BASE_URL_ENV) or "").strip()
    if not password and not base_url:
        return None
    if not password or not base_url:
        missing = OAUTH_PASSWORD_ENV if not password else OAUTH_BASE_URL_ENV
        raise SystemExit(f"OAuth partially configured; set BOTH {OAUTH_PASSWORD_ENV} and "
                         f"{OAUTH_BASE_URL_ENV} (or unset both). Missing: {missing}.")
    if len(password) < MIN_PASSWORD_LEN:
        raise SystemExit(f"{OAUTH_PASSWORD_ENV} too weak: >= {MIN_PASSWORD_LEN} chars (random).")
    _validate_bare_https_origin(base_url, OAUTH_BASE_URL_ENV)
    sp = os.environ.get(STATE_PATH_ENV)
    return OAuthConfig(password=password, base_url=base_url,
                       state_path=Path(sp) if sp else None,
                       trust_proxy=os.environ.get(TRUST_PROXY_ENV) == "1")


def _client_ip(request: Request, *, trust_proxy: bool) -> str:
    if trust_proxy:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
    return request.client.host if request.client else "unknown"


class SelfHostedOAuthProvider(InMemoryOAuthProvider):
    """InMemoryOAuthProvider + a password-gated /authorize, DoS bounds, and
    file-backed persistence of clients + tokens."""

    def __init__(self, password, base_url, state_path=None, trust_proxy=False) -> None:
        super().__init__(base_url=base_url,
                         client_registration_options=ClientRegistrationOptions(enabled=True))
        self.__salt = secrets.token_bytes(16)
        self.__pw_digest = self._kdf(password)
        self._kdf_limiter = anyio.CapacityLimiter(4)
        self._state_path = state_path
        self._trust_proxy = trust_proxy
        self._pending: dict[str, _Pending] = {}
        self._fail_times: dict[str, list[float]] = {}
        self._load_state()

    def _kdf(self, password: str) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=self.__salt,
                              n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id not in self.clients and len(self.clients) >= MAX_CLIENTS:
            used = {t.client_id for t in self.access_tokens.values()}
            used |= {t.client_id for t in self.refresh_tokens.values()}
            evictable = next((cid for cid in self.clients if cid not in used), None)
            if evictable is None:
                raise RegistrationError(error="invalid_client_metadata",
                                        error_description="Client registration limit reached.")
            self.clients.pop(evictable, None)
        await super().register_client(client_info)
        self._save_state()

    async def authorize(self, client, params) -> str:
        self._prune_pending()
        while len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending, key=lambda s: self._pending[s].expiry)
            self._pending.pop(oldest, None)
        sid = secrets.token_urlsafe(32)
        self._pending[sid] = _Pending(client, params, time.time() + PENDING_TTL_SECONDS, 0)
        return f"{str(self.base_url).rstrip('/')}/login?sid={sid}"

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        routes.append(Route("/login", self._login, methods=["GET", "POST"]))
        return routes

    async def _login(self, request: Request) -> Response:
        if request.method == "GET":
            self._prune_pending()
            entry = self._pending.get(request.query_params.get("sid", ""))
            redirect = str(entry.params.redirect_uri) if entry else None
            return self._render_form(request.query_params.get("sid", ""), redirect_uri=redirect)

        ip = _client_ip(request, trust_proxy=self._trust_proxy)
        retry_after = self._throttled(ip)
        if retry_after is not None:
            return Response("Too many attempts. Try again later.", status_code=429,
                            headers={"Retry-After": str(retry_after)})

        form = await request.form()
        sid = str(form.get("sid", ""))
        password = str(form.get("password", ""))
        self._prune_pending()
        entry = self._pending.get(sid)
        if entry is None:
            return self._render_form("", error="Your login link expired. Restart from claude.ai.")
        client, params, expiry, attempts = entry

        presented = await anyio.to_thread.run_sync(self._kdf, password, limiter=self._kdf_limiter)
        if not hmac.compare_digest(presented, self.__pw_digest):
            self._record_failure(ip)
            attempts += 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                self._pending.pop(sid, None)
                return self._render_form("", error="Too many failed attempts. Restart from claude.ai.")
            self._pending[sid] = _Pending(client, params, expiry, attempts)
            return self._render_form(sid, redirect_uri=str(params.redirect_uri), error="Incorrect password.")

        self._pending.pop(sid, None)
        self._fail_times.pop(ip, None)
        redirect = await InMemoryOAuthProvider.authorize(self, client, params)
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    def _render_form(self, sid, *, redirect_uri=None, error="") -> HTMLResponse:
        safe_sid = html.escape(sid, quote=True)
        err = f'<p style="color:#c00">{html.escape(error)}</p>' if error else ""
        consent = (f"<p>Authorizing a client that returns to <b>{html.escape(redirect_uri)}</b>.</p>"
                   if redirect_uri else "")
        body = ("<h2>ConvertX connector</h2>"
                "<p>Enter the connector password to authorize this client.</p>"
                f"{consent}{err}"
                '<form method="post" action="login">'
                f'<input type="hidden" name="sid" value="{safe_sid}">'
                '<input type="password" name="password" autofocus required '
                'style="font-size:1.1em;padding:.4em;width:20em">'
                '<button type="submit" style="font-size:1.1em;padding:.4em 1em;margin-left:.5em">Sign in</button>'
                "</form>")
        resp = create_secure_html_response(body, status_code=401 if error else 200)
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'"
        return resp

    def _throttled(self, ip: str) -> int | None:
        now = time.time()
        times = [t for t in self._fail_times.get(ip, []) if now - t < THROTTLE_WINDOW_SECONDS]
        if times:
            self._fail_times[ip] = times
        else:
            self._fail_times.pop(ip, None)
        if len(times) >= THROTTLE_MAX_FAILURES:
            return int(THROTTLE_WINDOW_SECONDS - (now - min(times))) + 1
        return None

    def _record_failure(self, ip: str) -> None:
        if ip not in self._fail_times and len(self._fail_times) >= MAX_THROTTLE_IPS:
            self._fail_times.pop(next(iter(self._fail_times)), None)
        self._fail_times.setdefault(ip, []).append(time.time())

    def _prune_pending(self) -> None:
        now = time.time()
        for sid in [s for s, e in self._pending.items() if e.expiry < now]:
            self._pending.pop(sid, None)

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        token = await super().exchange_authorization_code(client, authorization_code)
        self._save_state()
        return token

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        token = await super().exchange_refresh_token(client, refresh_token, scopes)
        self._save_state()
        return token

    async def revoke_token(self, token) -> None:
        await super().revoke_token(token)
        self._save_state()

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        data = {
            "clients": {k: v.model_dump(mode="json") for k, v in self.clients.items()},
            "access_tokens": {k: v.model_dump(mode="json") for k, v in self.access_tokens.items()},
            "refresh_tokens": {k: v.model_dump(mode="json") for k, v in self.refresh_tokens.items()},
            "a2r": dict(self._access_to_refresh_map),
            "r2a": dict(self._refresh_to_access_map),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._state_path, data)
        except OSError as exc:
            logger.warning("Could not persist OAuth state to %s: %s", self._state_path, exc)

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            clients = {k: OAuthClientInformationFull.model_validate(v)
                       for k, v in data.get("clients", {}).items()}
            access_tokens = {k: MCPAccessToken.model_validate(v)
                             for k, v in data.get("access_tokens", {}).items()}
            refresh_tokens = {k: RefreshToken.model_validate(v)
                              for k, v in data.get("refresh_tokens", {}).items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Ignoring unreadable OAuth state %s (re-auth): %s", self._state_path, exc)
            return
        self.clients, self.access_tokens, self.refresh_tokens = clients, access_tokens, refresh_tokens
        self._access_to_refresh_map = dict(data.get("a2r", {}))
        self._refresh_to_access_map = dict(data.get("r2a", {}))


def build_auth(token: str | None, oauth: AuthProvider | None) -> AuthProvider | None:
    """bearer + oauth → MultiAuth; one of them → that one; neither → None."""
    bearer = BearerAuthProvider(token) if token else None
    if oauth and bearer:
        return MultiAuth(server=oauth, verifiers=[bearer])
    return oauth or bearer


def build_oauth_provider(config: OAuthConfig) -> AuthProvider:
    return SelfHostedOAuthProvider(password=config.password, base_url=config.base_url,
                                   state_path=config.state_path, trust_proxy=config.trust_proxy)
