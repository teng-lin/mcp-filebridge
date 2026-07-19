# Example: ConvertX behind an MCP server (with s3_filebridge)

Wraps [ConvertX](https://github.com/C4illin/ConvertX) — a self-hosted converter for 1000+ formats — behind an MCP server, using `s3_filebridge` (S3 presigned URLs) so files move to/from a remote host like claude.ai. ConvertX runs **unmodified**; only its unofficial HTTP contract is isolated in one class.

```
claude.ai ──JSON-RPC (tools + presigned URLs)──▶ convertx-mcp ──HTTP──▶ ConvertX
    │                                                 │
    └────────── HTTPS PUT/GET bytes ───────▶ MinIO / S3 / R2 ◀── boto3 get/put/head ┘
```

## Tools
- `request_upload(filename)` → an `upload_required` offer (human widget + agent PUT paths).
- `list_conversions(file_type)` → the `format,tool` pairs ConvertX offers for that type.
- `convert(src_key, target, tool="pandoc")` → runs the conversion, returns a `download_ready` offer.

## Run it locally

```bash
make setup      # writes .env with generated secrets
make up         # build + start MinIO + ConvertX + the MCP server
make smoke      # end-to-end: upload offer → PUT → convert → download → valid .docx
make logs       # tail the server
make down       # stop
```

The MCP endpoint is `http://localhost:8080/mcp` (add `Authorization: Bearer <MCP_BEARER_TOKEN>` once set).

## How the pieces talk (two S3 endpoints)

Presigned URLs are consumed by the **client** (your browser / claude.ai's sandbox), so they must name a **public** bucket host — which usually isn't the internal one the server uses on the container network. The server therefore holds two S3 clients:

- `S3_ENDPOINT` (`http://minio:9000`) — internal `get`/`put`/`head`.
- `S3_PUBLIC_ENDPOINT` (`http://localhost:9100`) — the host baked into presigned URLs. `generate_presigned_url` only *signs* (no network), so the presign client never needs to reach it from inside the container.

## Connect from claude.ai

Two things must be publicly reachable: the **MCP endpoint** and the **bucket**.

1. **Bucket:** the simplest correct choice is **Cloudflare R2 or AWS S3** — one public endpoint, reachable by both the server and the client, no MinIO to expose. Set `S3_PUBLIC_ENDPOINT` (and `S3_ENDPOINT`) to it and drop the `minio` service. On S3/R2, `set_bucket_cors()` also configures browser CORS for the upload widget (MinIO does it via `MINIO_API_CORS_ALLOW_ORIGIN`).
2. **MCP endpoint:** put `convertx-mcp` behind a tunnel and set `PUBLIC_BASE_URL` to the tunnel hostname:
   ```bash
   # set CF_TUNNEL_TOKEN in .env, route the hostname → http://convertx-mcp:8080
   docker compose --profile cloudflare up -d --build
   ```
3. **Upload from the sandbox:** whitelist the bucket's domain under claude.ai → Settings → Capabilities → Code execution → Additional allowed domains, so the agent's `PUT` to the presigned URL is allowed.

## Auth model
- `/mcp` is **bearer-gated** (fail-closed: a non-loopback bind with no bearer and no `MCP_ALLOW_EXTERNAL_BIND=1` refuses to start).
- `/u/<shortid>` (the upload widget) is **public** — the human opens it in a browser without the MCP credential. It's protected by the unguessable id and the presigned URL's own signature/expiry, not the bearer.

## Caveats
- **ConvertX's HTTP contract is unofficial** (login → `GET /` mints jobId → multipart `/upload` → `/convert` → poll `/progress` → `/download`; the `auth` cookie is `Secure`). **Pin the image** — it can change between releases.
- **ConvertX is AGPL-3.0.** Serving it over a network triggers §13 copyleft — you may owe users the complete corresponding source. Review before hosting for others.
- Set a bucket **lifecycle rule** to expire `src/` and `out/` objects; keep presign TTLs short.
