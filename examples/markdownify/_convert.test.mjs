// Unit tests for the pure convert helpers. Run: node --test _convert.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { b64url, b64urlDecode, mdKeyFor, mintTicket, verifyTicket } from "./_convert.mjs";

test("b64url is url-safe (no + / =) and round-trips", () => {
  const s = b64url(Buffer.from("hello?/+world>>>"));
  assert.ok(!/[+/=]/.test(s), "must not contain +, /, or =");
  assert.equal(b64urlDecode(s).toString(), "hello?/+world>>>");
});

test("mdKeyFor derives md/<uuid>/<stem>.md and strips only the last extension", () => {
  assert.equal(mdKeyFor("src/abc-123/report.docx"), "md/abc-123/report.md");
  assert.equal(mdKeyFor("src/u/a.b.pdf"), "md/u/a.b.md");
  assert.equal(mdKeyFor("src/u/noext"), "md/u/noext.md");
});

test("mdKeyFor is deterministic on malformed input (no randomness)", () => {
  assert.equal(mdKeyFor("garbage"), mdKeyFor("garbage"));
  assert.equal(mdKeyFor(""), mdKeyFor(""));
});

test("mintTicket/verifyTicket round-trips the payload", () => {
  const t = mintTicket("src/u/f.pdf", "k", { nowSec: 1000, ttl: 300 });
  const p = verifyTicket(t, "k", { nowSec: 1100 });
  assert.equal(p.k, "src/u/f.pdf");
  assert.equal(p.exp, 1300);
});

test("verifyTicket rejects a wrong key", () => {
  const t = mintTicket("src/u/f.pdf", "k1", { nowSec: 1000 });
  assert.equal(verifyTicket(t, "k2", { nowSec: 1000 }), null);
});

test("verifyTicket rejects a tampered signature", () => {
  const t = mintTicket("src/u/f.pdf", "k", { nowSec: 1000 });
  const bad = t.slice(0, -2) + (t.endsWith("aa") ? "bb" : "aa");
  assert.equal(verifyTicket(bad, "k", { nowSec: 1000 }), null);
});

test("verifyTicket rejects a forged payload (can't lift the sig onto new data)", () => {
  const t = mintTicket("src/u/f.pdf", "k", { nowSec: 1000 });
  const sig = t.split(".")[1];
  const forged = b64url(Buffer.from(JSON.stringify({ exp: 9e9, k: "src/evil/x" }))) + "." + sig;
  assert.equal(verifyTicket(forged, "k", { nowSec: 1000 }), null);
});

test("verifyTicket rejects an expired ticket", () => {
  const t = mintTicket("src/u/f.pdf", "k", { nowSec: 1000, ttl: 300 }); // exp 1300
  assert.equal(verifyTicket(t, "k", { nowSec: 2000 }), null);
});

test("verifyTicket rejects malformed tokens", () => {
  for (const bad of ["", "nodot", "a.b.c."]) {
    assert.equal(verifyTicket(bad, "k", { nowSec: 0 }), null);
  }
});
