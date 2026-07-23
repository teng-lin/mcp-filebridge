# mcp-filebridge

Move files between remote MCP clients and servers without putting binary data in
JSON-RPC.

`mcp-filebridge` uses short-lived, S3-compatible presigned URLs as a file data
plane. The MCP tool result carries a small upload or download offer; the bytes
move separately over HTTPS. The repository provides matching Python and Node
ESM implementations, an MCP Apps upload widget, a reusable OAuth provider, and
three deployable conversion examples.

> **Project status:** working source and deployable examples, not a published
> package release. Install the Python package from this checkout and import the
> Node modules from `ts/`. Python reports version `0.1.0`; the private Node
> package reports `0.0.1`.

## Why this exists

Remote MCP clients can call tools over JSON-RPC, but that is a poor channel for
large files. Base64 expands the payload, tool results are size-limited, and a
large document returned as text consumes the model's context on every following
turn.

The bridge keeps control and data separate:

```text
                                  MCP: small JSON offers
Remote client / widget  <-------------------------------->  MCP server
          |                                                       |
          | PUT or GET raw bytes over HTTPS                       | head/get/put
          v                                                       v
                         S3 / R2 / MinIO
```

An upload offer supports two actors:

- An **agent** sends a raw `PUT` to a presigned URL.
- A **human** opens a short `/u/<id>` link or uses the inline MCP Apps file
  picker. The browser then uploads the file.

The server checks that the object arrived, performs its domain work, and returns
a presigned download offer. No binary content crosses the MCP protocol.

## Repository map

| Path | Purpose |
|---|---|
| [`python/mcp_filebridge`](python/mcp_filebridge) | Python S3 helper, widget, OAuth provider, conversion broker, and host render gates |
| [`ts`](ts) | Node ESM S3 helper plus ticket and widget-gate twins |
| [`spec/offer.golden.json`](spec/offer.golden.json) | Normalized language-neutral upload-offer contract |
| [`spec/verify_parity.py`](spec/verify_parity.py) | Live Python ↔ Node offer parity check |
| [`examples/convertx`](examples/convertx/README.md) | Python MCP server wrapping ConvertX |
| [`examples/markdownify`](examples/markdownify/README.md) | Node MCP backend behind a Python OAuth gateway |
| [`examples/docling`](examples/docling/README.md) | Python MCP server handing a presigned source URL directly to Docling |

The shared widget host bridge is bundled as Python package data in
[`widget_bridge.js`](python/mcp_filebridge/widget_bridge.js). Both the Python
widget and the polyglot Markdown example use that copy.

## Requirements

- Python 3.10 or newer
- Node.js 20.11 or newer, plus npm, for the Node helper and parity checks
- An existing bucket on AWS S3, Cloudflare R2, MinIO, or another
  SigV4-compatible object store
- HTTPS endpoints reachable by the clients that will consume the presigned URLs
- Docker and Compose only for the deployable examples and live integration tests

This repository does not currently include a license file.

## Install from source

Clone the repository and install the Python package in a virtual environment:

```bash
git clone https://github.com/teng-lin/mcp-filebridge.git
cd mcp-filebridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ./python
```

Install the locked Node dependencies separately:

```bash
cd ts
npm ci
cd ..
```

For Python development and tests, install the `dev` extra:

```bash
python -m pip install -e "./python[dev]"
```

## Quick start: Python

Create one S3 client for server-side operations and, when the public bucket
hostname differs, another client for presigning:

```python
from mcp_filebridge.s3_filebridge import S3FileHelper, make_client

credentials = {
    "access_key": "spikekey",
    "secret_key": "spikesecret",
}

internal_s3 = make_client(
    "http://minio:9000",
    credentials["access_key"],
    credentials["secret_key"],
)
public_s3 = make_client(
    "https://s3.example.com",
    credentials["access_key"],
    credentials["secret_key"],
)

files = S3FileHelper(
    internal_s3,
    "documents",
    "https://mcp.example.com",
    presign_s3=public_s3,
)

offer = files.offer_upload(filename="report.pdf")
print(offer["src_key"])
print(offer["agent_upload"]["url"])
print(offer["human_upload"]["url"])
```

The bucket must already exist. `offer_upload()` only signs a request; it does
not contact the public endpoint.

Upload raw bytes to the agent URL:

```bash
curl -X PUT -T report.pdf '<agent_upload.url>'
```

