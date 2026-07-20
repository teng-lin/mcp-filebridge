// Pure, side-effect-free helpers shared by server.mjs and its tests.
// The ticket + md_key logic here MUST stay byte-compatible with the Python twin
// in _convert.py (the gateway mints/derives in JS, verifies/derives in Python).
import { createHmac } from "node:crypto";

export const b64url = (buf) =>
  Buffer.from(buf).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export const b64urlDecode = (s) =>
  Buffer.from(s.replace(/-/g, "+").replace(/_/g, "/"), "base64");

// Deterministic .md key from a src_key ("src/<uuid>/<file>"). MUST match _convert.py md_key_for
// so the widget's conversion (stored by the gateway) is reused by to_markdown(src_key).
export function mdKeyFor(srcKey) {
  const p = String(srcKey || "").split("/");
  if (p.length >= 3 && p[1]) {
    const stem = (p[2] || "document").replace(/\.[^.]+$/, "") || "document";
    return `md/${p[1]}/${stem}.md`;
  }
  const stem = (p[p.length - 1] || "document").replace(/\.[^.]+$/, "") || "document";
  return `md/_/${stem}.md`;
}

// A signed, short-lived ticket authorizing one conversion. Payload carries {exp, k:src_key}.
export function mintTicket(srcKey, signKey, { nowSec = Math.floor(Date.now() / 1000), ttl = 300 } = {}) {
  const payload = b64url(JSON.stringify({ exp: nowSec + ttl, k: srcKey }));
  const sig = b64url(createHmac("sha256", signKey).update(payload).digest());
  return `${payload}.${sig}`;
}

// Returns the verified, unexpired payload object, or null. Constant-time signature compare.
export function verifyTicket(token, signKey, { nowSec = Math.floor(Date.now() / 1000) } = {}) {
  const i = String(token).indexOf(".");
  if (i < 0) return null;
  const payload = token.slice(0, i), sig = token.slice(i + 1);
  const expected = b64url(createHmac("sha256", signKey).update(payload).digest());
  if (expected.length !== sig.length) return null;
  let diff = 0;
  for (let k = 0; k < sig.length; k++) diff |= expected.charCodeAt(k) ^ sig.charCodeAt(k);
  if (diff !== 0) return null;
  let data;
  try { data = JSON.parse(b64urlDecode(payload).toString("utf-8")); } catch { return null; }
  return Number(data.exp || 0) >= nowSec ? data : null;
}
