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

import hashlib
import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP

_WIDGET_URI = "ui://convertx/upload-v1"
_WIDGET_FLAG = "MCP_UPLOAD_WIDGET"  # default on; set to "0" to disable


def _widget_domain(base_url: str) -> str:
    """claude.ai render gate: sha256("<base>/mcp")[:32] + .claudemcpcontent.com."""
    endpoint = f"{base_url.rstrip('/')}/mcp"
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32] + ".claudemcpcontent.com"


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
 #out{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;margin-top:12px;color:#4a564e}
 @media(prefers-color-scheme:dark){body{color:#e6eae4}.card{background:#1d231f;border-color:#313a33}#out{color:#b7c0b8}}
</style></head><body>
<div class="card">
 <div class="head">📎 Upload the file to convert</div>
 <div id="sub" style="font-size:12px;color:#6b7a6e;margin-top:3px">starting…</div>
 <input id="f" type="file" disabled>
 <button id="up" disabled>Upload</button>
 <div id="out"></div>
</div>
<script type="module">
 const sub=document.getElementById('sub'),out=document.getElementById('out'),fi=document.getElementById('f'),btn=document.getElementById('up');
 const log=m=>{out.textContent+=(out.textContent?"\\n":"")+m;size();};
 const post=m=>{try{window.parent.postMessage(m,"*")}catch(e){}};
 const oai=window.openai;
 let initialized=false, uploadUrl=null;
 const geturl=o=>o&&o.upload_url||null;
 function size(){post({jsonrpc:"2.0",method:"ui/notifications/size-changed",
   params:{height:document.documentElement.scrollHeight,width:document.documentElement.scrollWidth}});}
 function ready(h){if(initialized)return;initialized=true;
   sub.textContent=(h||(oai?"ChatGPT":"host"))+" · ready";
   post({jsonrpc:"2.0",method:"ui/notifications/initialized",params:{}});}   // claude.ai render gate
 post({jsonrpc:"2.0",id:1,method:"ui/initialize",params:{capabilities:{},protocolVersion:"2026-01-26",
   clientInfo:{name:"convertx-upload",version:"1"},appCapabilities:{availableDisplayModes:["inline"]}}});
 setTimeout(()=>ready(oai?"ChatGPT":null),500);
 function consider(p){ if(!p)return; if(p.toolResult)p=p.toolResult;
   let d=p.structuredContent;
   if(!geturl(d)&&Array.isArray(p.content))for(const c of p.content)if(c&&c.type==="text"){
     try{const j=JSON.parse(c.text);if(geturl(j))d=j}catch(e){}}
   if(!geturl(d)&&geturl(p))d=p;
   const u=geturl(d);
   if(u&&!uploadUrl){uploadUrl=u;fi.disabled=false;sub.textContent="pick the file to upload";}
 }
 // claude.ai: tool result via postMessage. We don't allowlist ev.origin: the only thing a message
 // can set is uploadUrl, and the CSP connect-src pins uploads to the bucket + the presigned URL is a
 // server-signed single-use target, so a spoofed URL can't exfiltrate or land anything.
 window.addEventListener("message",ev=>{let d=ev.data;if(d==null)return;
   if(typeof d==="string"){try{d=JSON.parse(d)}catch(e){return}}
   if(d.result&&!d.method){ready(d.result.hostInfo&&d.result.hostInfo.name);
     if(d.result.toolResult)consider(d.result.toolResult);return;}
   if(typeof d.method==="string"){if(d.method.includes("tool"))consider(d.params||{});
     else if(d.id!=null)post({jsonrpc:"2.0",id:d.id,result:{}});}});
 function pullOai(){if(oai&&oai.toolOutput)consider(oai.toolOutput);}
 window.addEventListener("openai:set_globals",pullOai);
 let _pt=0;const _pi=setInterval(()=>{pullOai();if(uploadUrl||++_pt>66)clearInterval(_pi);},300);
 fi.addEventListener('change',()=>{btn.disabled=!(fi.files&&fi.files.length);});
 btn.addEventListener('click',async()=>{
   const file=fi.files&&fi.files[0]; if(!file||!uploadUrl){log("no file selected yet");return;}
   if(file.size>200*1024*1024){log("❌ exceeds 200 MB");return;}
   btn.disabled=true; fi.disabled=true; log("uploading "+file.name+" ("+file.size+" B)…");
   try{
     const res=await fetch(uploadUrl,{method:"PUT",body:file});   // direct PUT to the S3 presigned URL
     if(res.ok){sub.textContent="✅ uploaded — ask me to convert it";log("done ✓");}
     else{log("["+res.status+"] upload failed");btn.disabled=false;fi.disabled=false;}
   }catch(e){log("❌ upload failed (CSP/CORS/network): "+e);btn.disabled=false;fi.disabled=false;}
 });
</script></body></html>"""


def register_upload_widget(mcp: FastMCP, files, s3_public_endpoint: str, public_base_url: str,
                           mint_upload) -> None:
    """Register the inline upload widget + its tool. No-op if MCP_UPLOAD_WIDGET=0.

    ``mint_upload(source_format)`` returns ``(upload_url, src_key)`` — the caller
    wires it to S3FileHelper.offer_upload. ``s3_public_endpoint`` is where the
    widget PUTs (the CSP connect domain)."""
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
        — it sets both the input type and the output name (MyBook.epub → MyBook.mobi). Once
        the user has uploaded, call `convert(src_key, target)` — do NOT ask them to paste the
        file, and do NOT pass a tool (convert picks the right converter automatically).
        Returns `src_key` (pass to convert), `upload_url` (the widget uses it), and
        `upload_link` (a click-to-upload fallback for the user)."""
        offer = mint_upload(filename)
        return {
            "src_key": offer["src_key"],
            "upload_url": offer["agent_upload"]["url"],   # the widget PUTs here
            "upload_link": offer["human_upload"]["url"],  # fallback if the widget doesn't render
            "target": target,
        }
