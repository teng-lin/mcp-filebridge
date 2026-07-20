"""Unit tests for s3_filebridge — no network: generate_presigned_url only signs."""
import asyncio
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.client import Config

from mcp_filebridge import s3_filebridge as fb

REQUIRED_SIGV4 = {"X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date",
                  "X-Amz-Expires", "X-Amz-Signature", "X-Amz-SignedHeaders"}


def _client(endpoint="http://s3.local:9000"):
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k",
                        aws_secret_access_key="s", region_name="us-east-1",
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))


@pytest.fixture
def helper():
    return fb.S3FileHelper(_client(), "bucket", "https://mcp.example.test")


def test_offer_upload_shape(helper):
    off = helper.offer_upload(filename="doc.md")
    assert off["status"] == "upload_required"
    assert {"human_upload", "agent_upload", "agent_instructions", "src_key"} <= off.keys()
    assert off["human_upload"]["url"].startswith("https://mcp.example.test/u/")
    assert off["agent_upload"]["method"] == "PUT"
    assert off["src_key"].startswith("src/") and off["src_key"].endswith("/doc.md")


def test_offer_upload_url_is_valid_presigned(helper):
    url = helper.offer_upload(filename="doc.md")["agent_upload"]["url"]
    q = set(parse_qs(urlsplit(url).query))
    assert REQUIRED_SIGV4 <= q, f"missing SigV4 params: {REQUIRED_SIGV4 - q}"


def test_offer_download_shape(helper):
    off = helper.offer_download(key="out/x/doc.docx", filename="doc.docx", mime="application/x")
    assert off["status"] == "download_ready"
    assert off["filename"] == "doc.docx" and off["mime_type"] == "application/x"
    assert REQUIRED_SIGV4 <= set(parse_qs(urlsplit(off["url"]).query))


def test_mime_locked_flag(helper):
    assert helper.offer_upload(filename="a", mime="text/plain")["mime_locked"] is True
    assert helper.offer_upload(filename="a")["mime_locked"] is False


def test_presign_seam_uses_public_endpoint():
    # presign_s3 (public) signs the URL; internal s3 is a different host.
    h = fb.S3FileHelper(_client("http://internal:9000"), "bucket", "https://mcp.example.test",
                        presign_s3=_client("https://s3.public.test"))
    url = h.offer_upload(filename="doc.md")["agent_upload"]["url"]
    assert urlsplit(url).netloc == "s3.public.test"


def test_upload_page_has_picker_and_url():
    url = "https://s3.public.test/bucket/key?X-Amz-Signature=abc"
    page = fb.upload_page(url)
    assert "type=file" in page
    assert "PUT" in page
    assert url.split("?")[0] in page  # the URL is embedded


def test_shortlink_store_roundtrip():
    store = fb.ShortLinkStore()
    sid = store.put("https://x/very/long/presigned")
    assert store.get(sid) == "https://x/very/long/presigned"
    assert store.get("nope") is None


def test_shortlink_unique():
    store = fb.ShortLinkStore()
    ids = {store.put(f"u{i}") for i in range(50)}
    assert len(ids) == 50


class _FakeS3:
    def __init__(self, exists_after=0):
        self.calls = 0
        self.exists_after = exists_after

    def head_object(self, **_):
        self.calls += 1
        if self.calls > self.exists_after:
            return {"ContentLength": 123}
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")


def test_await_upload_success():
    h = fb.S3FileHelper(_FakeS3(exists_after=1), "b", "https://m", presign_s3=_client())
    res = asyncio.run(h.await_upload("k", timeout=5, interval=0.01))
    assert res == {"status": "added", "key": "k", "size_bytes": 123}


def test_await_upload_timeout():
    h = fb.S3FileHelper(_FakeS3(exists_after=9999), "b", "https://m", presign_s3=_client())
    with pytest.raises(TimeoutError):
        asyncio.run(h.await_upload("k", timeout=0.05, interval=0.01))
