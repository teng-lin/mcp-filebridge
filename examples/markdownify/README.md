# Example: markdownify behind an OAuth gateway (TypeScript backend)

Makes the [markdownify-mcp](https://github.com/zcaceres/markdownify-mcp) idea — "convert almost anything to Markdown" — work on **remote** hosts (claude.ai/ChatGPT), and does it as the repo's **TypeScript** proof.

Two components:

```
claude.ai ─JSON-RPC─▶ gateway.py (Python: OAuth + create_proxy)
                          │ stdio
                          ▼
                       server.mjs (TS MCP server: filebridge + markitdown)
                          └── HTTPS PUT/GET ─▶ S3 / R2 / MinIO
```

- **`server.mjs`** — a real TypeScript MCP server (stdio). Tools: `upload_file` (filebridge upload offer via `ts/s3_filebridge.mjs`) → `to_markdown` (stage the upload to a local path, run `markitdown`, return markdown inline or as a presigned `.md` when large). It does **no auth**.
- **`gateway.py`** — an **OAuth-terminating proxy** (FastMCP `create_proxy`) that spawns `server.mjs`, owns OAuth (reusing `examples/convertx/oauth.py`: DCR + CIMD + password `/login`), validates the token, and forwards `/mcp` to the backend.

## Why the gateway (and not OAuth-in-TS)
A TS MCP server needs OAuth to work on claude.ai's connector UI — but re-implementing DCR/CIMD/PKCE on the TS SDK is a large lift. Instead, a **language-agnostic Python gateway** terminates OAuth and proxies to *any* backend. The backend just does its domain work. This is a [recognized production pattern](https://developers.openai.com/apps-sdk/build/auth) (Kong/Obot/TrueFoundry) and was de-risked before building: `tools/call` flows client → gateway(auth) → proxy → backend → back, both locally **and over a live Cloudflare tunnel**.

Security notes (from the OAuth best-practices review): the token audience is the **gateway's** URI; the gateway **never forwards the client token** to the backend (confused-deputy); secure the gateway↔backend hop and keep the backend off the public internet. Here the backend is a **stdio child** of the gateway — not network-reachable at all — which satisfies that cleanly.

## Run it

```bash
cp .env.example .env      # set MCP_BEARER_TOKEN (+ OAuth for claude.ai)
docker compose up -d --build
# in-network smoke (docx → markdown through gateway → TS backend → markitdown):
docker cp report.docx markdownify-gateway-1:/tmp/report.docx
docker compose exec gateway python - < smoke.py   # see README history / the session for the snippet
```

This is a **self-contained** stack — its own MinIO, gateway, and a dedicated Cloudflare
tunnel on its own `markdownify_default` network. It shares nothing with the ConvertX stack
(ports 9101/8090 differ so both can run on one host).

## Connect from claude.ai / ChatGPT
The `cloudflared` service runs a dedicated tunnel (`APPS_CF_TUNNEL_TOKEN`, forced to
`http2`). Give that tunnel two Public Hostnames in the Cloudflare dashboard — both resolve
by docker-DNS because the connector is on this stack's network:

| Hostname | Service |
|---|---|
| `s3-markdownify.<domain>` | `http://minio:9000` (presigned upload/download target) |
| `markdownify.<domain>` | `http://gateway:8090` (the MCP endpoint) |

Set `S3_PUBLIC_ENDPOINT`/`PUBLIC_BASE_URL`/`MCP_OAUTH_BASE_URL` to those hostnames, add the
connector at `https://markdownify.<domain>/mcp`, and enter the password at `/login`. For a
managed bucket instead of the bundled MinIO, point `S3_ENDPOINT`/`S3_PUBLIC_ENDPOINT` at R2/S3.

## Status
- ✅ **Validated**: local (bearer gateway → TS backend → markitdown), in-container end-to-end, and the proxy hop over a live Cloudflare quick tunnel.
- ⏳ **Not yet done**: the final claude.ai *UI* connect through a stable tunnel hostname (needs a Cloudflare dashboard route), and the MCP-Apps widget on the TS backend (the widget HTML is language-neutral; wiring its `_meta` on the TS SDK is the remaining polish).
- Complements ConvertX: this adds **PDF/PPTX/XLSX/image-OCR/audio → markdown** (markitdown), which pandoc can't do.