Then wait for the object and offer a download:

```python
import asyncio

uploaded = asyncio.run(files.await_upload(offer["src_key"], timeout=45))

download = files.offer_download(
    key=uploaded["key"],
    filename="report.pdf",
    mime="application/pdf",
)
print(download["url"])
```

To serve the human fallback link from an existing Starlette/FastMCP app, add
the helper's routes:

```python
app.router.routes.extend(files.routes())
```

That exposes `GET /u/{sid}`. The returned page contains a file picker and sends
the selected file directly to the presigned S3 URL.

## Quick start: Node

The `ts/` directory contains JavaScript ESM modules used as the Node/TypeScript
side of the contract:

```js
import {
  S3FileHelper,
  makeS3Client,
} from "./ts/s3_filebridge.mjs";

const credentials = {
  accessKeyId: "spikekey",
  secretAccessKey: "spikesecret",
};

const internalS3 = makeS3Client("http://minio:9000", credentials);
const publicS3 = makeS3Client("https://s3.example.com", credentials);

const files = new S3FileHelper(
  internalS3,
  "documents",
  "https://mcp.example.com",
  { presignS3: publicS3 },
);

const upload = await files.offerUpload({ filename: "report.pdf" });
console.log(upload.agent_upload.url);
console.log(upload.human_upload.url);
```

The Node helper implements presigning, offers, the in-memory short-link store,
and `uploadPage()`. Bucket creation, upload polling, CORS configuration, and
HTTP route registration remain the host application's responsibility. After the
host confirms the upload, call `offerDownload({ key, filename, mime })` to
create the download offer.

## The offer contract

`offer_upload()` / `offerUpload()` returns this shape:

```json
{
  "status": "upload_required",
  "src_key": "src/<uuid>/report.pdf",
  "expires_in_seconds": 300,
  "mime_locked": false,
  "human_upload": {
    "url": "https://mcp.example.com/u/<short-id>",
    "instructions": "Open on the device that has the file, then pick it. Works on mobile."
  },
  "agent_upload": {
    "method": "PUT",
    "url": "https://s3.example.com/documents/src/<uuid>/report.pdf?<sigv4>",
    "body": "raw file bytes (not multipart/form-data)",
    "returns": "{\"status\":\"added\",\"key\":...}",
    "example": "curl -X PUT -T <file> '<presigned-url>'"
  },
  "agent_instructions": "Try agent_upload (PUT the bytes); else surface human_upload.url to the user."
}
```

`agent_upload.returns` is compatibility metadata describing the status produced
after the server confirms the upload. A successful direct S3 `PUT` normally
has an empty response body; do not parse it as JSON. In Python,
`await_upload()` returns the confirmed `{"status": "added", ...}` result.

Volatile UUIDs, short IDs, timestamps, and signatures differ between calls.
[`offer.golden.json`](spec/offer.golden.json) is the normalized conformance
vector. The parity check verifies all stable fields, key layout, URL target,
and required SigV4 parameters.

`offer_download()` / `offerDownload()` returns:

```json
{
  "status": "download_ready",
  "filename": "report.pdf",
  "mime_type": "application/pdf",
  "url": "https://s3.example.com/documents/<key>?<sigv4>",
  "expires_in_seconds": 900
}
```

If `mime` is supplied when creating an upload offer, the content type becomes
part of the signature and `mime_locked` is `true`. The uploader must send the
same `Content-Type` header.

## API reference

### Python transport

| API | Behavior |
|---|---|
| `make_client(endpoint_url, access_key, secret_key, *, region="us-east-1")` | Builds a path-style SigV4 `boto3` client |
| `S3FileHelper(s3, bucket, widget_base_url, *, presign_s3=None, upload_ttl=300, download_ttl=900, links=None)` | Configures internal operations, public signing, TTLs, and short links |
| `offer_upload(*, filename, key_prefix="src", mime=None)` | Creates a presigned `PUT` and both actor paths |
| `await_upload(key, *, timeout=45, interval=1.0)` | Polls `head_object` until the key exists or raises `TimeoutError` |
| `offer_download(*, key, filename, mime)` | Creates a presigned `GET` |
| `routes()` | Returns the public Starlette `GET /u/{sid}` route |
| `set_bucket_cors()` | Applies browser PUT/GET bucket CORS where supported; falls back when the backend returns `NotImplemented` |
| `ShortLinkStore` | Process-local map from an eight-character ID to a presigned URL |
| `upload_page(put_url)` | Returns the standalone HTML upload page |

