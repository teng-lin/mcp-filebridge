# mcp-filebridge

A **cross-language (Python + TypeScript) file side-channel for remote MCP servers**, backed by S3-compatible presigned URLs — plus two deployable examples and a reusable pattern for putting a **non-Python MCP server on claude.ai/ChatGPT without writing OAuth in that language**.

Remote MCP hosts (claude.ai, ChatGPT, Copilot) can't move binaries through the JSON-RPC tool channel — results are size-capped and base64 inlining breaks at a few kilobytes. The fix is out-of-band: the server hands back a short-lived **signed URL**, the bytes move over HTTPS, and only a lightweight reference rides the protocol.

This repo keeps the two genuinely valuable pieces of [`mcp_filebridge`](https://github.com/teng-lin/mcp-filebridge) — the **offer contract** and the **human upload widget** — and replaces its Python-locked HMAC/route engine with **commodity S3 presigning** (`boto3` / `@aws-sdk`). That swap is what makes it work identically in both languages with no shared binary and no FFI.

## What's here

```
python/s3_filebridge.py   # Python helper: offer_upload / offer_download / await_upload / routes / set_bucket_cors
ts/s3_filebridge.mjs      # TypeScript twin (@aws-sdk) — same offer JSON + widget, with the presignS3 seam
verify_parity.py          # golden-vector test: proves Python & TS emit byte-identical offers
offer.golden.json         # the normalized offer contract (checked in)
examples/convertx/        # deployable example — ConvertX (1000+ formats) behind a Python MCP server
examples/markdownify/     # deployable example — markitdown behind a TypeScript backend + Python OAuth gateway
tests/                    # pytest (unit + parity + integration), plus JS unit tests in the markdownify example
```

## Two things this repo demonstrates

**1. One file side-channel, two languages.** `s3_filebridge` presigns an upload/download URL so bytes move over HTTPS while only a reference rides JSON-RPC. Python and TypeScript emit **byte-identical** offers (locked by `verify_parity.py` + a golden vector), so a widget written once renders on either backend. Both sides carry a `presignS3` seam: internal ops use the in-network endpoint, presigned URLs use the public one.

**2. An OAuth-terminating gateway for any-language backends.** claude.ai's connector UI needs OAuth, but re-implementing DCR/CIMD/PKCE per language is a large lift. The `markdownify` example puts a **Python gateway** (FastMCP `create_proxy` + a self-hosted OAuth provider) in front of a **TypeScript** MCP backend over stdio — so the backend does its domain work and speaks zero OAuth. This is a recognized production pattern (Kong/Obot/TrueFoundry); `tools/call` **and** the MCP-Apps widget `_meta` were both proven to survive the proxy hop, locally and over a live Cloudflare tunnel.

## The offer contract

`offer_upload()` returns an `upload_required` payload with **both** actor paths:

- `agent_upload` — a raw `PUT` to a presigned URL (the code-execution-sandbox / curl case).
- `human_upload` — a mobile-safe `/u/<shortid>` link to a file-picker widget page that `fetch`-PUTs to the presigned URL (the browser case).
- `agent_instructions` — "try the agent path, else surface the human link."

`offer_download()` returns `download_ready` with a presigned `GET`. `await_upload()` blocks until the object lands (`head_object` poll).

## The two examples

| | `examples/convertx` | `examples/markdownify` |
|---|---|---|
| Backend language | **Python** (FastMCP directly) | **TypeScript** (`@modelcontextprotocol/sdk`, stdio) |
| Auth | self-hosted OAuth in-process | Python **gateway** proxies to the TS backend |
| Wraps | [ConvertX](https://github.com/C4illin/ConvertX) — 1000+ format conversions | [markitdown](https://github.com/microsoft/markitdown) — PDF/PPTX/XLSX/DOCX/image/audio → Markdown |
| Upload | inline widget → **direct PUT to S3** | inline widget → **POST to the server, which converts on receipt** |
| Output | `download_ready` presigned link | **context-safe**: `to_markdown` returns a preview + a clickable download link + a paging handle |
| Deploy | Compose (MCP + ConvertX + MinIO) + tunnel | self-contained Compose (own MinIO + gateway + dedicated tunnel) |

Both are live-validated behind Cloudflare tunnels and connectable from claude.ai. See each example's README.

## How claude.ai actually moves the bytes

- **Agent path:** the tool returns the presigned URL + a curl command; the model runs it in claude.ai's code sandbox. Requires the S3 domain in **Settings → Capabilities → Code execution → Additional allowed domains**.
- **Human/widget path:** the user picks the file in the inline widget, which uploads it (direct to S3 in ConvertX; to the gateway's convert route in markdownify). Requires bucket CORS.
- **Downloads on claude.ai:** the widget iframe is sandboxed (no in-widget downloads), so results are surfaced as **clickable links in the chat**, which are not sandboxed.

## Gotchas this repo already handles

1. **`@aws-sdk` signs a default CRC32 checksum** into presigned URLs, which can break a plain `PUT` on real AWS S3. The TS twin sets `requestChecksumCalculation: "WHEN_REQUIRED"`.
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
# Python helper self-check
pip install boto3 requests starlette && python3 python/s3_filebridge.py
# cross-language parity (needs Node + MinIO)
cd ts && npm install && cd .. && python3 verify_parity.py

# test suite
pip install -r tests/requirements-dev.txt -r examples/convertx/requirements.txt
pytest                 # unit + in-process OAuth + cross-language parity (no containers)
pytest -m integration  # + live MinIO round-trip; markdownify e2e if the stack is on :8090
cd examples/markdownify && npm test    # JS unit tests (node's built-in runner)
```

Coverage spans the offer/widget/short-link logic, `await_upload`, the OAuth pieces (config validation, SSRF-guarded CIMD, bearer, static-client/DCR wiring, full authorize→login→token dances in-process), the markdownify convert route + HMAC tickets, and **cross-language parity** of the ticket + `md_key` derivation (Python↔TypeScript, shelling out to node).

## Status

Validated spikes deployed live, not a packaged release. The offer JSON + widget page are the language-neutral spec; `offer.golden.json` is the conformance anchor. If this graduates: publish as a `pip` + `npm` package pair sharing the golden vector, and add a SEP-2631 adapter.
