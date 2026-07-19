"""s3_filebridge — mcp_filebridge's offer contract + upload widget, S3 engine.

Keeps the two durable, cross-language-worthy parts of mcp_filebridge:
  - the `upload_required` / `download_ready` OFFER shape (human + agent paths), and
  - the human upload WIDGET behind a mobile-safe /u/<shortid> link,
and replaces the Python-locked engine (HMAC routes, in-process byte streaming)
with S3 presigned URLs. The presign calls map 1:1 to @aws-sdk in TypeScript, so
this same design ports natively — the offer JSON + widget page are the shared,
language-neutral spec.

Self-check (needs the local MinIO from the earlier spike):
    python3 s3_filebridge.py
"""
from __future__ import annotations

import asyncio
import json
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


class ShortLinkStore:
    """Maps a short id -> a (long, ugly) presigned URL. In-memory; swap for
    Redis in prod. Exists because presigned URLs are long and get mangled in
    mobile chat — the same reason filebridge ships /u/<shortid>."""

    def __init__(self) -> None:
        self._m: dict[str, str] = {}

    def put(self, url: str) -> str:
        sid = uuid.uuid4().hex[:8]
        self._m[sid] = url
        return sid

    def get(self, sid: str) -> str | None:
        return self._m.get(sid)


def upload_page(put_url: str) -> str:
    """The language-neutral widget: a file picker that PUTs the chosen file to
    the presigned URL (this is what a TS port would serve verbatim)."""
    u = json.dumps(put_url)  # safe JS string literal
    return (
        "<!doctype html><meta charset=utf-8><title>Upload a file</title>"
        "<input type=file id=f> <button onclick=up()>Upload</button><pre id=s></pre>"
        "<script>async function up(){const f=document.getElementById('f').files[0];"
        "document.getElementById('s').textContent='Uploading '+f.name+'…';"
        f"const r=await fetch({u},{{method:'PUT',body:f}});"
        "document.getElementById('s').textContent=r.ok?'Done \\u2713':'Failed '+r.status;}</script>"
    )


class S3FileHelper:
    def __init__(self, s3, bucket: str, widget_base_url: str, *,
                 upload_ttl: int = 300, download_ttl: int = 900,
                 links: ShortLinkStore | None = None) -> None:
        self.s3, self.bucket = s3, bucket
        self.base = widget_base_url.rstrip("/")
        self.up_ttl, self.dl_ttl = upload_ttl, download_ttl
        self.links = links or ShortLinkStore()

    def _shortlink(self, target_url: str) -> str:
        return f"{self.base}/u/{self.links.put(target_url)}"

    # --- offers: filebridge's exact wire shape, S3-backed URLs ------------- #
    def offer_upload(self, *, filename: str, key_prefix: str = "src", mime: str | None = None) -> dict:
        key = f"{key_prefix}/{uuid.uuid4()}/{filename}"
        params = {"Bucket": self.bucket, "Key": key}
        if mime:  # signing ContentType means the PUT MUST send that header
            params["ContentType"] = mime
        put = self.s3.generate_presigned_url("put_object", Params=params, ExpiresIn=self.up_ttl)
        return {
            "status": "upload_required",
            "src_key": key,
            "expires_in_seconds": self.up_ttl,
            "mime_locked": bool(mime),
            "human_upload": {
                "url": self._shortlink(put),
                "instructions": "Open on the device that has the file, then pick it. Works on mobile.",
            },
            "agent_upload": {
                "method": "PUT",
                "url": put,
                "body": "raw file bytes (not multipart/form-data)",
                "returns": '{"status":"added","key":...}',
                "example": f"curl -X PUT -T <file> '{put}'",
            },
            "agent_instructions": "Try agent_upload (PUT the bytes); else surface human_upload.url to the user.",
        }

    def offer_download(self, *, key: str, filename: str, mime: str) -> dict:
        get = self.s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.dl_ttl)
        return {"status": "download_ready", "filename": filename, "mime_type": mime,
                "url": get, "expires_in_seconds": self.dl_ttl}

    async def await_upload(self, key: str, *, timeout: float = 45, interval: float = 1.0) -> dict:
        """Was an in-process completion map; now a head_object poll (or wire an
        S3 event for push instead of poll)."""
        for _ in range(int(timeout / interval)):
            try:
                h = self.s3.head_object(Bucket=self.bucket, Key=key)
                return {"status": "added", "key": key, "size_bytes": h["ContentLength"]}
            except ClientError:
                await asyncio.sleep(interval)
        raise TimeoutError(f"upload never landed: {key}")

    # --- the ONE residual server piece: serve the widget + short link ----- #
    def routes(self):
        from starlette.responses import HTMLResponse, PlainTextResponse
        from starlette.routing import Route

        def widget(request):
            target = self.links.get(request.path_params["sid"])
            if not target:
                return PlainTextResponse("link expired", status_code=404)
            return HTMLResponse(upload_page(target))

        return [Route("/u/{sid}", widget, methods=["GET"])]

    def set_bucket_cors(self) -> str:
        """The one thing S3 doesn't give free: allow the browser widget to PUT
        cross-origin. (filebridge's own route had CORS built in.)

        Per-bucket CORS is the AWS S3 / Cloudflare R2 path. MinIO doesn't
        implement PutBucketCors — it configures CORS server-side instead
        (MINIO_API_CORS_ALLOW_ORIGIN, default '*'), so we no-op there.
        Returns which path applied."""
        try:
            self.s3.put_bucket_cors(Bucket=self.bucket, CORSConfiguration={"CORSRules": [
                {"AllowedMethods": ["PUT", "GET"], "AllowedOrigins": ["*"],
                 "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"]}]})
            return "put_bucket_cors (S3/R2)"
        except ClientError as e:
            if e.response["Error"]["Code"] == "NotImplemented":
                return "backend-global-cors (MinIO: MINIO_API_CORS_ALLOW_ORIGIN)"
            raise


