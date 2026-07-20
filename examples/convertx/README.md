# Example: ConvertX behind an MCP server (with s3_filebridge)

Wraps [ConvertX](https://github.com/C4illin/ConvertX) — a self-hosted converter for 1000+ formats — behind an MCP server, using `s3_filebridge` (S3 presigned URLs) so files move to/from a remote host like claude.ai. ConvertX runs **unmodified**; only its unofficial HTTP contract is isolated in one class.

```
claude.ai ──JSON-RPC (tools + presigned URLs)──▶ convertx-mcp ──HTTP──▶ ConvertX
    │                                                 │
    └────────── HTTPS PUT/GET bytes ───────▶ MinIO / S3 / R2 ◀── boto3 get/put/head ┘
```

## Tools
- `upload_file(filename, target?)` → renders an **inline MCP-Apps upload widget** (a file picker in claude.ai / ChatGPT); the user picks the source file and it uploads **straight to S3**. Returns the `src_key` to pass to `convert`. The signed-link fallback (for hosts without inline widgets) is folded into its result — there is no separate `request_upload` tool.
- `list_conversions(file_type)` → the `format,tool` pairs ConvertX offers for that type.
- `convert(src_key, target, tool="auto", filename="")` → waits for the upload to land, **auto-selects the converter** (pandoc / calibre / imagemagick / … via ConvertX's own tool list) unless you pin `tool`, runs the conversion, and returns a `download_ready` offer. Preserves the original filename stem; fails loud on a suspiciously tiny (<64-byte) result.

### Inline upload widget
`widget.py` (adapted from notebooklm-py) registers a `ui://` resource whose HTML renders in an MCP-Apps host's sandboxed iframe and PUTs the chosen file **directly to the S3 presigned URL**. The two host render-gates are handled: the claude.ai `sha256("<PUBLIC_BASE_URL>/mcp")[:32].claudemcpcontent.com` domain and the flat `_meta["ui/resourceUri"]`; the CSP `connect_domains` includes the **connector origin** (`PUBLIC_BASE_URL`) and the **bucket's public endpoint** (`S3_PUBLIC_ENDPOINT`), where the widget uploads. Set `MCP_UPLOAD_WIDGET=0` to disable and fall back to links. *(Inline rendering is experimental + host-gated; the server-side wiring is verified, but the actual in-iframe render can only be confirmed inside claude.ai/ChatGPT.)*

## Run it locally

```bash
make setup      # writes .env with generated secrets
make up         # build + start MinIO + ConvertX + the MCP server
make smoke      # end-to-end: upload offer → PUT → convert → download → valid .docx
make logs       # tail the server
make down       # stop
```

The MCP endpoint is `http://localhost:9400/mcp` (add `Authorization: Bearer <MCP_BEARER_TOKEN>` once set).

## How the pieces talk (two S3 endpoints)

Presigned URLs are consumed by the **client** (your browser / claude.ai's sandbox), so they must name a **public** bucket host — which usually isn't the internal one the server uses on the container network. The server therefore holds two S3 clients:

- `S3_ENDPOINT` (`http://minio:9000`) — internal `get`/`put`/`head`.
- `S3_PUBLIC_ENDPOINT` (`http://localhost:9100`) — the host baked into presigned URLs. `generate_presigned_url` only *signs* (no network), so the presign client never needs to reach it from inside the container.

## Connect from claude.ai

Two things must be publicly reachable: the **MCP endpoint** and the **bucket**.

1. **Bucket:** the simplest correct choice is **Cloudflare R2 or AWS S3** — one public endpoint, reachable by both the server and the client, no MinIO to expose. Set `S3_PUBLIC_ENDPOINT` (and `S3_ENDPOINT`) to it and drop the `minio` service. On S3/R2, `set_bucket_cors()` also configures browser CORS for the upload widget (MinIO does it via `MINIO_API_CORS_ALLOW_ORIGIN`).
2. **MCP endpoint:** put `convertx-mcp` behind a tunnel and set `PUBLIC_BASE_URL` to the tunnel hostname:
   ```bash
   # set CF_TUNNEL_TOKEN in .env, route the hostname → http://convertx-mcp:9400
   docker compose --profile cloudflare up -d --build
   ```
3. **Upload from the sandbox:** whitelist the bucket's domain under claude.ai → Settings → Capabilities → Code execution → Additional allowed domains, so the agent's `PUT` to the presigned URL is allowed.
4. **Add the connector:** in claude.ai, add a custom connector pointing at `https://<host>/mcp`. Under **Advanced settings**, enter the **OAuth Client ID** (`MCP_OAUTH_CLIENT_ID`) and **Client Secret** (`MCP_OAUTH_CLIENT_SECRET`). claude.ai runs Authorization Code + PKCE and redirects you to `/login` — enter `MCP_OAUTH_PASSWORD` once to authorize.

## Auth model
Auth is handled by FastMCP and composed via `MultiAuth` — set either or both:
- **Bearer** (`MCP_BEARER_TOKEN`) — for Claude Code / Desktop (send `Authorization: Bearer …`).
- **Self-hosted OAuth** (`MCP_OAUTH_PASSWORD` + `MCP_OAUTH_BASE_URL`) — for the **claude.ai web** connector, whose UI is OAuth-only. A tiny single-tenant OAuth 2.1 server (`oauth.py`, adapted from zlibrary-mcp) runs Authorization Code + PKCE + token issue/refresh via `InMemoryOAuthProvider`, gated by one password at `/login` (scrypt-hashed, constant-time, per-IP throttled). State persists to `MCP_OAUTH_STATE_PATH` (a full-account secret — the `oauth-state` volume).
- **Client registration:** setting `MCP_OAUTH_CLIENT_ID` **disables open Dynamic Client Registration by default** (its `/register` is an unauthenticated write surface) in favor of two modern paths — but `MCP_OAUTH_ALLOW_DCR=1` re-enables RFC 7591 registration as a fallback (the shipped `docker-compose.yml` sets it, since ChatGPT still leans on DCR):
  - **Claude.ai → static Client ID.** Pre-registers one client (the claude.ai callback exact-match allowlisted); enter `MCP_OAUTH_CLIENT_ID`/`_SECRET` in Advanced settings.
  - **ChatGPT → CIMD** (Client ID Metadata Documents, SEP-991). A URL `client_id` is fetched, validated (`client_id` must equal the URL, `redirect_uri` must be allowlisted in the doc), cached, and used on the fly — the metadata advertises `client_id_metadata_document_supported: true`, which ChatGPT keys off. The fetch is **SSRF-hardened**: https-only, 3s timeout, 10 KB cap, no redirects, and blocked loopback/private/link-local/metadata IPs. CIMD clients are treated as public + PKCE (the `/login` password is the real gate, so a `private_key_jwt` assertion isn't verified).

  Caveats: **RFC 8707 audience-binding is not implemented** (tokens are opaque, single-tenant — `aud` isn't asserted), and the ChatGPT path is validated against a **simulated** CIMD client, not a live ChatGPT connector. `MCP_OAUTH_CIMD_ALLOW_LOOPBACK=1` relaxes the SSRF guard for local testing only — never set it in production.

Fail-closed: a non-loopback bind with **no** bearer, **no** OAuth, and no `MCP_ALLOW_EXTERNAL_BIND=1` refuses to start.

`/mcp` is gated; the OAuth routes (`/authorize`, `/token`, `/register`, metadata, `/login`) and the `/u/<shortid>` upload widget are **public** by design — the widget is protected by its unguessable id and the presigned URL's own signature, not the connector auth.

## Caveats
- **ConvertX's HTTP contract is unofficial** (login → `GET /` mints jobId → multipart `/upload` → `/convert` → poll `/progress` → `/download`; the `auth` cookie is `Secure`). **Pin the image** — it can change between releases.
- **ConvertX is AGPL-3.0.** Serving it over a network triggers §13 copyleft — you may owe users the complete corresponding source. Review before hosting for others.
- Set a bucket **lifecycle rule** to expire `src/` and `out/` objects; keep presign TTLs short.
