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
  S3Client, PutObjectCommand, GetObjectCommand, HeadObjectCommand, CreateBucketCommand,
} from "@aws-sdk/client-s3";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";

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
    description: "STEP 1: get the file the user wants as Markdown. Returns an upload target (src_key + presigned URL + link). Immediately call to_markdown(src_key, filename) next — it waits for the upload.",
    inputSchema: { type: "object", properties: { filename: { type: "string", description: "source filename with extension, e.g. report.pdf" } }, required: ["filename"] } },
  { name: "to_markdown",
    description: "STEP 2: convert the uploaded file (src_key) to Markdown via markitdown (PDF/PPTX/XLSX/DOCX/image/audio…). Blocks until the upload lands, then returns the markdown (or a .md download link if huge). Call right after upload_file.",
    inputSchema: { type: "object", properties: { src_key: { type: "string" }, filename: { type: "string" } }, required: ["src_key"] } },
];

const server = new Server({ name: "markdownify-filebridge", version: "0.1.0" }, { capabilities: { tools: {} } });
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  await ensureBucket();
  const { name, arguments: a = {} } = req.params;
  if (name === "upload_file") {
    const offer = await files.offerUpload({ filename: a.filename });
    return { content: [{ type: "text", text: `Upload ${a.filename}, then call to_markdown with src_key=${offer.src_key}` }], structuredContent: offer };
  }
  if (name === "to_markdown") return await toMarkdown(a.src_key, a.filename);
  throw new Error(`unknown tool: ${name}`);
});

await server.connect(new StdioServerTransport());
process.stderr.write("markdownify-filebridge backend: stdio ready\n");
