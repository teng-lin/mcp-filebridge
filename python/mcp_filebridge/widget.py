"""In-app MCP-Apps upload widget for ConvertX.

Renders an <input type=file> inline in an MCP-Apps host's sandboxed iframe
(claude.ai / ChatGPT) so the user picks a file and uploads it WITHOUT leaving the
chat — the widget PUTs the bytes directly to the S3 presigned URL. The signed
`/u/<shortid>` link stays the portable fallback.

Adapted from notebooklm-py's mcp/_uploadwidget.py. The two host render-gates
(the claude.ai `<sha256>.claudemcpcontent.com` domain and the flat
`_meta["ui/resourceUri"]`) come from that proven implementation; the difference
here is the widget PUTs to an S3 presigned URL on a DIFFERENT origin, so the CSP
`connect_domains` must name the bucket's public endpoint, not the MCP server.
"""
from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP

from mcp_filebridge.gates import widget_domain as _widget_domain  # shared render gate (parity-tested)

# Shared MCP-Apps host bridge — bundled as package data (so the wheel is self-contained) and
# single-sourced with the markdownify widget. Read via importlib.resources (works installed or editable).
from importlib.resources import files as _pkg_files  # noqa: E402
_BRIDGE = (_pkg_files("mcp_filebridge") / "widget_bridge.js").read_text().replace("__CLIENT_NAME__", "convertx-upload")

_WIDGET_URI = "ui://convertx/upload-v1"
_WIDGET_FLAG = "MCP_UPLOAD_WIDGET"  # default on; set to "0" to disable