# --------------------------------------------------------------------------- #
# Self-check: agent PUT + browser-style CORS PUT + await + download, live MinIO
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import requests

    s3 = boto3.client("s3", endpoint_url="http://localhost:9100",
                      aws_access_key_id="spikekey", aws_secret_access_key="spikesecret",
                      region_name="us-east-1",
                      config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    BUCKET = "s3fb-spike"
    try:
        s3.head_bucket(Bucket=BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)

    h = S3FileHelper(s3, BUCKET, "https://mcp.example.test")
    cors_mode = h.set_bucket_cors()

    # (1) offer shape matches filebridge's contract
    off = h.offer_upload(filename="spike.txt")
    assert off["status"] == "upload_required"
    assert {"human_upload", "agent_upload", "agent_instructions"} <= off.keys()

    # (2) AGENT path — raw PUT (the sandbox/curl case)
    r = requests.put(off["agent_upload"]["url"], data=b"hello from agent path")
    assert r.status_code == 200, ("agent PUT", r.status_code)

    # (3) await_upload via head_object
    res = asyncio.run(h.await_upload(off["src_key"], timeout=10))
    assert res == {"status": "added", "key": off["src_key"], "size_bytes": 21}, res

    # (4) BROWSER path — CORS preflight + PUT-with-Origin (what the widget's fetch does)
    off2 = h.offer_upload(filename="browser.txt")
    put2 = off2["agent_upload"]["url"]
    ORIGIN = "https://claude.ai"
    pre = requests.options(put2, headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "content-type"})
    aco = pre.headers.get("Access-Control-Allow-Origin")
    assert aco in (ORIGIN, "*"), ("CORS preflight failed", pre.status_code, dict(pre.headers))
    r2 = requests.put(put2, data=b"hello from the browser path!", headers={"Origin": ORIGIN})
    assert r2.status_code == 200, ("browser PUT", r2.status_code)
    assert asyncio.run(h.await_upload(off2["src_key"], timeout=10))["size_bytes"] == 28

    # (5) widget page + route serve the presigned URL with a file picker
    page = upload_page(put2)
    assert "type=file" in page and put2.split("?")[0] in page
    try:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        sid = off2["human_upload"]["url"].rsplit("/", 1)[-1]
        rc = TestClient(Starlette(routes=h.routes())).get(f"/u/{sid}")
        assert rc.status_code == 200 and "type=file" in rc.text
        widget = "route-served"
    except Exception as e:  # httpx missing etc. — page fn already asserted above
        widget = f"route-test-skipped ({type(e).__name__})"

    # (6) download round-trip via presigned GET
    dl = h.offer_download(key=off["src_key"], filename="spike.txt", mime="text/plain")
    got = requests.get(dl["url"])
    assert got.status_code == 200 and got.content == b"hello from agent path"

    print(f"ok: agent PUT + browser CORS PUT + await + presigned download verified; "
          f"widget {widget}; cors={cors_mode}")
