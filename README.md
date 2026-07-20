# mcp-filebridge

A **cross-language (Python + TypeScript) file side-channel for remote MCP servers**, backed by S3-compatible presigned URLs — plus two deployable examples and a reusable pattern for putting a **non-Python MCP server on claude.ai/ChatGPT without writing OAuth in that language**.

Remote MCP hosts (claude.ai, ChatGPT, Copilot) can't move binaries through the JSON-RPC tool channel — results are size-capped and base64 inlining breaks at a few kilobytes. The fix is out-of-band: the server hands back a short-lived **signed URL**, the bytes move over HTTPS, and only a lightweight reference rides the protocol.

This repo keeps the two genuinely valuable pieces of [`mcp_filebridge`](https://github.com/teng-lin/mcp-filebridge) — the **offer contract** and the **human upload widget** — and replaces its Python-locked HMAC/route engine with **commodity S3 presigning** (`boto3` / `@aws-sdk`). That swap is what makes it work identically in both languages with no shared binary and no FFI.

## What's here

Three homes — two self-contained language libraries and the language-neutral contract they share:

```
python/                   # the Python library → pip
  mcp_filebridge/         #   s3_filebridge, oauth, widget, convert, gates (+ widget_bridge.js package-data)
  tests/                  #   pytest (unit + parity + integration)
  pyproject.toml
ts/                       # the JS library → npm
  s3_filebridge.mjs (@aws-sdk twin), convert.mjs, widget_gates.mjs (+ *.test.mjs)
  package.json
spec/                     # the language-neutral contract both libs must satisfy
  offer.golden.json       #   the offer-contract conformance vector (checked in)
  verify_parity.py        #   proves Python & TS emit byte-identical normalized offers
examples/convertx/        # deployable example — ConvertX (1000+ formats) behind a Python MCP server
examples/markdownify/     # deployable example — markitdown behind a TypeScript backend + Python OAuth gateway
examples/docling/         # deployable example — IBM Docling (layout/tables/OCR); the presigned URL IS the source
```

The shared MCP-Apps widget host-bridge is **package data** of `mcp_filebridge` (bundled in the wheel,
read via `importlib.resources`), so the Python package stays self-contained; the polyglot markdownify
example reads that same copy.

## Two things this repo demonstrates

**1. One file side-channel, two languages.** `s3_filebridge` presigns an upload/download URL so bytes move over HTTPS while only a reference rides JSON-RPC. Python and TypeScript emit **byte-identical _normalized_** offers (locked by `verify_parity.py` + a golden vector — volatile fields like signatures and UUIDs are masked before comparing), so a widget written once renders on either backend. Both sides carry a `presignS3` seam: internal ops use the in-network endpoint, presigned URLs use the public one.

**2. An OAuth-terminating gateway for any-language backends.** claude.ai's connector UI needs OAuth, but re-implementing DCR/CIMD/PKCE per language is a large lift. The `markdownify` example puts a **Python gateway** (FastMCP `create_proxy` + a self-hosted OAuth provider) in front of a **TypeScript** MCP backend over stdio — so the backend does its domain work and speaks zero OAuth. This is a recognized production pattern (Kong/Obot/TrueFoundry); `tools/call` **and** the MCP-Apps widget `_meta` were both proven to survive the proxy hop, locally and over a live Cloudflare tunnel.

## The offer contract

`offer_upload()` returns an `upload_required` payload with **both** actor paths:

- `agent_upload` — a raw `PUT` to a presigned URL (the code-execution-sandbox / curl case).
- `human_upload` — a mobile-safe `/u/<shortid>` link to a file-picker widget page that `fetch`-PUTs to the presigned URL (the browser case).
- `agent_instructions` — "try the agent path, else surface the human link."

`offer_download()` returns `download_ready` with a presigned `GET`. `await_upload()` blocks until the object lands (`head_object` poll).

A third example, **[examples/docling](examples/docling/README.md)**, wraps IBM Docling (layout/tables/OCR). It's the thinnest integration: Docling's `convert(source)` takes a **URL and fetches it server-side**, so the filebridge presigned URL *is* the source — no broker route, no staging.

## The two flagship examples

| | `examples/convertx` | `examples/markdownify` |
|---|---|---|
| Backend language | **Python** (FastMCP directly) | **TypeScript** (`@modelcontextprotocol/sdk`, stdio) |
| Auth | self-hosted OAuth in-process | Python **gateway** proxies to the TS backend |
| Wraps | [ConvertX](https://github.com/C4illin/ConvertX) — 1000+ format conversions | [markitdown](https://github.com/microsoft/markitdown) — PDF/PPTX/XLSX/DOCX/image/audio → Markdown |
| Upload | inline widget → **direct PUT to S3** | inline widget → **POST to the server, which converts on receipt** |
| Output | `download_ready` presigned link | **context-safe**: `to_markdown` returns a preview + a clickable download link + a paging handle |
| Deploy | Compose (MCP + ConvertX + MinIO) + tunnel | self-contained Compose (own MinIO + gateway + dedicated tunnel) |

Both are deployed behind Cloudflare tunnels and connectable from claude.ai — see **[examples/convertx](examples/convertx/README.md)** and **[examples/markdownify](examples/markdownify/README.md)**. (markdownify's widget convert-on-upload and chat-link download are verified end-to-end; ConvertX's inline-widget **render** is host-gated — the server wiring is verified, the in-iframe render is confirmable only inside claude.ai/ChatGPT.)

## How claude.ai actually moves the bytes

- **Agent path:** the tool returns the presigned URL + a curl command; the model runs it in claude.ai's code sandbox. Requires the S3 domain in **Settings → Capabilities → Code execution → Additional allowed domains**.
- **Human/widget path:** the user picks the file in the inline widget, which uploads it — **direct to S3 in ConvertX** (needs bucket CORS) or **to the gateway's convert route in markdownify** (route-level CORS, no bucket CORS needed).
- **Downloads on claude.ai:** the widget iframe is sandboxed (no in-widget downloads), so results are surfaced as **clickable links in the chat**, which are not sandboxed.

## Gotchas this repo already handles

1. **`@aws-sdk` signs a default CRC32 checksum** into presigned URLs, which can break a plain `PUT` on real AWS S3. The TS twin sets `requestChecksumCalculation: "WHEN_REQUIRED"` on the S3 client it constructs — a library consumer passing their own client must set it too.
2. **Per-bucket CORS differs by backend.** AWS S3 / Cloudflare R2 use `put_bucket_cors`; **MinIO doesn't implement it** and configures CORS server-side (`MINIO_API_CORS_ALLOW_ORIGIN`). `set_bucket_cors()` handles both.
3. **The `presignS3` seam.** Presigned URLs must name a **public** bucket host, not the internal container-network one — both the Python and TS helper take a separate presign client for that (regression-tested; the TS side once silently ignored it).

## Running the checks

Local S3 (MinIO) on `:9100`:

```bash
docker run -d --name minio -p 9100:9000 \
  -e MINIO_ROOT_USER=spikekey -e MINIO_ROOT_PASSWORD=spikesecret \
  quay.io/minio/minio:latest server /data
```

```bash
# Python library + helper self-check
pip install -e ./python && python3 -m mcp_filebridge.s3_filebridge
# cross-language parity (needs Node + MinIO)
cd ts && npm ci && cd .. && python3 spec/verify_parity.py   # npm ci = reproducible install from the lockfile

# test suite
pip install -r python/tests/requirements-dev.txt -r examples/convertx/requirements.txt
cd python && pytest              # unit + in-process OAuth + cross-language parity (no containers)
cd python && pytest -m integration   # + live MinIO round-trip; markdownify e2e if the stack is on :8090
cd ts && npm test                # JS unit tests (node's built-in runner)
```

Coverage spans the offer/widget/short-link logic, `await_upload`, the OAuth pieces (config validation, SSRF-guarded CIMD, bearer, static-client/DCR wiring, full authorize→login→token dances in-process), the markdownify convert route + HMAC tickets, and **cross-language parity** of the ticket + `md_key` derivation (Python↔TypeScript, shelling out to node).

## Status

Deployed live and tested end-to-end, but not yet a packaged release. The offer JSON + widget page are the language-neutral spec; `offer.golden.json` is the conformance anchor. If this graduates: publish as a `pip` + `npm` package pair sharing the golden vector, and add a SEP-2631 adapter.
