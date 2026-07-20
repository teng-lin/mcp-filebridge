// markdownify TS MCP server (backend) — runs over stdio; the OAuth gateway proxies to it.
// Tools: upload_file (filebridge upload widget/link) → to_markdown (stage + markitdown → markdown,
// inline when small, presigned .md when large). Uses ts/s3_filebridge.mjs. Does NO auth — the
// gateway in front owns OAuth.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import {
  PutObjectCommand, GetObjectCommand, HeadObjectCommand, CreateBucketCommand,
} from "@aws-sdk/client-s3";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema, CallToolRequestSchema,
  ListResourcesRequestSchema, ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { S3FileHelper, makeS3Client } from "../../ts/s3_filebridge.mjs";
import { mdKeyFor, mintTicket } from "../../ts/convert.mjs";
import { widgetDomain, resourceMeta, toolMeta } from "../../ts/widget_gates.mjs";

const execFileP = promisify(execFile);
const env = process.env;
const BUCKET = env.S3_BUCKET || "markdownify";
const MARKITDOWN = env.MARKITDOWN_BIN || "markitdown";
const PREVIEW_BYTES = Number(env.MD_PREVIEW_BYTES || 6000);  // markdown chars put into chat by default (~1.5K tokens)
const FULL_CAP = Number(env.MD_FULL_CAP || 60_000);          // full=true inlines only up to ~15K tokens; bigger stays preview+link
const s3creds = { accessKeyId: env.S3_ACCESS_KEY || "spikekey", secretAccessKey: env.S3_SECRET_KEY || "spikesecret" };
const s3 = makeS3Client(env.S3_ENDPOINT || "http://localhost:9100", s3creds);
const presign = env.S3_PUBLIC_ENDPOINT && env.S3_PUBLIC_ENDPOINT !== env.S3_ENDPOINT
  ? makeS3Client(env.S3_PUBLIC_ENDPOINT, s3creds) : s3;
const files = new S3FileHelper(s3, BUCKET, env.PUBLIC_BASE_URL || "http://localhost:8080", { presignS3: presign });

// --- MCP-Apps inline upload widget (renders a file picker in claude.ai/ChatGPT) ---
// The @modelcontextprotocol/sdk has no app= helper, so we attach the _meta gates by hand:
// a text/html;profile=mcp-app resource + ui/openai _meta on upload_file. HTML is language-neutral.
const WIDGET_URI = "ui://markdownify/upload-v1";
const PUBLIC_BASE = (env.PUBLIC_BASE_URL || "http://localhost:8080").replace(/\/+$/, "");
const S3_PUBLIC = (env.S3_PUBLIC_ENDPOINT || env.S3_ENDPOINT || "http://localhost:9100").replace(/\/+$/, "");
// mdKeyFor / mintTicket (ts/convert.mjs) + the render gates (ts/widget_gates.mjs) are byte-compatible
// with their Python twins (mcp_filebridge/convert.py, mcp_filebridge/gates.py). The HMAC key is shared
// with the gateway via env (it spawns this backend with the same os.environ).
const SIGN_KEY = env.MCP_UPLOAD_SIGNING_KEY || env.MCP_BEARER_TOKEN || "dev-key";
const mintConvertUrl = (srcKey) => `${PUBLIC_BASE}/u/convert/${mintTicket(srcKey, SIGN_KEY)}`;
const WIDGET_META = resourceMeta(widgetDomain(PUBLIC_BASE), [PUBLIC_BASE, S3_PUBLIC]);
const WIDGET_RESOURCE = { uri: WIDGET_URI, name: "markdownify upload", mimeType: "text/html;profile=mcp-app", _meta: WIDGET_META };
const TOOL_META = toolMeta(WIDGET_URI);
// Shared MCP-Apps host bridge (single-sourced with the convertx widget in mcp_filebridge/widget_bridge.js).
const BRIDGE_JS = fs.readFileSync(new URL("../../mcp_filebridge/widget_bridge.js", import.meta.url), "utf8")
  .replaceAll("__CLIENT_NAME__", "mdfy-upload");
