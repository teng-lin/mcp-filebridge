// s3_filebridge — TypeScript/Node twin of s3_filebridge.py.
// Same offer contract + widget, S3 engine via @aws-sdk. Proves the design is
// language-neutral: the static offer strings + widget page are copied verbatim
// from the Python; only the presign call differs (boto3 -> @aws-sdk).
import { randomUUID } from "node:crypto";
import { S3Client, PutObjectCommand, GetObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

export function uploadPage(putUrl) {
  const u = JSON.stringify(putUrl);
  return (
    "<!doctype html><meta charset=utf-8><title>Upload a file</title>" +
    "<input type=file id=f> <button onclick=up()>Upload</button><pre id=s></pre>" +
    "<script>async function up(){const f=document.getElementById('f').files[0];" +
    "document.getElementById('s').textContent='Uploading '+f.name+'\\u2026';" +
    `const r=await fetch(${u},{method:'PUT',body:f});` +
    "document.getElementById('s').textContent=r.ok?'Done \\u2713':'Failed '+r.status;}</script>"
  );
}

export class ShortLinkStore {
  #m = new Map();
  put(url) { const sid = randomUUID().replace(/-/g, "").slice(0, 8); this.#m.set(sid, url); return sid; }
  get(sid) { return this.#m.get(sid); }
}

export class S3FileHelper {
  constructor(s3, bucket, widgetBaseUrl, { uploadTtl = 300, downloadTtl = 900, links } = {}) {
    this.s3 = s3; this.bucket = bucket; this.base = widgetBaseUrl.replace(/\/+$/, "");
    this.upTtl = uploadTtl; this.dlTtl = downloadTtl; this.links = links ?? new ShortLinkStore();
  }
  #shortlink(target) { return `${this.base}/u/${this.links.put(target)}`; }

  async offerUpload({ filename, keyPrefix = "src", mime = null }) {
    const key = `${keyPrefix}/${randomUUID()}/${filename}`;
    const cmd = new PutObjectCommand({ Bucket: this.bucket, Key: key, ...(mime ? { ContentType: mime } : {}) });
    const put = await getSignedUrl(this.s3, cmd, { expiresIn: this.upTtl });
    return {
      status: "upload_required",
      src_key: key,
      expires_in_seconds: this.upTtl,
      mime_locked: Boolean(mime),
      human_upload: {
        url: this.#shortlink(put),
        instructions: "Open on the device that has the file, then pick it. Works on mobile.",
      },
      agent_upload: {
        method: "PUT",
        url: put,
        body: "raw file bytes (not multipart/form-data)",
        returns: '{"status":"added","key":...}',
        example: `curl -X PUT -T <file> '${put}'`,
      },
      agent_instructions: "Try agent_upload (PUT the bytes); else surface human_upload.url to the user.",
    };
  }

  async offerDownload({ key, filename, mime }) {
    const url = await getSignedUrl(this.s3, new GetObjectCommand({ Bucket: this.bucket, Key: key }),
      { expiresIn: this.dlTtl });
    return { status: "download_ready", filename, mime_type: mime, url, expires_in_seconds: this.dlTtl };
  }
}

// --- main: emit an offer for parity + do a live round-trip against MinIO --- #
const s3 = new S3Client({
  endpoint: "http://localhost:9100", region: "us-east-1", forcePathStyle: true,
  credentials: { accessKeyId: "spikekey", secretAccessKey: "spikesecret" },
  // @aws-sdk v3 defaults to signing a CRC32 checksum into presigned URLs, which
  // breaks a plain PUT (curl/browser fetch sends no matching checksum header) on
  // real AWS S3. Match boto3's leaner presign and keep the URL usable by any client.
  requestChecksumCalculation: "WHEN_REQUIRED",
  responseChecksumValidation: "WHEN_REQUIRED",
});
const h = new S3FileHelper(s3, "s3fb-spike", "https://mcp.example.test");

const offer = await h.offerUpload({ filename: "spike.txt" });

// functional: PUT bytes to the TS-minted presigned URL, GET them back
const put = await fetch(offer.agent_upload.url, { method: "PUT", body: "hello from TS agent path" });
const dl = await h.offerDownload({ key: offer.src_key, filename: "spike.txt", mime: "text/plain" });
const got = await fetch(dl.url);
const body = await got.text();
const functional = put.status === 200 && body === "hello from TS agent path" ? "ok" : `FAIL put=${put.status} body=${body}`;

console.log(JSON.stringify({ offer, functional, widget_has_picker: uploadPage(put.url ?? offer.agent_upload.url).includes("type=file") }));
