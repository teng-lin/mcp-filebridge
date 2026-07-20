"""Cross-language parity: the md_key derivation and HMAC ticket must be byte-compatible between
the JS backend (_convert.mjs, which mints) and the Python gateway (_convert.py, which verifies).
A drift here silently breaks the widget→chat reuse or the convert auth, so pin it with a test that
actually shells out to node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mcp_filebridge import convert as cv

TS_DIR = Path(__file__).resolve().parents[2] / "ts"
SRC_KEYS = ["src/abc-123/report.docx", "src/u/a.b.pdf", "src/u/noext"]


@pytest.fixture(autouse=True)
def _fixed_key(monkeypatch):
    monkeypatch.setenv("MCP_UPLOAD_SIGNING_KEY", "parity-key")
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)


def _node(js: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    out = subprocess.run([node, "--input-type=module", "-e", js], cwd=TS_DIR,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_md_key_derivation_matches():
    js = ('import {mdKeyFor} from "./convert.mjs";'
          f'console.log(JSON.stringify({json.dumps(SRC_KEYS)}.map(mdKeyFor)));')
    ts_keys = json.loads(_node(js))
    py_keys = [cv.md_key_for(s) for s in SRC_KEYS]
    assert ts_keys == py_keys


def test_ticket_minted_in_js_verifies_in_python():
    token = _node('import {mintTicket} from "./convert.mjs";'
                  'console.log(mintTicket("src/u/f.pdf","parity-key",{nowSec:1000,ttl:300}));')
    p = cv.ticket_payload(token, now_sec=1100)
    assert p and p["k"] == "src/u/f.pdf" and p["exp"] == 1300


def test_ticket_minted_in_python_verifies_in_js():
    token = cv.mint_ticket("src/u/f.pdf", now_sec=1000, ttl=300)
    out = _node('import {verifyTicket} from "./convert.mjs";'
                f'const p=verifyTicket({json.dumps(token)},"parity-key",{{nowSec:1100}});'
                'console.log(p?JSON.stringify(p):"null");')
    assert out != "null"
    p = json.loads(out)
    assert p["k"] == "src/u/f.pdf" and p["exp"] == 1300


def test_widget_domain_matches():
    from mcp_filebridge import gates
    bases = ["https://markdownify.hantekllc.com", "https://x.example/", "http://localhost:8090"]
    js = ('import {widgetDomain} from "./widget_gates.mjs";'
          f'console.log(JSON.stringify({json.dumps(bases)}.map(widgetDomain)));')
    assert json.loads(_node(js)) == [gates.widget_domain(b) for b in bases]


def test_js_ticket_with_wrong_key_fails_in_python():
    token = _node('import {mintTicket} from "./convert.mjs";'
                  'console.log(mintTicket("src/u/f.pdf","OTHER-key",{nowSec:1000,ttl:300}));')
    assert cv.ticket_payload(token, now_sec=1100) is None
