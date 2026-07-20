# Example: Docling behind an MCP server (with s3_filebridge)

Converts a user's uploaded document to Markdown with IBM [Docling](https://github.com/docling-project/docling) — real **layout analysis, table structure, reading order, and OCR** (a clear step up from pandoc/markitdown). Built on [docling-mcp](https://github.com/docling-project/docling-mcp)'s idea, this is the **thinnest** filebridge example because of one fact:

> Docling's `DocumentConverter.convert(source)` takes a **URL** and fetches it **server-side**.

So the filebridge **presigned URL *is* the source** — no broker route, no temp-file staging, no S3 SDK inside Docling.

```
claude.ai ─▶ docling-mcp  ──presigned GET (internal)──▶  Docling.convert(url) → Markdown
    │             │
    └── widget: user PUTs local file ─▶ MinIO / S3 / R2 ◀───────────────────────┘
```

## The gap it fills
Docling *accepts* URLs, but a claude.ai user's file is **on their laptop — at no URL**. filebridge's inline widget uploads the local file to S3, minting exactly the URL Docling consumes. And because Docling fetches **server-side**, the presigned GET only needs to be reachable by the MCP server (we presign it against the *internal* endpoint) — only the upload PUT has to be browser-reachable.

## Tools
- `upload_file(filename)` → renders the inline upload widget (from `mcp_filebridge.widget`); the user picks the file and it uploads straight to S3. The widget **auto-drives `convert`** after upload, so the result lands in the chat on its own.
- `convert(src_key)` → waits for the upload, presigns a GET of it, runs **Docling**, and returns a **context-safe preview + a clickable download link + `md_key`**. Reuses a prior conversion (keyed off `src_key`).
- `read_markdown(md_key, offset?, limit?)` → page a large Docling export without loading it all into context.

## Run it
```bash
cp .env.example .env      # set MCP_BEARER_TOKEN (+ OAuth for claude.ai)
docker compose up -d --build          # heavy image: Docling pulls torch; models download on first convert
```
The MCP endpoint is `http://localhost:9500/mcp`. For claude.ai, put it behind a tunnel (the `cloudflare` profile) and set `PUBLIC_BASE_URL`/`MCP_OAUTH_BASE_URL`; point the bucket at R2/S3 for real use. First conversion downloads Docling's models into the `docling-data` volume (slow once, then cached).

## Auth & deploy
Same as the other examples — `mcp_filebridge.oauth` composes Bearer (Claude Code) + self-hosted OAuth (claude.ai web). Pure Python, so no gateway-proxy hop: filebridge's `upload_file` + widget are added to the Docling server directly.

## Caveats
- **Weight:** Docling bundles torch + layout/OCR models — a multi-GB image and a slow first conversion. Not for a tiny host.
- **SSRF:** `convert` fetches a URL server-side. Here the URL is always our own just-minted presigned S3 URL (controlled), but if you expose Docling's raw `convert_document(source=<any url>)`, that's a server-side-request-forgery surface to guard.
- Set a bucket lifecycle rule to expire `src/` and `md/` objects; keep presign TTLs short.

## Tests
`python/tests/test_docling.py` (integration) proves the filebridge wiring end to end against live MinIO — upload → presign → `convert` (Docling stubbed) → context-safe preview → `read_markdown` paging → reuse — and asserts the presigned URL Docling receives is a **real, fetchable object**. Docling's own URL-fetch + parse is its tested behavior, so it's stubbed (no torch needed to run the test):

```bash
cd python && pytest -m integration tests/test_docling.py    # needs MinIO on :9100
```
