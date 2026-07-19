// markdownify TS MCP server (backend) — runs over stdio; the OAuth gateway proxies to it.
// Tools: upload_file (filebridge upload widget/link) → to_markdown (stage + markitdown → markdown,
// inline when small, presigned .md when large). Uses ts/s3_filebridge.mjs. Does NO auth — the
// gateway in front owns OAuth.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID, createHash } from "node:crypto";
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
const WIDGET_META = {
  "openai/widgetCSP": { connect_domains: [PUBLIC_BASE, S3_PUBLIC], resource_domains: [] },
  ui: { domain: widgetDomain, csp: { connectDomains: [PUBLIC_BASE, S3_PUBLIC] }, prefersBorder: true },
};
const WIDGET_RESOURCE = { uri: WIDGET_URI, name: "markdownify upload", mimeType: "text/html;profile=mcp-app", _meta: WIDGET_META };
const TOOL_META = { "ui/resourceUri": WIDGET_URI, "openai/outputTemplate": WIDGET_URI, ui: { resourceUri: WIDGET_URI, visibility: ["model"] } };
const WIDGET_HTML = `<!doctype html>
<html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta name=color-scheme content="light dark">
<style>body{font-family:system-ui,sans-serif;margin:0;padding:14px;background:transparent;color:#1c2420}
.card{border:1px solid #dde2da;border-radius:10px;padding:16px;max-width:520px;background:#fff}
.head{font-size:14px;font-weight:650;color:#2f6df7}input[type=file]{display:block;margin:12px 0;font-size:15px}
button{font-size:15px;padding:9px 16px;border-radius:8px;border:0;background:#2f6df7;color:#fff}button[disabled]{opacity:.5}
progress{display:block;width:100%;margin-top:10px;height:8px}#out{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:12px;margin-top:12px;color:#4a564e}
@media(prefers-color-scheme:dark){body{color:#e6eae4}.card{background:#1d231f;border-color:#313a33}#out{color:#b7c0b8}}</style></head>
<body><div class=card><div class=head>📄 Upload a file → Markdown</div>
<div id=sub style="font-size:12px;color:#6b7a6e;margin-top:3px">starting…</div>
<input id=f type=file disabled><button id=up disabled>Upload</button>
<progress id=pg value=0 max=100 style="display:none"></progress><div id=out></div></div>
<script type=module>
const sub=document.getElementById('sub'),out=document.getElementById('out'),fi=document.getElementById('f'),btn=document.getElementById('up'),pg=document.getElementById('pg');
const log=m=>{out.textContent+=(out.textContent?"\\n":"")+m;size();};
const post=m=>{try{window.parent.postMessage(m,"*")}catch(e){}};
const oai=window.openai; let initialized=false,uploadUrl=null,srcKey=null,fname=null,cvSeq=0;
const TOOL="to_markdown"; const geturl=o=>o&&o.upload_url||null;
function size(){post({jsonrpc:"2.0",method:"ui/notifications/size-changed",params:{height:document.documentElement.scrollHeight,width:document.documentElement.scrollWidth}});}
function ready(h){if(initialized)return;initialized=true;sub.textContent=(h||(oai?"ChatGPT":"host"))+" · ready";post({jsonrpc:"2.0",method:"ui/notifications/initialized",params:{}});}
post({jsonrpc:"2.0",id:1,method:"ui/initialize",params:{capabilities:{},protocolVersion:"2026-01-26",clientInfo:{name:"mdfy-upload",version:"1"},appCapabilities:{availableDisplayModes:["inline"]}}});
setTimeout(()=>ready(oai?"ChatGPT":null),500);
function consider(p){if(!p)return;if(p.toolResult)p=p.toolResult;let d=p.structuredContent;
 if(!geturl(d)&&Array.isArray(p.content))for(const c of p.content)if(c&&c.type==="text"){try{const j=JSON.parse(c.text);if(geturl(j))d=j}catch(e){}}
 if(!geturl(d)&&geturl(p))d=p;const u=geturl(d);
 if(u&&!uploadUrl){uploadUrl=u;srcKey=d.src_key;fname=d.filename;fi.disabled=false;sub.textContent="pick the file to convert";}}
window.addEventListener("message",ev=>{let d=ev.data;if(d==null)return;if(typeof d==="string"){try{d=JSON.parse(d)}catch(e){return}}
 if(d.result&&!d.method){ready(d.result.hostInfo&&d.result.hostInfo.name);if(d.result.toolResult)consider(d.result.toolResult);return;}
 if(typeof d.method==="string"){if(d.method.includes("tool"))consider(d.params||{});else if(d.id!=null)post({jsonrpc:"2.0",id:d.id,result:{}});}});
function pullOai(){if(oai&&oai.toolOutput)consider(oai.toolOutput);}
window.addEventListener("openai:set_globals",pullOai);let _pt=0;const _pi=setInterval(()=>{pullOai();if(uploadUrl||++_pt>66)clearInterval(_pi);},300);
function putFile(url,file,onp){return new Promise((res,rej)=>{const x=new XMLHttpRequest();x.open("PUT",url);
 x.upload.onprogress=e=>{if(e.lengthComputable&&onp)onp(Math.round(e.loaded/e.total*100));};
 x.onload=()=>res({ok:x.status>=200&&x.status<300,status:x.status});x.onerror=()=>rej(new Error("net"));x.send(file);});}
function autoRun(name){if(!srcKey)return null;const args={src_key:srcKey,filename:name||fname||""};
 if(oai&&typeof oai.callTool==="function"){try{return oai.callTool(TOOL,args);}catch(e){return null;}}
 post({jsonrpc:"2.0",id:"cv"+(++cvSeq),method:"tools/call",params:{name:TOOL,arguments:args}});return null;}
fi.addEventListener('change',()=>{btn.disabled=!(fi.files&&fi.files.length);});
btn.addEventListener('click',async()=>{const file=fi.files&&fi.files[0];if(!file||!uploadUrl){log("no file yet");return;}
 btn.disabled=true;fi.disabled=true;pg.style.display="block";pg.value=0;
 try{const res=await putFile(uploadUrl,file,pct=>{pg.value=pct;sub.textContent="uploading "+pct+"%";});pg.style.display="none";
  if(!res.ok){log("["+res.status+"] upload failed");btn.disabled=false;fi.disabled=false;return;}
  log("uploaded ✓");const p=autoRun(file.name);
  if(p){sub.textContent="converting…";try{const r=await p;const o=(r&&(r.structuredContent||r.toolResult||r))||{};
    sub.textContent="✅ converted — see the chat";if(o.url){const a=document.createElement("a");a.href=o.url;a.target="_blank";a.textContent="⬇ Download .md";a.style.cssText="display:block;margin-top:10px;font-weight:600;color:#2f6df7";document.querySelector(".card").appendChild(a);size();}
   }catch(e){sub.textContent="✅ uploaded — ask me to convert it";}}
  else{sub.textContent="✅ uploaded — converting…";}
 }catch(e){pg.style.display="none";log("❌ "+e);btn.disabled=false;fi.disabled=false;}});
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
    const sc = { ...offer, upload_url: offer.agent_upload?.url, upload_link: offer.human_upload?.url, filename: a.filename };
    return { content: [{ type: "text", text: `Upload ${a.filename} in the widget above (or ${sc.upload_link}); I'll convert it to Markdown.` }], structuredContent: sc, _meta: TOOL_META };
  }
  if (name === "to_markdown") return await toMarkdown(a.src_key, a.filename);
  throw new Error(`unknown tool: ${name}`);
});

await server.connect(new StdioServerTransport());
process.stderr.write("markdownify-filebridge backend: stdio ready\n");
