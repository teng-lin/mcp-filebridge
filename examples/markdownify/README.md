# Example (spike): markdownify + s3_filebridge (TypeScript)

Extends the [markdownify-mcp](https://github.com/zcaceres/markdownify-mcp) idea — "convert almost anything to Markdown" — with the two directions it lacks on a **remote** MCP host, using the **TypeScript** side of this repo (`ts/s3_filebridge.mjs`).

markdownify's `*-to-markdown` tools take a **local file path** and wrap `markitdown`. On claude.ai/ChatGPT the user's file isn't on the server's disk, so there's no way to reach it. This spike adds:

- **Upload** — the user's file is PUT to S3 via a presigned URL, then **staged to a local path** so `markitdown` can read it. (`offerUpload` → `waitForKey` → pull → `markitdown`.)
- **Download** — markdown is returned inline when small, but a huge input → huge markdown busts the tool-result cap, so above a threshold it's uploaded and returned as a **presigned `.md` URL**. (`offerDownload`.)

```
claude.ai ─JSON-RPC─▶ markdownify (+ ts/s3_filebridge.mjs) ─▶ markitdown (local path)
   └──── HTTPS PUT/GET bytes ────▶ S3 / R2 / MinIO
```

## Run the spike

Needs the local MinIO (`:9100`) and `markitdown`:

```bash
pip install "markitdown[all]"        # the converter markdownify wraps
npm install                          # @aws-sdk/*
MARKITDOWN_BIN=$(command -v markitdown) node spike.mjs
```

It generates/uses `report.docx`, offers an upload, PUTs it to S3, stages it, runs `markitdown`, and asserts the markdown (heading + bullets + table) — then forces the large-output branch and fetches the `.md` back via the presigned URL.

## Status / notes

- **Validated spike, not a full server.** It proves the core loop (upload → stage → markitdown → markdown, + large→download) against real MinIO + real markitdown. It does **not** yet wire the MCP transport, the MCP-Apps widget, or OAuth — those are the same pieces as `examples/convertx/` (the widget HTML is language-neutral; OAuth would need the TS SDK's auth, since `oauth.py` is Python).
- **This is the "TS twin works in a real server" proof** — it's the first thing to exercise `ts/s3_filebridge.mjs` beyond the parity test.
- Complements ConvertX: ConvertX does *document*→markdown (docx/epub/html via pandoc); markitdown adds **PDF / PPTX / XLSX / image-OCR / audio-transcription** → markdown, which ConvertX can't.