const WIDGET_HTML = `<!doctype html>
<html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta name=color-scheme content="light dark">
<style>body{font-family:system-ui,sans-serif;margin:0;padding:14px;background:transparent;color:#1c2420}
.card{border:1px solid #dde2da;border-radius:10px;padding:16px;max-width:560px;background:#fff}
.head{font-size:14px;font-weight:650;color:#2f6df7}input[type=file]{display:block;margin:12px 0;font-size:15px}
button{font-size:15px;padding:9px 16px;border-radius:8px;border:0;background:#2f6df7;color:#fff}button[disabled]{opacity:.5}
.mini{font-size:13px;padding:5px 10px;background:#eef2fb;color:#2f6df7}
progress{display:block;width:100%;margin-top:10px;height:8px}#out{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:12px;margin-top:10px;color:#4a564e}
#md{display:none;width:100%;height:220px;margin-top:12px;font-family:ui-monospace,monospace;font-size:12px;border:1px solid #dde2da;border-radius:8px;padding:8px;box-sizing:border-box;background:#fafbfa;color:#1c2420}
#actions{display:none;margin-top:8px}#lk{width:100%;font-size:11px;padding:5px;margin-top:3px;box-sizing:border-box;font-family:ui-monospace,monospace}#cl{margin-top:4px}
@media(prefers-color-scheme:dark){body{color:#e6eae4}.card{background:#1d231f;border-color:#313a33}#out{color:#b7c0b8}.mini{background:#243043}#md{background:#151a16;border-color:#313a33;color:#e6eae4}}</style></head>
<body><div class=card><div class=head>📄 File → Markdown</div>
<div id=sub style="font-size:12px;color:#6b7a6e;margin-top:3px">starting…</div>
<input id=f type=file disabled><button id=up disabled>Convert</button>
<progress id=pg value=0 max=100 style="display:none"></progress>
<div id=out></div>
<textarea id=md readonly></textarea>
<div id=actions><button id=cp class=mini>Copy Markdown</button>
<div id=lkrow><div style="margin-top:8px;font-size:12px;color:#6b7a6e">To save a file: ask me for the download link in the chat (it's clickable there), or copy this link and open it in a browser tab — claude.ai blocks downloads inside the widget itself.</div>
<input id=lk readonly onclick="this.select()"><button id=cl class=mini>Copy link</button></div></div></div>
<script type=module>
const $=i=>document.getElementById(i);
const sub=$('sub'),out=$('out'),fi=$('f'),btn=$('up'),pg=$('pg'),mdEl=$('md'),actions=$('actions');
const getconv=o=>o&&o.convert_url||null; let convUrl=null;   // the widget POSTs bytes to convert_url; server converts on receipt
window.onReady=h=>{sub.textContent=h+" · ready";};
window.onToolResult=p=>{if(p.toolResult)p=p.toolResult;let d=p.structuredContent;
 if(!getconv(d)&&Array.isArray(p.content))for(const c of p.content)if(c&&c.type==="text"){try{const j=JSON.parse(c.text);if(getconv(j))d=j}catch(e){}}
 if(!getconv(d)&&getconv(p))d=p;const u=getconv(d);
 if(u&&!convUrl){convUrl=u;window.__gotResult=true;fi.disabled=false;sub.textContent="pick a file to convert to Markdown";}};
${BRIDGE_JS}
fi.addEventListener('change',()=>{btn.disabled=!(fi.files&&fi.files.length);});
btn.addEventListener('click',()=>{const file=fi.files&&fi.files[0];if(!file||!convUrl){return;}
 btn.disabled=true;fi.disabled=true;pg.style.display="block";pg.value=0;out.textContent="";mdEl.style.display="none";actions.style.display="none";
 const x=new XMLHttpRequest();
 x.open("POST",convUrl+"?filename="+encodeURIComponent(file.name));
 x.setRequestHeader("Content-Type",file.type||"application/octet-stream");
 x.upload.onprogress=e=>{if(e.lengthComputable){pg.value=Math.round(e.loaded/e.total*100);sub.textContent="uploading "+pg.value+"% — converting on the server…";}};
 x.onload=()=>{pg.style.display="none";
  if(x.status>=200&&x.status<300){let j={};try{j=JSON.parse(x.responseText)}catch(e){}
   const md=(j.markdown!==undefined)?j.markdown:x.responseText;
   mdEl.value=md;mdEl.style.display="block";actions.style.display="block";
   sub.textContent="✅ converted "+file.name+" ("+md.length+" chars)";
   if(j.download_url){$('lk').value=j.download_url;$('lkrow').style.display="";}
   else{$('lkrow').style.display="none";}   // no server URL → Copy Markdown only
   window.wsize();}
  else{sub.textContent="conversion failed";out.textContent="["+x.status+"] "+x.responseText.slice(0,240);btn.disabled=false;fi.disabled=false;}};
 x.onerror=()=>{pg.style.display="none";sub.textContent="";out.textContent="❌ network/CORS error reaching the server";btn.disabled=false;fi.disabled=false;};
 x.send(file);});
$('cp').addEventListener('click',()=>{mdEl.select();try{document.execCommand('copy')}catch(e){}$('cp').textContent="Copied ✓";setTimeout(()=>$('cp').textContent="Copy Markdown",1200);});
$('cl').addEventListener('click',()=>{const l=$('lk');l.select();try{document.execCommand('copy')}catch(e){}$('cl').textContent="Copied ✓";setTimeout(()=>$('cl').textContent="Copy link",1200);});
</script></body></html>`;

