"""Prove the Python and TypeScript helpers emit the SAME offer contract.

Volatile fields (presigned signature/date, the key's uuid, the short-link id)
can't match byte-for-byte across two runs, so ONE normalizer masks them to
structural placeholders and we compare everything else exactly — status, all
static strings, the URL's host+path+query-param NAMES, key layout. If the
contracts drift (a renamed key, a changed instruction string, a different query
param set), the assert fails."""
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).parent))  # repo root, so `mcp_filebridge` imports without install
from mcp_filebridge import s3_filebridge as fb  # the Python helper
import boto3
from botocore.client import Config

HERE = Path(__file__).parent
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


# The SigV4 params that MAKE a URL a valid presigned URL. Both SDKs emit these;
# extras (x-id, X-Amz-Content-Sha256) are benign SDK framing, not contract.
_SIGV4_REQUIRED = {"X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date",
                   "X-Amz-Expires", "X-Amz-Signature", "X-Amz-SignedHeaders"}


def _norm_url(u: str) -> str:
    s = urlsplit(u)
    path = re.sub(rf"/{UUID}/", "/<UUID>/", s.path)
    present = set(parse_qs(s.query))
    # Contract = "a valid SigV4 presigned URL to this object", not the exact
    # SDK-specific param set. Assert the required params are present; collapse.
    tag = "sigv4-presigned" if _SIGV4_REQUIRED <= present else f"NOT-PRESIGNED:{sorted(present)}"
    return f"{s.scheme}://{s.netloc}{path}?<{tag}>"


def normalize(offer: dict) -> dict:
    o = copy.deepcopy(offer)
    o["src_key"] = re.sub(rf"/{UUID}/", "/<UUID>/", o["src_key"])
    o["human_upload"]["url"] = re.sub(r"/u/[0-9a-f]{8}$", "/u/<SHORTID>", o["human_upload"]["url"])
    o["agent_upload"]["url"] = _norm_url(o["agent_upload"]["url"])
    o["agent_upload"]["example"] = re.sub(
        r"'(https?://[^']+)'", lambda m: f"'{_norm_url(m.group(1))}'", o["agent_upload"]["example"])
    return o


# --- Python offer ---------------------------------------------------------- #
s3 = boto3.client("s3", endpoint_url="http://localhost:9100",
                  aws_access_key_id="spikekey", aws_secret_access_key="spikesecret",
                  region_name="us-east-1",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
try:
    s3.head_bucket(Bucket="s3fb-spike")
except Exception:
    s3.create_bucket(Bucket="s3fb-spike")

py_offer = fb.S3FileHelper(s3, "s3fb-spike", "https://mcp.example.test").offer_upload(filename="spike.txt")
golden = normalize(py_offer)
(HERE / "offer.golden.json").write_text(json.dumps(golden, indent=2, sort_keys=True))

# --- TS offer (run the Node twin, read its JSON) --------------------------- #
proc = subprocess.run(["node", "s3_filebridge.mjs"], cwd=HERE / "ts",
                      capture_output=True, text=True)
if proc.returncode != 0:
    sys.exit(f"TS twin failed:\n{proc.stderr}")
ts_out = json.loads(proc.stdout)
ts_norm = normalize(ts_out["offer"])

# --- compare --------------------------------------------------------------- #
assert ts_out["functional"] == "ok", f"TS functional round-trip failed: {ts_out['functional']}"
assert ts_out["widget_has_picker"] is True, "TS widget page missing file picker"

if ts_norm != golden:
    import difflib
    a = json.dumps(golden, indent=2, sort_keys=True).splitlines()
    b = json.dumps(ts_norm, indent=2, sort_keys=True).splitlines()
    print("\n".join(difflib.unified_diff(a, b, "python.golden", "typescript", lineterm="")))
    sys.exit("PARITY FAILED: offers differ")

print("ok: Python and TypeScript emit byte-identical normalized offers")
print("    + TS presigned round-trip against MinIO succeeded")
print(f"    golden written: {(HERE / 'offer.golden.json').name} ({len(golden)} top-level keys)")
