"""ConvertX MCP spike v2 — now built on the reusable S3FileHelper.

Same architecture as v1 (wrap the unmodified ConvertX container; S3 presigned
URLs as the host side-channel) but the presigning + offer contract now come from
`s3_filebridge.S3FileHelper` instead of inline boto3. `request_upload` returns the
full filebridge-style `upload_required` offer (human widget + agent curl paths),
and `convert` returns a `download_ready` offer. ConvertX stays untouched.

Self-check (needs the local MinIO + ConvertX containers):
    python3 convertx_mcp_v2.py
"""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass

import boto3
import requests
from botocore.client import Config

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from s3_filebridge import S3FileHelper

# ---- config -------------------------------------------------------------- #
S3_ENDPOINT, S3_KEY, S3_SECRET, BUCKET = "http://localhost:9100", "spikekey", "spikesecret", "cx-mcp-spike"
WIDGET_BASE = "https://mcp.example.test"      # where /u/<shortid> is served in prod
CONVERTX, CX_EMAIL, CX_PW = "http://localhost:3300", "spike2@test.local", "spikepass123"

s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT,
                  aws_access_key_id=S3_KEY, aws_secret_access_key=S3_SECRET,
                  region_name="us-east-1",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
try:
    s3.head_bucket(Bucket=BUCKET)
except Exception:
    s3.create_bucket(Bucket=BUCKET)

files = S3FileHelper(s3, BUCKET, WIDGET_BASE)   # <- the reusable helper


# ---- ConvertX: the validated unofficial contract (unchanged from v1) ------ #
@dataclass
class ConvertXResult:
    filename: str
    content: bytes


class ConvertX:
    def __init__(self, base: str, email: str, pw: str) -> None:
        self._b, self._email, self._pw = base, email, pw

    def convert(self, filename: str, data: bytes, target: str, tool: str) -> ConvertXResult:
        s = requests.Session()
        r = s.post(f"{self._b}/login", data={"email": self._email, "password": self._pw},
                   allow_redirects=False)
        auth = re.search(r"auth=([^;]+)", r.headers["set-cookie"]).group(1)
        uid = json.loads(base64.urlsafe_b64decode(auth.split(".")[1] + "==="))["id"]
        r = s.get(f"{self._b}/", headers={"Cookie": f"auth={auth}"}, allow_redirects=False)
        job = re.search(r"jobId=([^;]+)", r.headers["set-cookie"]).group(1)
        s.headers["Cookie"] = f"auth={auth}; jobId={job}"
        s.post(f"{self._b}/upload", files={"file": (filename, data)})
        s.post(f"{self._b}/convert", data={"convert_to": f"{target},{tool}",
               "file_names": json.dumps([filename])})
        stem = filename.rsplit(".", 1)[0]
        href = None
        for _ in range(60):
            r = s.post(f"{self._b}/progress/{job}")
            m = re.search(rf'href="(/download/[^"]+\.{re.escape(target)})"', r.text)
            if m:
                href = m.group(1)
                break
            time.sleep(0.5)
        if not href:
            raise RuntimeError(f"ConvertX conversion to .{target} did not complete")
        return ConvertXResult(filename=f"{stem}.{target}", content=s.get(f"{self._b}{href}").content)


cx = ConvertX(CONVERTX, CX_EMAIL, CX_PW)


# ---- MCP tools ----------------------------------------------------------- #
def request_upload(filename: str) -> dict:
    """Full filebridge-style upload offer (human widget + agent PUT paths)."""
    return files.offer_upload(filename=filename)


def convert(src_key: str, target: str, tool: str = "pandoc") -> dict:
    """Pull source from S3 -> ConvertX -> push result -> download_ready offer."""
    src = s3.get_object(Bucket=BUCKET, Key=src_key)["Body"].read()
    result = cx.convert(src_key.split("/")[-1], src, target, tool)
    out_key = f"out/{uuid.uuid4()}/{result.filename}"
    s3.put_object(Bucket=BUCKET, Key=out_key, Body=result.content)
    return files.offer_download(key=out_key, filename=result.filename,
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def build_server():
    from fastmcp import FastMCP
    mcp = FastMCP(name="convertx-filebridge")
    mcp.tool(request_upload)
    mcp.tool(convert)
    return mcp


# ---- self-check: full loop through the helper's offers ------------------- #
if __name__ == "__main__":
    up = request_upload("spike.md")
    assert up["status"] == "upload_required" and {"human_upload", "agent_upload"} <= up.keys()
    # agent path: PUT the source to the offer's presigned URL
    put = requests.put(up["agent_upload"]["url"], data=b"# Spike v2\n\nvia S3FileHelper.\n")
    assert put.status_code == 200, put.status_code

    out = convert(up["src_key"], "docx")
    assert out["status"] == "download_ready" and out["filename"] == "spike.docx", out
    # fetch the result via the download offer's presigned GET
    got = requests.get(out["url"])
    assert got.status_code == 200 and got.content[:4] == b"PK\x03\x04", "result not a valid .docx"

    srv = build_server()
    print(f"ok: request_upload(offer) -> agent PUT -> convert -> download_ready offer -> "
          f"valid .docx ({len(got.content)} bytes); server '{srv.name}' builds")