The built-in `ShortLinkStore` uses a 32-bit, eight-hex-character ID and has no
persistence or cross-process sharing. A restart invalidates its links. Treat it
as a single-process example, not an authentication boundary; supply a stronger,
shared store and rate-limit the route for an internet-scale deployment.

### Node transport

| Export | Behavior |
|---|---|
| `makeS3Client(endpoint, credentials)` | Builds a path-style `S3Client` with checksum calculation limited to required cases |
| `S3FileHelper` | Provides `offerUpload()` and `offerDownload()` |
| `ShortLinkStore` | Process-local short-link map |
| `uploadPage(putUrl)` | Returns the standalone HTML upload page |

If you pass your own AWS SDK v3 client, set
`requestChecksumCalculation: "WHEN_REQUIRED"`; otherwise the SDK may sign a
CRC32 checksum that a plain browser or `curl` PUT does not send.

## MCP Apps upload widget

The Python widget registers:

- a `ui://convertx/upload-v1` HTML resource;
- an `upload_file(filename, target="")` MCP tool;
- Claude and ChatGPT resource metadata;
- a direct browser-to-S3 upload with progress reporting;
- a short-link fallback when the host does not render the widget.

Register it on a `FastMCP` server:

```python
from fastmcp import FastMCP
from mcp_filebridge.widget import register_upload_widget

mcp = FastMCP("file-tools")

register_upload_widget(
    mcp,
    files,
    s3_public_endpoint="https://s3.example.com",
    public_base_url="https://mcp.example.com",
    mint_upload=lambda filename: files.offer_upload(filename=filename),
)

app = mcp.http_app(path="/mcp")
app.router.routes.extend(files.routes())
```

Set `MCP_UPLOAD_WIDGET=0` to skip widget registration and keep only the
short-link/agent paths.

The widget's network policy names both the MCP origin and the public bucket
origin. The Python implementation emits FastMCP's standard `ui.csp` metadata
plus ChatGPT's compatibility `openai/widgetCSP`; the Node gate helper emits
`ui.csp.connectDomains` and `openai/widgetCSP.connect_domains` explicitly.

## Connect from Claude and ChatGPT

Two endpoints must be reachable from the relevant host:

1. `https://mcp.example.com/mcp` for MCP and OAuth.
2. The hostname in `S3_PUBLIC_ENDPOINT` for presigned uploads and downloads.

Use real HTTPS origins in production. A private container hostname such as
`http://minio:9000` cannot appear in a URL consumed by a remote browser or
agent.

### Claude

Follow Anthropic's [remote custom connector
setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp):

1. Add the public `/mcp` URL under **Customize → Connectors**. Team and
   Enterprise owners add it under **Organization settings → Connectors**.
2. Enter `MCP_OAUTH_CLIENT_ID` and `MCP_OAUTH_CLIENT_SECRET` in **Advanced
   settings** when using the static OAuth client.
3. Connect/authenticate the connector, then enable it for the conversation from
   the composer **+ → Connectors** menu.

For the **agent/curl upload path**, Claude must permit code execution and
outbound access to the exact `S3_PUBLIC_ENDPOINT` hostname:

- On Free, Pro, and Max plans, enable **Code execution and file creation**
  under **Settings → Capabilities**. These plans use Anthropic-approved
  network sources and do not expose the Team/Enterprise per-domain owner
  control, so the agent path may remain unavailable for an unapproved bucket
  host; use the widget path instead.
- On Team and Enterprise plans, an owner configures **Organization settings →
  Capabilities**, enables code execution and network egress, selects
  **Allow network egress to package managers and specific domains**, and adds
  the bucket hostname to the domain allowlist. No individual entry is needed
  when the organization already allows all domains.

Without these capabilities and, where applicable, the domain allowlist entry,
the sandbox can receive the presigned URL but its `PUT` is blocked. Anthropic's
[file-creation capability guide](https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude)
documents the plan-specific controls.

The inline widget also requires the bucket's browser CORS policy and correct
MCP Apps CSP metadata. Those server-side requirements are separate from the
Claude code-execution domain allowlist.

If the MCP endpoint is behind a firewall, the connection originates from
Anthropic's cloud even when using Claude Desktop. Allowlist Anthropic's
published IP ranges or expose the endpoint through an approved public ingress.

