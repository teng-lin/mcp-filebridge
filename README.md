# mcp-s3-filebridge

A **cross-language (Python + TypeScript) file side-channel for remote MCP servers**, backed by S3-compatible presigned URLs.

Remote MCP hosts (claude.ai, ChatGPT, Copilot) can't move binaries through the JSON-RPC tool channel — results are size-capped and base64 inlining breaks at a few kilobytes. The fix is out-of-band: the server hands back a short-lived **signed URL**, the bytes move over HTTPS, and only a lightweight reference rides the protocol.

This is a small, deliberately un-clever take on that pattern. It keeps the two genuinely valuable pieces of [`mcp_filebridge`](https://github.com/teng-lin/mcp-filebridge) — the **offer contract** and the **human upload widget** — and replaces its Python-locked HMAC/route engine with **commodity S3 presigning** (`boto3` / `@aws-sdk`). That swap is what makes it work identically in both languages with no shared binary and no FFI.

## Why S3 instead of a bespoke library

| | This helper (S3 presigning) | `mcp_filebridge` |
|---|---|---|
| Signed URL points at | the bucket (S3 / R2 / MinIO) | filebridge's routes, in your process |
| Object store | required | none (self-contained) |
| Crypto | AWS SigV4 (battle-tested) | its own HMAC |
| Languages | **Python + TypeScript + any** | Python only |
| Best when | cross-language; already run object storage | Python-only; want zero storage dep |

If you only need Python and don't want a bucket, use `mcp_filebridge`. If you need more than one language, this is the lazier correct answer. Both are superseded by **MCP SEP-2631** (`files/authorizeUpload` / `authorizeDownload`) once it lands in the official SDKs — treat this as the bridge until then.

## What's here

```
s3_filebridge.py        # the Python helper: offer_upload / offer_download / await_upload / routes / set_bucket_cors
ts-twin/s3_filebridge.mjs  # the TypeScript twin (@aws-sdk) — same offer JSON + widget, verbatim
verify_parity.py        # golden-vector test: proves Python & TS emit byte-identical offers
offer.golden.json       # the normalized offer contract (checked in)
examples/convertx_mcp.py   # wraps the ConvertX converter behind an MCP server using the helper
```

## The offer contract

`offer_upload()` returns an `upload_required` payload with **both** actor paths:

- `agent_upload` — a raw `PUT` to a presigned URL (the code-execution-sandbox / curl case).
- `human_upload` — a mobile-safe `/u/<shortid>` link to a file-picker widget page that `fetch`-PUTs to the presigned URL (the browser case).
- `agent_instructions` — "try the agent path, else surface the human link."

`offer_download()` returns `download_ready` with a presigned `GET`. `await_upload()` blocks until the object lands (`head_object` poll).

## How claude.ai actually uploads

- **Agent path:** the tool returns the presigned URL + a curl command; the model runs it in claude.ai's code sandbox. Requires the S3 domain in **Settings → Capabilities → Code execution → Additional allowed domains**.
- **Human path:** the user opens the `/u/<shortid>` widget on the device with the file (works on mobile) and picks it. Requires bucket CORS (below).

## Two gotchas this repo already handles

1. **@aws-sdk signs a default CRC32 checksum** into presigned URLs, which can break a plain `PUT` on real AWS S3 (the client sends no matching checksum header). The TS twin sets `requestChecksumCalculation: "WHEN_REQUIRED"`. Without this, Python and TS presigned URLs diverge and the TS one can 400 on S3.
2. **Per-bucket CORS differs by backend.** AWS S3 / Cloudflare R2 use `put_bucket_cors`; **MinIO doesn't implement it** and configures CORS server-side (`MINIO_API_CORS_ALLOW_ORIGIN`, default `*`). `set_bucket_cors()` handles both.

## Running the checks

Bring up a local S3 (MinIO) on `:9100`:

```bash
docker run -d --name minio -p 9100:9000 \
  -e MINIO_ROOT_USER=spikekey -e MINIO_ROOT_PASSWORD=spikesecret \
  quay.io/minio/minio:latest server /data
```

Python helper self-check:

```bash
pip install boto3 requests starlette
python3 s3_filebridge.py
```

Cross-language parity (needs Node + MinIO):

```bash
cd ts-twin && npm install && cd ..
python3 verify_parity.py     # asserts Python and TS emit byte-identical offers
```

ConvertX example additionally needs a ConvertX container on `:3300`:

```bash
docker run -d --name convertx -p 3300:3000 -e JWT_SECRET=x -e ACCOUNT_REGISTRATION=true \
  ghcr.io/c4illin/convertx:latest
python3 examples/convertx_mcp.py
```

> ConvertX's HTTP contract is **unofficial** (login → `GET /` mints jobId → multipart `/upload` → `/convert` → poll `/progress` → `/download`) and its `auth` cookie is `Secure`; pin the image version. ConvertX is **AGPL-3.0** — offering it over a network triggers §13 copyleft. See `examples/convertx_mcp.py`.

## Status

Validated spikes, not a packaged release. The offer JSON + widget page are the language-neutral spec; `offer.golden.json` is the conformance anchor. Next steps if this graduates: publish as a `pip` + `npm` package pair sharing the golden vector as a conformance test, and add a SEP-2631 adapter.
