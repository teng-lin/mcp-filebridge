// markdownify TS MCP server (backend) — runs over stdio; the OAuth gateway proxies to it.
// Tools: upload_file (filebridge upload widget/link) → to_markdown (stage + markitdown → markdown,
// inline when small, presigned .md when large). Uses ts/s3_filebridge.mjs. Does NO auth — the
// gateway in front owns OAuth.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID, createHash, createHmac } from "node:crypto";
import {
  S3Client, PutObjectCommand, GetObjectCommand, HeadObjectCommand, CreateBucketCommand,
} from "@aws-sdk/client-s3";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema, CallToolRequestSchema,
  ListResourcesRequestSchema, ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { S3FileHelper } from "../../ts/s3_filebridge.mjs";

const execFileP = promisify(execFile);
const env = process.env;
const BUCKET = env.S3_BUCKET || "markdownify";
const MARKITDOWN = env.MARKITDOWN_BIN || "markitdown";
const INLINE_CAP = Number(env.MD_INLINE_CAP || 200_000);
const s3cfg = (endpoint) => ({
  endpoint, region: "us-east-1", forcePathStyle: true,
  credentials: { accessKeyId: env.S3_ACCESS_KEY || "spikekey", secretAccessKey: env.S3_SECRET_KEY || "spikesecret" },
  requestChecksumCalculation: "WHEN_REQUIRED", responseChecksumValidation: "WHEN_REQUIRED",
});
const s3 = new S3Client(s3cfg(env.S3_ENDPOINT || "http://localhost:9100"));
const presign = env.S3_PUBLIC_ENDPOINT && env.S3_PUBLIC_ENDPOINT !== env.S3_ENDPOINT
  ? new S3Client(s3cfg(env.S3_PUBLIC_ENDPOINT)) : s3;
const files = new S3FileHelper(s3, BUCKET, env.PUBLIC_BASE_URL || "http://localhost:8080", { presignS3: presign });