### ChatGPT

Follow OpenAI's [developer-mode app
setup](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt):

1. Enable Developer mode under **Settings → Security and login** if your
   account or workspace permits it.
2. Under **Settings → Plugins**, select **+** to create a developer-mode app
   and provide the public `/mcp` URL.
3. Complete OAuth, verify the scanned tools, and add the app from the composer
   **+ → More** menu.
4. Review the app's action permissions. Workspace admins may also need to grant
   developer-mode access, app access, and specific actions.

Action permissions govern tool calls; widget network access is separate.
ChatGPT does **not** document a Claude-style user setting named “Additional
allowed domains” for Apps SDK widget egress. The app instead declares its
allowlist in the resource CSP:

- `ui.csp.connectDomains` for the standard MCP Apps metadata;
- `openai/widgetCSP.connect_domains` for ChatGPT compatibility.

This repository emits both forms with `PUBLIC_BASE_URL` and
`S3_PUBLIC_ENDPOINT`. If either hostname changes, redeploy and refresh/re-scan
the app metadata in ChatGPT. Published apps use reviewed metadata snapshots, so
domain changes require a new reviewed version. See the Apps SDK [CSP
reference](https://developers.openai.com/apps-sdk/reference#component-resource-_meta-fields).

For ChatGPT, prefer the widget upload path. If another ChatGPT runtime or agent
uses `agent_upload` directly, that runtime must independently permit outbound
HTTPS to the bucket; no equivalent manual ChatGPT allowlist is assumed here.
The OAuth integration test in this repository exercises a simulated CIMD
client, not a live ChatGPT connection; validate OAuth and action permissions in
the target ChatGPT workspace before deployment.

## Internal and public S3 endpoints

Deployments with different internal and public hostnames need two clients:

| Setting | Used by | Example |
|---|---|---|
| `S3_ENDPOINT` | Server-side `head`, `get`, and `put` calls | `http://minio:9000` |
| `S3_PUBLIC_ENDPOINT` | Host embedded in browser/agent presigned URLs | `https://s3.example.com` |

Pass the internal client as `s3` and the public client as `presign_s3` /
`presignS3`. Signing is local, so the server does not need a network route to
the public hostname merely to create a URL.

For Docling, the server itself consumes the source presigned GET, so that
specific URL can use the internal endpoint. Browser uploads and user-facing
downloads still use the public endpoint.

## Browser CORS

The upload page and direct-upload widget send cross-origin `PUT` requests:

- AWS S3, Cloudflare R2, and compatible MinIO AIStor modes use bucket CORS.
  Python's `set_bucket_cors()` allows `PUT` and `GET`, all origins and headers,
  and exposes `ETag`.
- The bundled `quay.io/minio/minio` image currently returns `NotImplemented`
  for `PutBucketCors`. The example stacks configure
  `MINIO_API_CORS_ALLOW_ORIGIN` on that server instead.
- The Markdown conversion widget instead sends a cross-origin `POST` to its
  gateway, whose broker route returns its own CORS headers.

Tighten `AllowedOrigins` for production if your object-store deployment and
host origins are stable.

## Authentication

[`mcp_filebridge.oauth`](python/mcp_filebridge/oauth.py) composes two FastMCP
authentication modes:

- A constant-time single bearer-token verifier for programmatic clients.
- A password-gated, single-tenant OAuth 2.1 provider built on FastMCP's
  `InMemoryOAuthProvider`.

```python
import os

from fastmcp import FastMCP
from mcp_filebridge.oauth import (
    build_auth,
    build_oauth_provider,
    get_oauth_config,
)

oauth_config = get_oauth_config()
oauth = build_oauth_provider(oauth_config) if oauth_config else None
auth = build_auth(os.environ.get("MCP_BEARER_TOKEN") or None, oauth)

mcp = FastMCP("file-tools", auth=auth)
```

`build_auth()` returns `MultiAuth` when both modes are present, one provider
when only one is configured, and `None` when neither is configured. ConvertX
and Docling refuse an unauthenticated non-loopback bind unless
`MCP_ALLOW_EXTERNAL_BIND=1`. Markdownify's gateway has no equivalent bind
guard, so configure bearer or OAuth authentication before exposing it. The
library does not impose either server policy for you.

### OAuth environment variables

| Variable | Meaning |
|---|---|
| `MCP_BEARER_TOKEN` | Optional static bearer token |
| `MCP_OAUTH_PASSWORD` | Password shown at `/login`; at least 16 characters |
| `MCP_OAUTH_BASE_URL` | Bare public HTTPS origin, with no path, query, or fragment |
| `MCP_OAUTH_STATE_PATH` | Optional JSON file for clients and issued tokens |
| `MCP_OAUTH_TRUST_PROXY=1` | Trust `CF-Connecting-IP` for login throttling |
| `MCP_OAUTH_CLIENT_ID` | Optional static client ID; disables open DCR by default |
| `MCP_OAUTH_CLIENT_SECRET` | Optional secret for the static client |
| `MCP_OAUTH_REDIRECT_URIS` | Comma-separated exact redirect allowlist |
| `MCP_OAUTH_ALLOW_DCR=1` | Re-enable RFC 7591 registration as a fallback |
| `MCP_OAUTH_CIMD_ALLOW_LOOPBACK=1` | Test-only relaxation for local CIMD documents; never use in production |

`MCP_OAUTH_PASSWORD` and `MCP_OAUTH_BASE_URL` are an all-or-nothing pair. A
partial or weak configuration fails closed. When a static client is configured,
the default redirect is Claude's connector callback.

The provider also supports URL-form client IDs through Client ID Metadata
Documents (CIMD). Fetches are HTTPS-only in production, capped at 10 KiB,
limited to three seconds, do not follow redirects, and reject private,
loopback, link-local, reserved, and multicast targets.

Persisted OAuth state contains active credentials. Mount it on durable private
storage, restrict access, and back it up as a secret.

## Convert-on-upload broker

[`mcp_filebridge.convert`](python/mcp_filebridge/convert.py) supports widgets
that POST a file to the MCP gateway and convert it on receipt:

```python
from mcp_filebridge.convert import register_convert_route

app = mcp.http_app(path="/mcp")
register_convert_route(app)
```

This inserts `POST /u/convert/{ticket}` and its `OPTIONS` preflight route. A
short-lived HMAC ticket authorizes one `src_key`. The Node backend mints the
ticket with `mintTicket()` and the Python gateway verifies it with
`ticket_payload()`; parity tests lock their encoding and deterministic
`md_key` derivation together.

| Variable | Default | Purpose |
|---|---:|---|
| `MCP_UPLOAD_SIGNING_KEY` | `MCP_BEARER_TOKEN`, then `dev-key` | Shared HMAC key; set an independent random value in production |
| `CONVERT_MAX_BYTES` | `52428800` | Maximum request body accepted by the broker |
| `S3_BUCKET` | `markdownify` | Destination bucket |
| `S3_ENDPOINT` | `http://minio:9000` | Internal S3 endpoint |
| `S3_PUBLIC_ENDPOINT` | `S3_ENDPOINT` | Download-presigning endpoint |
| `MD_DOWNLOAD_TTL` | `86400` | Markdown download URL lifetime in seconds |

Install the optional conversion dependencies with:

```bash
python -m pip install -e "./python[markitdown]"
```

## Deployable examples

| Example | MCP backend | File workflow | Local endpoint |
|---|---|---|---|
| [ConvertX](examples/convertx/README.md) | Python FastMCP | Widget/agent PUT → S3 → ConvertX → presigned result | `http://localhost:9400/mcp` |
| [markdownify](examples/markdownify/README.md) | Node SDK over stdio, Python OAuth gateway | Widget POST converts on receipt; agent path uses S3; large Markdown is previewed and paged | `http://localhost:8090/mcp` |
| [Docling](examples/docling/README.md) | Python FastMCP | Widget PUT → presigned GET passed directly to Docling → Markdown preview/paging | `http://localhost:9500/mcp` |

Each example includes a Compose stack and `.env.example`. Run only one stack
that binds the same host ports, or override its port variables.

ConvertX has a convenience Makefile:

```bash
cd examples/convertx
make setup
# Edit .env: set both MCP_OAUTH_PASSWORD and MCP_OAUTH_BASE_URL,
# or clear both for bearer-only local use.
make config
make up
make smoke
```

Markdownify and Docling use Compose directly:

```bash
cd examples/markdownify  # or examples/docling
cp .env.example .env
# Edit .env before continuing:
# - set a random MCP_BEARER_TOKEN, without the example's inline comment;
# - replace or clear the tunnel token;
# - set both MCP_OAUTH_PASSWORD and MCP_OAUTH_BASE_URL, or clear both.
docker compose config
docker compose up -d --build
```

Docker Compose treats the inline comments on some empty example assignments as
values. Inspect the resolved configuration and never rely on checked-in
comments or placeholders as credentials.

Read the example-specific README before exposing a stack. ConvertX uses an
unofficial upstream HTTP contract and is AGPL-3.0; Docling has a large model
footprint; Markdownify's Python gateway and Node child must share the upload
signing key.

## Production checklist

- Use HTTPS for the MCP origin and every client-facing S3 origin.
- Set independent random bearer, OAuth, upload-signing, storage, and upstream
  application secrets.
- Keep upload and download TTLs short.
- Add lifecycle rules for `src/`, `out/`, and `md/` objects.
- Configure bucket CORS and the host-specific domain allowlists described above.
- Keep `S3_ENDPOINT` private and put only `S3_PUBLIC_ENDPOINT` into presigned
  URLs.
- Persist OAuth state on private durable storage.
- Replace the process-local short-link store before using multiple workers.
- Treat presigned URLs as bearer credentials and avoid logging their query
  strings.
- Keep `/mcp` authenticated. `/u/<id>`, OAuth metadata/routes, and broker
  upload routes are public by design. Protect them according to their actual
  controls: short-link lookup plus presign expiry, OAuth login throttling, or
  broker HMAC expiry plus a body-size cap.
- Restrict object-store credentials to the required bucket and operations.

## Run the checks

### Unit tests

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e "./python[dev]"
pytest -m "not integration" python/tests

npm --prefix ts ci
npm --prefix ts test
```

The Python unit suite covers offers, short links, upload polling, widget
registration and render gates, OAuth configuration and flows, broker tickets,
route limits, and Python ↔ Node conversion parity. Node uses the built-in test
runner for ticket, key-derivation, and widget metadata checks.

### Live S3 and language parity

Start MinIO:

```bash
docker run --rm -d --name mcp-filebridge-minio \
  -p 9100:9000 \
  -e MINIO_ROOT_USER=spikekey \
  -e MINIO_ROOT_PASSWORD=spikesecret \
  quay.io/minio/minio:latest server /data
```

Then run the Python self-check and cross-language offer verification:

```bash
python -m mcp_filebridge.s3_filebridge
python spec/verify_parity.py
pytest -m integration python/tests
```

`spec/verify_parity.py` rewrites `spec/offer.golden.json` from the Python result,
runs the Node live round trip, normalizes volatile values, and fails if the
contracts differ.

The integration tests skip services that are not reachable. Start the relevant
example stack to exercise its full workflow.

Stop the test MinIO container when finished:

```bash
docker stop mcp-filebridge-minio
```

## Compatibility notes

- Python and Node emit byte-identical normalized upload offers, not identical
  signatures or UUIDs.
- The Python package is self-contained; it loads the shared widget bridge with
  `importlib.resources`.
- The MCP Apps inline render remains host-controlled. The resource/tool
  metadata is testable locally, but final iframe behavior must be tested in the
  target Claude or ChatGPT client.
- Sandboxed widgets may block downloads or navigation. The examples surface
  download URLs as clickable chat links and use paging for large text results.
- The OAuth implementation is single-tenant and uses opaque tokens. It does not
  implement RFC 8707 resource/audience binding.

## Relationship to SEP-2631

[SEP-2631: File Objects and
Transfer](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2631)
is a draft MCP proposal for file-valued inputs and outputs plus
`files/authorizeUpload` / `files/authorizeDownload`.

The proposal defines a control plane and leaves the HTTPS data plane and
storage lifecycle to implementations. `mcp-filebridge` is an S3/broker data
plane with its own current offer contract. A future adapter can map SEP-2631
authorization calls to `offer_upload()` and `offer_download()` while retaining
the widget as a fallback for clients that do not support the proposal.

SEP-2631 does not replace two example-specific concerns here:

- Docling needs a server-reachable HTTP URL that it can fetch directly.
- Large Markdown still needs previewing and paging so model context stays
  bounded.

## Contributing

Keep changes across the language boundary synchronized:

1. Update both S3 helpers when the offer contract changes.
2. Update both ticket/key implementations when broker signing changes.
3. Keep the shared widget bridge single-sourced.
4. Run Python units, Node units, and `spec/verify_parity.py`.
5. Review the generated golden diff before committing it.