async function ensureBucket() { try { await s3.send(new CreateBucketCommand({ Bucket: BUCKET })); } catch {} }
async function getBytes(key) {
  const r = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
  return Buffer.from(await r.Body.transformToByteArray());
}
async function waitForKey(key, timeoutS = 55) {
  for (let i = 0; i < timeoutS * 2; i++) {
    try { await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: key })); return true; }
    catch { await new Promise((r) => setTimeout(r, 500)); }
  }
  return false;
}

async function keyExists(key) {
  try { await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: key })); return true; } catch { return false; }
}

// Context-safe: returns a PREVIEW + a download link + a paging handle (md_key) by DEFAULT — never the
// whole document, which for a big file would flood the model's context on every subsequent turn. Reuses
// the widget's already-converted .md (keyed off src_key) so a file is never converted twice.
async function toMarkdown(srcKey, filename, full = false) {
  const mdKey = mdKeyFor(srcKey);
  let md;
  if (await keyExists(mdKey)) {
    md = (await getBytes(mdKey)).toString("utf-8");           // widget already converted this file
  } else {
    if (!(await waitForKey(srcKey))) throw new Error(`upload for '${path.basename(srcKey)}' not received yet — call to_markdown again once the file is uploaded.`);
    const name = path.basename(filename || srcKey);
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mdfy-"));
    const local = path.join(dir, name);
    fs.writeFileSync(local, await getBytes(srcKey));
    md = (await execFileP(MARKITDOWN, [local], { maxBuffer: 64 * 1024 * 1024 })).stdout;
    fs.rmSync(dir, { recursive: true, force: true });
    await s3.send(new PutObjectCommand({ Bucket: BUCKET, Key: mdKey, Body: md, ContentType: "text/markdown" }));
  }
  const bytes = Buffer.byteLength(md, "utf-8");
  const dl = await files.offerDownload({ key: mdKey, filename: path.basename(mdKey), mime: "text/markdown" });
  if (full && bytes <= FULL_CAP) {
    return { content: [{ type: "text", text: md }], structuredContent: { md_key: mdKey, bytes, truncated: false, download_url: dl.url } };
  }
  const preview = md.slice(0, PREVIEW_BYTES);
  const truncated = preview.length < md.length;
  const kTok = Math.max(1, Math.round(bytes / 4000));
  // Present the download as a markdown link so it renders CLICKABLE in the chat (chat links
  // aren't sandboxed like the widget iframe, so this is the download path that actually works).
  const dlLink = `[⬇ Download ${path.basename(mdKey)}](${dl.url})`;
  const footer = truncated
    ? `\n\n---\n${dlLink}  ·  preview: first ${preview.length} of ${bytes} bytes (~${kTok}K tokens). Page more with read_markdown(md_key="${mdKey}", offset=${preview.length}); or to_markdown(..., full=true) if it's small.`
    : `\n\n---\n${dlLink}  ·  complete (${bytes} bytes).`;
  return { content: [{ type: "text", text: preview + footer }], structuredContent: { md_key: mdKey, bytes, truncated, preview_bytes: preview.length, download_url: dl.url } };
}