// --- MCP-Apps inline upload widget (renders a file picker in claude.ai/ChatGPT) ---
// The @modelcontextprotocol/sdk has no app= helper, so we attach the _meta gates by hand:
// a text/html;profile=mcp-app resource + ui/openai _meta on upload_file. HTML is language-neutral.
const WIDGET_URI = "ui://markdownify/upload-v1";
const PUBLIC_BASE = (env.PUBLIC_BASE_URL || "http://localhost:8080").replace(/\/+$/, "");
const S3_PUBLIC = (env.S3_PUBLIC_ENDPOINT || env.S3_ENDPOINT || "http://localhost:9100").replace(/\/+$/, "");
const widgetDomain = createHash("sha256").update(PUBLIC_BASE + "/mcp").digest("hex").slice(0, 32) + ".claudemcpcontent.com";
// Signed ticket for the gateway's POST /u/convert/<token> route — the widget uploads bytes
// straight to the server (like notebooklm's /files/ul), which converts on receipt. HMAC key is
// shared with the Python gateway via env (it spawns this backend with the same os.environ).
const SIGN_KEY = env.MCP_UPLOAD_SIGNING_KEY || env.MCP_BEARER_TOKEN || "dev-key";
const b64url = (buf) => Buffer.from(buf).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
function mintConvertUrl() {
  const payload = b64url(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 300 }));
  const sig = b64url(createHmac("sha256", SIGN_KEY).update(payload).digest());
  return `${PUBLIC_BASE}/u/convert/${payload}.${sig}`;
}
const WIDGET_META = {
  "openai/widgetCSP": { connect_domains: [PUBLIC_BASE, S3_PUBLIC], resource_domains: [] },
  ui: { domain: widgetDomain, csp: { connectDomains: [PUBLIC_BASE, S3_PUBLIC] }, prefersBorder: true },
};
const WIDGET_RESOURCE = { uri: WIDGET_URI, name: "markdownify upload", mimeType: "text/html;profile=mcp-app", _meta: WIDGET_META };
const TOOL_META = { "ui/resourceUri": WIDGET_URI, "openai/outputTemplate": WIDGET_URI, ui: { resourceUri: WIDGET_URI, visibility: ["model"] } };
const WIDGET_HTML = `<!doctype html>
<html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta name=color-scheme content="light dark">
<style>body{font-family:system-ui,sans-serif;margin:0;padding:14px;background:transparent;color:#1c2420}
.card{border:1px solid #dde2da;border-radius:10px;padding:16px;max-width:560px;background:#fff}
.head{font-size:14px;font-weight:650;color:#2f6df7}input[type=file]{display:block;margin:12px 0;font-size:15px}
button{font-size:15px;padding:9px 16px;border-radius:8px;border:0;background:#2f6df7;color:#fff}button[disabled]{opacity:.5}
.mini{font-size:13px;padding:5px 10px;background:#eef2fb;color:#2f6df7}
progress{display:block;width:100%;margin-top:10px;height:8px}#out{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:12px;margin-top:10px;color:#4a564e}
#md{display:none;width:100%;height:220px;margin-top:12px;font-family:ui-monospace,monospace;font-size:12px;border:1px solid #dde2da;border-radius:8px;padding:8px;box-sizing:border-box;background:#fafbfa;color:#1c2420}
#actions{display:none;margin-top:8px}#actions a{margin-left:10px;font-weight:600;color:#2f6df7;text-decoration:none}
@media(prefers-color-scheme:dark){body{color:#e6eae4}.card{background:#1d231f;border-color:#313a33}#out{color:#b7c0b8}.mini{background:#243043}#md{background:#151a16;border-color:#313a33;color:#e6eae4}}</style></head>
<body><div class=card><div class=head>📄 File → Markdown</div>
<div id=sub style="font-size:12px;color:#6b7a6e;margin-top:3px">starting…</div>
<input id=f type=file disabled><button id=up disabled>Convert</button>
<progress id=pg value=0 max=100 style="display:none"></progress>
<div id=out></div>
<textarea id=md readonly></textarea>
<div id=actions><button id=cp class=mini>Copy</button><a id=dl download>⬇ Download .md</a></div></div>
<script type=module>
const $=i=>document.getElementById(i);
const sub=$('sub'),out=$('out'),fi=$('f'),btn=$('up'),pg=$('pg'),mdEl=$('md'),actions=$('actions');
const post=m=>{try{window.parent.postMessage(m,"*")}catch(e){}};
const oai=window.openai; let initialized=false,convUrl=null;
const getconv=o=>o&&o.convert_url||null;   // widget uploads bytes straight to the server, which converts on receipt
function size(){post({jsonrpc:"2.0",method:"ui/notifications/size-changed",params:{height:document.documentElement.scrollHeight,width:document.documentElement.scrollWidth}});}
function ready(h){if(initialized)return;initialized=true;sub.textContent=(h||(oai?"ChatGPT":"host"))+" · ready";post({jsonrpc:"2.0",method:"ui/notifications/initialized",params:{}});}
post({jsonrpc:"2.0",id:1,method:"ui/initialize",params:{capabilities:{},protocolVersion:"2026-01-26",clientInfo:{name:"mdfy-upload",version:"1"},appCapabilities:{availableDisplayModes:["inline"]}}});
setTimeout(()=>ready(oai?"ChatGPT":null),500);
function consider(p){if(!p)return;if(p.toolResult)p=p.toolResult;let d=p.structuredContent;
 if(!getconv(d)&&Array.isArray(p.content))for(const c of p.content)if(c&&c.type==="text"){try{const j=JSON.parse(c.text);if(getconv(j))d=j}catch(e){}}
 if(!getconv(d)&&getconv(p))d=p;const u=getconv(d);
 if(u&&!convUrl){convUrl=u;fi.disabled=false;sub.textContent="pick a file to convert to Markdown";}}
window.addEventListener("message",ev=>{let d=ev.data;if(d==null)return;if(typeof d==="string"){try{d=JSON.parse(d)}catch(e){return}}
 if(d.result&&!d.method){ready(d.result.hostInfo&&d.result.hostInfo.name);if(d.result.toolResult)consider(d.result.toolResult);return;}
 if(typeof d.method==="string"){if(d.method.includes("tool"))consider(d.params||{});else if(d.id!=null)post({jsonrpc:"2.0",id:d.id,result:{}});}});
function pullOai(){if(oai&&oai.toolOutput)consider(oai.toolOutput);}
window.addEventListener("openai:set_globals",pullOai);let _pt=0;const _pi=setInterval(()=>{pullOai();if(convUrl||++_pt>66)clearInterval(_pi);},300);
fi.addEventListener('change',()=>{btn.disabled=!(fi.files&&fi.files.length);});
btn.addEventListener('click',()=>{const file=fi.files&&fi.files[0];if(!file||!convUrl){return;}
 btn.disabled=true;fi.disabled=true;pg.style.display="block";pg.value=0;out.textContent="";mdEl.style.display="none";actions.style.display="none";
 const x=new XMLHttpRequest();
 x.open("POST",convUrl+"?filename="+encodeURIComponent(file.name));
 x.setRequestHeader("Content-Type",file.type||"application/octet-stream");
 x.upload.onprogress=e=>{if(e.lengthComputable){pg.value=Math.round(e.loaded/e.total*100);sub.textContent="uploading "+pg.value+"% — converting on the server…";}};
 x.onload=()=>{pg.style.display="none";
  if(x.status>=200&&x.status<300){let md="";try{md=JSON.parse(x.responseText).markdown||""}catch(e){md=x.responseText;}
   mdEl.value=md;mdEl.style.display="block";actions.style.display="block";
   sub.textContent="✅ converted "+file.name+" ("+md.length+" chars)";
   const blob=new Blob([md],{type:"text/markdown"});$('dl').href=URL.createObjectURL(blob);$('dl').download=file.name.replace(/\\.[^.]+$/,"")+".md";
   size();}
  else{sub.textContent="conversion failed";out.textContent="["+x.status+"] "+x.responseText.slice(0,240);btn.disabled=false;fi.disabled=false;}};
 x.onerror=()=>{pg.style.display="none";sub.textContent="";out.textContent="❌ network/CORS error reaching the server";btn.disabled=false;fi.disabled=false;};
 x.send(file);});
$('cp').addEventListener('click',()=>{mdEl.select();try{document.execCommand('copy')}catch(e){}});
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

async function toMarkdown(srcKey, filename) {
  if (!(await waitForKey(srcKey))) throw new Error(`upload for '${path.basename(srcKey)}' not received yet — call to_markdown again once the user has uploaded.`);
  const name = path.basename(filename || srcKey);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mdfy-"));
  const local = path.join(dir, name);
  fs.writeFileSync(local, await getBytes(srcKey));
  const { stdout: md } = await execFileP(MARKITDOWN, [local], { maxBuffer: 64 * 1024 * 1024 });
  fs.rmSync(dir, { recursive: true, force: true });
  if (md.length <= INLINE_CAP) return { content: [{ type: "text", text: md }] };
  const key = `out/${randomUUID()}/${name.replace(/\.[^.]+$/, "")}.md`;
  await s3.send(new PutObjectCommand({ Bucket: BUCKET, Key: key, Body: md }));
  const dl = await files.offerDownload({ key, filename: `${name}.md`, mime: "text/markdown" });
  return { content: [{ type: "text", text: `Markdown ready (large): ${dl.url}` }], structuredContent: dl };
}

const TOOLS = [
  { name: "upload_file",
    description: "STEP 1: get the file the user wants as Markdown. Shows an inline upload widget and returns an upload target (src_key + presigned URL + link). The widget auto-runs to_markdown after upload; if there's no widget, call to_markdown(src_key, filename) next — it waits for the upload.",
    inputSchema: { type: "object", properties: { filename: { type: "string", description: "source filename with extension, e.g. report.pdf" } }, required: ["filename"] },
    _meta: TOOL_META },
  { name: "to_markdown",
    description: "STEP 2: convert the uploaded file (src_key) to Markdown via markitdown (PDF/PPTX/XLSX/DOCX/image/audio…). Blocks until the upload lands, then returns the markdown (or a .md download link if huge). Call right after upload_file.",
    inputSchema: { type: "object", properties: { src_key: { type: "string" }, filename: { type: "string" } }, required: ["src_key"] } },
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
      convert_url: mintConvertUrl(), filename: a.filename };
    return { content: [{ type: "text", text: `Upload ${a.filename} in the widget above (or ${sc.upload_link}); I'll convert it to Markdown.` }], structuredContent: sc, _meta: TOOL_META };
  }
  if (name === "to_markdown") return await toMarkdown(a.src_key, a.filename);
  throw new Error(`unknown tool: ${name}`);
});

await server.connect(new StdioServerTransport());
process.stderr.write("markdownify-filebridge backend: stdio ready\n");
