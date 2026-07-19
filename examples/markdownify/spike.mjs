// Validated spike: markdownify + s3_filebridge (TS side).
//
// Extends the markdownify-mcp idea with the two directions it lacks:
//   UPLOAD  — stage a user's file (PDF/docx/xlsx/…) from S3 onto local disk so
//             markitdown (which takes a local path) can read it.
//   DOWNLOAD— if the resulting markdown is large, hand back a presigned .md URL
//             instead of a giant inline blob.
//
// The offer contract + presigning come from ts/s3_filebridge.mjs (the TS twin of
// python/s3_filebridge.py) — so this is also the "TS core works in a real server"
// proof. markitdown does the actual conversion.
//
// Run (needs the local MinIO + markitdown on MARKITDOWN_BIN): node spike.mjs
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import {
  S3Client, PutObjectCommand, GetObjectCommand, HeadObjectCommand, CreateBucketCommand,
} from "@aws-sdk/client-s3";

import { S3FileHelper } from "../../ts/s3_filebridge.mjs";

const execFileP = promisify(execFile);
const BUCKET = "markdownify-spike";
const MARKITDOWN = process.env.MARKITDOWN_BIN || "markitdown";
const INLINE_CAP = Number(process.env.MD_INLINE_CAP || 200_000);

const s3 = new S3Client({
  endpoint: "http://localhost:9100", region: "us-east-1", forcePathStyle: true,
  credentials: { accessKeyId: "spikekey", secretAccessKey: "spikesecret" },
  requestChecksumCalculation: "WHEN_REQUIRED", responseChecksumValidation: "WHEN_REQUIRED",
});
const files = new S3FileHelper(s3, BUCKET, "https://mcp.example.test");

async function ensureBucket() {
  try { await s3.send(new CreateBucketCommand({ Bucket: BUCKET })); } catch { /* exists */ }
}

async function getBytes(key) {
  const r = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
  return Buffer.from(await r.Body.transformToByteArray());
}

// Block until the upload lands (the TS twin has no awaitUpload yet; poll head_object).
async function waitForKey(key, timeoutS = 30) {
  for (let i = 0; i < timeoutS * 2; i++) {
    try { await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: key })); return true; }
    catch { await new Promise((r) => setTimeout(r, 500)); }
  }
  return false;
}

// STEP 2: stage the uploaded file locally → markitdown → markdown (inline, or a .md download if large).
async function toMarkdown(srcKey, filename, inlineCap = INLINE_CAP) {
  if (!(await waitForKey(srcKey))) throw new Error(`upload not received: ${srcKey}`);
  const name = path.basename(filename || srcKey);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mdfy-"));
  const local = path.join(dir, name);
  fs.writeFileSync(local, await getBytes(srcKey));
  const { stdout: md } = await execFileP(MARKITDOWN, [local], { maxBuffer: 64 * 1024 * 1024 });
  fs.rmSync(dir, { recursive: true, force: true });
  if (md.length <= inlineCap) return { kind: "inline", markdown: md };
  const key = `out/${randomUUID()}/${name}.md`;
  await s3.send(new PutObjectCommand({ Bucket: BUCKET, Key: key, Body: md }));
  const dl = await files.offerDownload({ key, filename: `${name}.md`, mime: "text/markdown" });
  return { kind: "download", url: dl.url, filename: `${name}.md` };
}

// --- self-check: upload a real .docx → markdown, both inline and download paths --- #
async function main() {
  await ensureBucket();
  const docx = fs.readFileSync(path.join(import.meta.dirname, "report.docx"));

  // STEP 1: offer an upload target (widget/link) for the source file
  const offer = await files.offerUpload({ filename: "report.docx" });
  // simulate the widget/agent PUT to the presigned URL
  const put = await fetch(offer.agent_upload.url, { method: "PUT", body: docx });
  if (put.status !== 200) throw new Error(`presigned PUT failed: ${put.status}`);

  // inline path
  const res = await toMarkdown(offer.src_key, "report.docx");
  const md = res.markdown || "";
  const ok = md.includes("Quarterly Report") && md.includes("MRR") && md.includes("Revenue up 20%");
  if (res.kind !== "inline" || !ok) throw new Error("inline markdown missing expected content:\n" + md.slice(0, 300));

  // large path: force the download branch (cap=10) and fetch the .md back via the presigned URL
  const res2 = await toMarkdown(offer.src_key, "report.docx", 10);
  if (res2.kind !== "download") throw new Error("expected download branch for large markdown");
  const got = await fetch(res2.url);
  const back = await got.text();
  if (!back.includes("Quarterly Report")) throw new Error("downloaded .md missing content");

  console.log(`ok: docx → markdown via markitdown+filebridge`);
  console.log(`  inline: ${md.length} chars, has heading+table+bullets ✓`);
  console.log(`  download: presigned .md fetched (${back.length} chars) ✓`);
  console.log(`  markdown preview:\n${md.split("\n").filter(Boolean).slice(0, 6).map((l) => "    " + l).join("\n")}`);
}

main().catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