# Single-file picker → direct PUT to the S3 presigned URL. Reads the tool result
# from the postMessage bridge (claude.ai) or window.openai.toolOutput (ChatGPT).
_WIDGET_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<style>
 body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:14px;background:transparent;color:#1c2420}
 .card{border:1px solid #dde2da;border-radius:10px;padding:16px;max-width:520px;background:#fff}
 .head{font-size:14px;font-weight:650;color:#2f6df7}
 input[type=file]{display:block;margin:12px 0;font-size:15px}
 button{font-size:15px;padding:9px 16px;border-radius:8px;border:0;background:#2f6df7;color:#fff}
 button[disabled]{opacity:.5}
 progress{display:block;width:100%;margin-top:10px;height:8px}
 #out{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;margin-top:12px;color:#4a564e}
 @media(prefers-color-scheme:dark){body{color:#e6eae4}.card{background:#1d231f;border-color:#313a33}#out{color:#b7c0b8}}
</style></head><body>
<div class="card">
 <div class="head">📎 Upload the file to convert</div>
 <div id="sub" style="font-size:12px;color:#6b7a6e;margin-top:3px">starting…</div>
 <input id="f" type="file" disabled>
 <button id="up" disabled>Upload</button>
 <progress id="pg" value="0" max="100" style="display:none"></progress>
 <div id="out"></div>
</div>
<script type="module">
 const sub=document.getElementById('sub'),out=document.getElementById('out'),fi=document.getElementById('f'),btn=document.getElementById('up'),pg=document.getElementById('pg');
 const log=m=>{out.textContent+=(out.textContent?"\\n":"")+m;window.wsize();};
 let uploadUrl=null, srcKey=null, target=null, cvSeq=0;
 const CONVERT_TOOL="convert";   // hard allowlist: a spoofed postMessage can't redirect which tool runs
 const geturl=o=>o&&o.upload_url||null;
 window.onReady=h=>{sub.textContent=h+" · ready";};
 // The bridge (below) hands us each tool-result payload; we don't allowlist ev.origin because the
 // only thing a message can set is uploadUrl, and the CSP connect-src pins uploads to the bucket +
 // the presigned URL is a server-signed single-use target — a spoofed URL can't exfiltrate or land.
 window.onToolResult=p=>{ if(p.toolResult)p=p.toolResult; let d=p.structuredContent;
   if(!geturl(d)&&Array.isArray(p.content))for(const c of p.content)if(c&&c.type==="text"){try{const j=JSON.parse(c.text);if(geturl(j))d=j}catch(e){}}
   if(!geturl(d)&&geturl(p))d=p; const u=geturl(d);
   if(u&&!uploadUrl){uploadUrl=u;srcKey=d.src_key;target=d.target;window.__gotResult=true;fi.disabled=false;sub.textContent="pick the file to upload";} };
 //__BRIDGE__
 // XHR upload so we can drive a progress bar (fetch can't report upload progress) — nlm-py #1950.
 function putFile(url,file,onp){return new Promise((res,rej)=>{
   const x=new XMLHttpRequest(); x.open("PUT",url);
   x.upload.onprogress=e=>{if(e.lengthComputable&&onp)onp(Math.round(e.loaded/e.total*100));};
   x.onload=()=>res({ok:x.status>=200&&x.status<300,status:x.status});
   x.onerror=()=>rej(new Error("network")); x.onabort=()=>rej(new Error("abort")); x.send(file);});}
 // Auto-convert after upload — nlm-py #1948. ChatGPT: window.openai.callTool returns the result
 // (so we can show a link). claude.ai/MCP-Apps: a tools/call postMessage (fire-and-forget → the
 // download link reaches the MODEL, which shows it in chat). Hard-allowlist CONVERT_TOOL; the args
 // are our own just-minted src_key/target, so a spoofed message can't redirect the tool or its input.
 function autoConvert(fname){ if(!srcKey)return null;
   const args={src_key:srcKey,target:target||"",filename:fname||""};  // pass the REAL picked name → output keeps its stem
   if(oai&&typeof oai.callTool==="function"){try{return oai.callTool(CONVERT_TOOL,args);}catch(e){return null;}}
   window.wpost({jsonrpc:"2.0",id:"cv"+(++cvSeq),method:"tools/call",params:{name:CONVERT_TOOL,arguments:args}}); return null;}
 fi.addEventListener('change',()=>{btn.disabled=!(fi.files&&fi.files.length);});
 btn.addEventListener('click',async()=>{
   const file=fi.files&&fi.files[0]; if(!file||!uploadUrl){log("no file selected yet");return;}
   if(file.size>200*1024*1024){log("❌ exceeds 200 MB");return;}
   btn.disabled=true; fi.disabled=true; pg.style.display="block"; pg.value=0;
   try{
     const res=await putFile(uploadUrl,file,pct=>{pg.value=pct;sub.textContent="uploading "+file.name+" — "+pct+"%";});
     pg.style.display="none";
     if(!res.ok){log("["+res.status+"] upload failed");btn.disabled=false;fi.disabled=false;return;}
     log("uploaded ✓");
     const p=autoConvert(file.name);   // the real filename the user picked → output keeps its stem
     if(p){ sub.textContent="converting…";   // ChatGPT: result comes back → show a download link
       try{ const r=await p; const o=(r&&(r.structuredContent||r.toolResult||r))||{};
         sub.textContent="✅ converted";
         if(o.url){const a=document.createElement("a");a.href=o.url;a.target="_blank";
           a.textContent="⬇ Download "+(o.filename||"result");
           a.style.cssText="display:block;margin-top:10px;font-weight:600;color:#2f6df7";
           document.querySelector(".card").appendChild(a);window.wsize();}
       }catch(e){sub.textContent="✅ uploaded — ask me to convert it";}
     } else { sub.textContent="✅ uploaded — converting…"; }   // claude.ai: convert runs; link appears in chat
   }catch(e){pg.style.display="none";log("❌ upload failed (CSP/CORS/network): "+e);btn.disabled=false;fi.disabled=false;}
 });
</script></body></html>"""
_WIDGET_HTML = _WIDGET_HTML.replace("//__BRIDGE__", _BRIDGE)


def register_upload_widget(mcp: FastMCP, files, s3_public_endpoint: str, public_base_url: str,
                           mint_upload, validate_target=None) -> None:
    """Register the inline upload widget + its tool. No-op if MCP_UPLOAD_WIDGET=0.

    ``mint_upload(filename)`` returns the offer dict (agent PUT url + human link +
    src_key) — wired to S3FileHelper.offer_upload. ``s3_public_endpoint`` is where
    the widget PUTs (the CSP connect domain). ``validate_target(source_ext, target)``
    (optional) raises if the conversion is unsupported — checked up front so the user
    is told BEFORE uploading rather than after."""
    if os.environ.get(_WIDGET_FLAG) == "0":
        return

    origin = s3_public_endpoint.rstrip("/")
    # The widget only fetches the S3 bucket, but include the connector origin too
    # (as notebooklm-py does) in case the host's iframe init expects its own origin
    # in connect-src. Order: connector origin first, then the bucket.
    connect = [public_base_url.rstrip("/"), origin]
    if origin == public_base_url.rstrip("/"):
        connect = [origin]

    @mcp.resource(
        _WIDGET_URI,
        meta={"openai/widgetCSP": {"connect_domains": connect, "resource_domains": []}},
        app=AppConfig(
            domain=_widget_domain(public_base_url),           # claude.ai render gate
            csp=ResourceCSP(connect_domains=connect),         # widget → S3 bucket (+ connector origin)
            prefers_border=True,
        ),
    )
    def _upload_widget_html() -> str:
        return _WIDGET_HTML

    @mcp.tool(
        meta={"ui/resourceUri": _WIDGET_URI, "openai/outputTemplate": _WIDGET_URI},
        app=AppConfig(resource_uri=_WIDGET_URI, visibility=["model"]),
    )
    def upload_file(filename: str, target: str = "") -> dict[str, Any]:
        """STEP 1 of converting a file: get the source file from the user. Shows an inline
        file picker (or, if the host can't render it, surface `upload_link` to the user).
        `filename` is the source file's name WITH extension (e.g. "MyBook.epub", "report.docx")
        — it sets both the input type and the output name (MyBook.epub → MyBook.mobi).
        IMMEDIATELY after calling this, call `convert(src_key, target)` in the SAME turn — do
        NOT wait for another user message and do NOT ask the user to confirm. convert BLOCKS
        until the user finishes uploading and then converts automatically, so the user only
        has to pick the file. Do NOT ask them to paste the file, and do NOT pass a tool.
        Returns `src_key` (pass to convert), `upload_url` (the widget uses it), and
        `upload_link` (a click-to-upload fallback for the user)."""
        # Reject an unsupported target up front (before the user wastes time uploading).
        if validate_target and target and "." in filename:
            validate_target(filename.rsplit(".", 1)[-1].lower(), target)  # raises with the valid targets
        offer = mint_upload(filename)
        return {
            "src_key": offer["src_key"],
            "upload_url": offer["agent_upload"]["url"],   # the widget PUTs here
            "upload_link": offer["human_upload"]["url"],  # fallback if the widget doesn't render
            "target": target,
        }