async function readMarkdown(mdKey, offset = 0, limit = 8000) {
  const head = await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: mdKey }));  // throws if the key is unknown
  const total = head.ContentLength;
  offset = Math.max(0, offset | 0);
  const end = Math.min(offset + Math.max(1, limit | 0), total);
  if (offset >= total) return { content: [{ type: "text", text: "" }], structuredContent: { offset, returned: 0, total, next_offset: null } };
  const r = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: mdKey, Range: `bytes=${offset}-${end - 1}` }));
  const buf = Buffer.from(await r.Body.transformToByteArray());
  return { content: [{ type: "text", text: buf.toString("utf-8") }], structuredContent: { offset, returned: buf.length, total, next_offset: end < total ? end : null } };
}

const TOOLS = [
  { name: "upload_file",
    description: "STEP 1: get the file the user wants as Markdown. Shows an inline upload widget and returns an upload target (src_key + presigned URL + link). The widget converts on upload and shows the result there. To also bring the Markdown INTO THE CHAT, call to_markdown(src_key) next — it reuses the widget's conversion (or converts an agent PUT) and returns a context-safe preview + paging handle.",
    inputSchema: { type: "object", properties: { filename: { type: "string", description: "source filename with extension, e.g. report.pdf" } }, required: ["filename"] },
    _meta: TOOL_META },
  { name: "to_markdown",
    description: "STEP 2: convert the uploaded file (src_key) to Markdown via markitdown (PDF/PPTX/XLSX/DOCX/image/audio…). Blocks until the upload lands. Returns a PREVIEW + a download link + a paging handle (md_key) — NOT the whole document, to protect the context window. Pass full=true to inline a small file; use read_markdown to page a big one.",
    inputSchema: { type: "object", properties: { src_key: { type: "string" }, filename: { type: "string" }, full: { type: "boolean", description: "inline the entire markdown (small files only; capped)" } }, required: ["src_key"] } },
  { name: "read_markdown",
    description: "Read a byte slice of an already-converted document by md_key (returned by to_markdown). Returns bytes [offset, offset+limit) plus next_offset, so you can page through large markdown without loading it all into context.",
    inputSchema: { type: "object", properties: { md_key: { type: "string" }, offset: { type: "integer", description: "byte offset (default 0)" }, limit: { type: "integer", description: "max bytes to return (default 8000)" } }, required: ["md_key"] } },
];

const server = new Server({ name: "markdownify-filebridge", version: "0.1.0" }, { capabilities: { tools: {}, resources: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
server.setRequestHandler(ListResourcesRequestSchema, async () => ({ resources: [WIDGET_RESOURCE] }));
server.setRequestHandler(ReadResourceRequestSchema, async (req) => {
  if (req.params.uri !== WIDGET_URI) throw new Error(`unknown resource: ${req.params.uri}`);
  return { contents: [{ uri: WIDGET_URI, mimeType: "text/html;profile=mcp-app", text: WIDGET_HTML, _meta: WIDGET_META }] };
});
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  await ensureBucket();
  const { name, arguments: a = {} } = req.params;
  if (name === "upload_file") {
    const offer = await files.offerUpload({ filename: a.filename });
    // Flatten to the widget's contract: it reads upload_url + src_key + filename.
    const sc = { ...offer, upload_url: offer.agent_upload?.url, upload_link: offer.human_upload?.url,
      convert_url: mintConvertUrl(offer.src_key), filename: a.filename };
    return { content: [{ type: "text", text: `Upload ${a.filename} in the widget above (or ${sc.upload_link}); then call to_markdown(src_key="${offer.src_key}") to bring the Markdown into the chat.` }], structuredContent: sc, _meta: TOOL_META };
  }
  if (name === "to_markdown") return await toMarkdown(a.src_key, a.filename, a.full === true);
  if (name === "read_markdown") return await readMarkdown(a.md_key, a.offset || 0, a.limit || 8000);
  throw new Error(`unknown tool: ${name}`);
});

await server.connect(new StdioServerTransport());
process.stderr.write("markdownify-filebridge backend: stdio ready\n");
