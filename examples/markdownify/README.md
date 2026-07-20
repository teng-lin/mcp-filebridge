# Example: markdownify behind an OAuth gateway (TypeScript backend)

Makes the [markdownify-mcp](https://github.com/zcaceres/markdownify-mcp) idea — "convert almost anything to Markdown" (via Microsoft [markitdown](https://github.com/microsoft/markitdown): **PDF/PPTX/XLSX/DOCX** out of the box; image-OCR/audio if you rebuild with the `markitdown[all]` extras) — work on **remote** hosts (claude.ai/ChatGPT), and does it as the repo's **TypeScript** proof: a TS MCP backend that speaks zero OAuth, fronted by a Python gateway.

```
claude.ai ─JSON-RPC─▶ gateway.py (Python: OAuth + create_proxy)  ──stdio──▶  server.mjs (TS MCP backend)
   │                        │  + POST /u/convert  (widget uploads land here, converted on receipt)     │
   └──────── widget POST / presigned PUT·GET ────────────────────▶  MinIO / S3 / R2  ◀── markitdown ────┘
```

- **`server.mjs`** — a real TypeScript MCP server (stdio, `@modelcontextprotocol/sdk`). Does **no auth**. Tools below.
- **`gateway.py`** — an **OAuth-terminating proxy** (FastMCP `create_proxy`) that spawns `server.mjs`, owns OAuth (reusing the shared `mcp_filebridge.oauth`: static client + CIMD + password `/login`), validates the token, forwards `/mcp` to the backend, **and** hosts the `POST /u/convert` route (from `mcp_filebridge.convert`).
- **`ts/convert.mjs` / `mcp_filebridge/convert.py`** — the shared, byte-compatible ticket + `md_key` logic (JS mints, Python verifies), in the libraries so any example can reuse them; kept pure so they're unit- and parity-testable.

## Tools

| Tool | What it does | Context cost |
|---|---|---|
| `upload_file(filename)` | shows the inline upload widget; returns `src_key` + a signed `convert_url` (+ a presigned S3 URL for the agent path) | tiny |
| `to_markdown(src_key, full?)` | converts (or **reuses the widget's conversion**), returns a **preview + a clickable download link + a paging handle (`md_key`)** — never the whole doc, so a big file can't flood the context. `full=true` inlines small files (capped) | flat (~1.5K tokens) regardless of file size |
| `read_markdown(md_key, offset?, limit?)` | S3 range-read a slice of an already-converted doc, with `next_offset` — page huge markdown without loading it all | only what you ask for |

## Two ways bytes get in, and how markdown gets out

- **Widget path (humans):** the inline widget POSTs the picked file to `POST /u/convert/<ticket>` on the gateway, which **converts on receipt** (mirrors notebooklm's `/files/ul` — no widget-initiated tool call, which claude.ai does not do) and shows the markdown inline with a **Copy Markdown** button. The convert ticket is a short-lived HMAC signed with a key shared between the TS backend (mints) and the gateway (verifies), so `/u/convert` is not an open endpoint.
- **Agent path (Claude Code / curl):** `upload_file` also returns a presigned S3 `PUT` (`upload_url`); the agent PUTs the bytes and calls `to_markdown(src_key)`.
- **Getting a file out on claude.ai:** the widget iframe is sandboxed (no in-widget downloads/navigation), so the download is surfaced as a **clickable markdown link in the chat** by `to_markdown` — chat links aren't sandboxed. In the widget, use **Copy Markdown** or copy the link and open it in a browser tab.

Both paths converge on one S3 object: the widget stores the `.md` at a deterministic `md_key` derived from `src_key`, so a later `to_markdown(src_key)` **reuses it instead of converting twice**.

## Why the gateway (and not OAuth-in-TS)
Re-implementing DCR/CIMD/PKCE on the TS SDK is a large lift. Instead a **language-agnostic Python gateway** terminates OAuth and proxies to *any* backend — a [recognized production pattern](https://developers.openai.com/apps-sdk/build/auth) (Kong/Obot/TrueFoundry). De-risked before building: `tools/call` **and** the MCP-Apps widget `_meta` both survive `create_proxy`, locally and over a live Cloudflare tunnel.

Security notes: the token audience is the **gateway's** URI; the gateway **never forwards the client token** to the backend (confused-deputy). The backend is a **stdio child** of the gateway — not network-reachable at all — which keeps it off the internet cleanly.

## Run it
Self-contained stack — its own MinIO, gateway, and a dedicated Cloudflare tunnel on its own `markdownify_default` network. Shares nothing with the ConvertX stack (ports 9101/8090 differ so both run on one host).

```bash
cp .env.example .env      # set MCP_BEARER_TOKEN, OAuth (for claude.ai), APPS_CF_TUNNEL_TOKEN
docker compose up -d --build
```

## Connect from claude.ai / ChatGPT
The `cloudflared` service runs a **dedicated** tunnel (`APPS_CF_TUNNEL_TOKEN`, forced to `http2` because QUIC/UDP 7844 egress is often blocked). Give that tunnel two Public Hostnames in the Cloudflare dashboard — both resolve by docker-DNS because the connector is on this stack's network, and the service **Type must be HTTP** (the origins are plain HTTP):

| Hostname | Service (Type: HTTP) |
|---|---|
| `s3-markdownify.<domain>` | `minio:9000` — presigned upload/download target |
| `markdownify.<domain>` | `gateway:8090` — the MCP endpoint |

Set `S3_PUBLIC_ENDPOINT`/`PUBLIC_BASE_URL`/`MCP_OAUTH_BASE_URL` to those hostnames, add the connector at `https://markdownify.<domain>/mcp`, and enter `MCP_OAUTH_PASSWORD` at `/login`. For a managed bucket instead of the bundled MinIO, point `S3_ENDPOINT`/`S3_PUBLIC_ENDPOINT` at R2/S3.

## Context safety (why `to_markdown` doesn't just return the markdown)
A tool result enters the model's context and is re-sent every turn. A 100-page PDF is ~75K tokens — dumping it would tax the whole conversation or exceed the window. So `to_markdown` returns a **preview (`MD_PREVIEW_BYTES`, default 6000 ≈ 1.5K tokens) + a download link + a paging handle**; the model pages with `read_markdown` only what it needs. `full=true` inlines only up to `MD_FULL_CAP` (60KB). The full content always lives in the widget + S3, so nothing is lost. (Verified: a 107KB doc puts ~1.7K tokens into context.)

## Tests
Pure logic (HMAC tickets, `md_key` derivation, convert-route control flow) is unit-tested in **both languages**, with a cross-language parity suite and a live end-to-end integration test:

```bash
cd ts && npm test                          # JS units (node --test) — ts/convert.mjs
# from repo root:
pip install -r python/tests/requirements-dev.txt
cd python && pytest tests/test_markdownify_gateway.py tests/test_markdownify_parity.py   # units + JS↔Py parity
cd python && pytest -m integration tests/test_markdownify_integration.py                 # live stack on :8090
```

The parity tests shell out to `node` to prove `ts/convert.mjs` and `mcp_filebridge/convert.py` stay byte-compatible (a drift silently breaks widget→chat reuse or convert auth). The integration test drives `upload_file → /u/convert → to_markdown reuse → read_markdown` paging and asserts a big file's preview stays capped.

## Status
- ✅ **Live**: deployed behind a dedicated Cloudflare tunnel, connected from claude.ai — widget convert-on-upload, Copy Markdown, chat-link download, and the context-safe `to_markdown`/`read_markdown` path all verified end-to-end over the tunnel.
- ⚠️ **Host limitation**: claude.ai's widget iframe blocks in-widget downloads/navigation (sandbox) — hence downloads are surfaced as chat links, not an in-widget button.
- Complements ConvertX: adds **PDF/PPTX/XLSX/DOCX → markdown** (markitdown; image-OCR/audio need a rebuild with `markitdown[all]`), which pandoc can't do.
