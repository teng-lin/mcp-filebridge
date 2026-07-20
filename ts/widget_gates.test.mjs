// Unit tests for the render gates. Run: node --test widget_gates.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { widgetDomain, resourceMeta, toolMeta } from "./widget_gates.mjs";

test("widgetDomain is the sha256 claude.ai gate, trailing-slash-insensitive", () => {
  const d = widgetDomain("https://x.example");
  assert.match(d, /^[0-9a-f]{32}\.claudemcpcontent\.com$/);
  assert.equal(widgetDomain("https://x.example/"), d);
});

test("resourceMeta carries both host CSP gates + the domain", () => {
  const m = resourceMeta("d.claudemcpcontent.com", ["https://a", "https://b"]);
  assert.equal(m.ui.domain, "d.claudemcpcontent.com");
  assert.deepEqual(m.ui.csp.connectDomains, ["https://a", "https://b"]);
  assert.deepEqual(m["openai/widgetCSP"].connect_domains, ["https://a", "https://b"]);
});

test("toolMeta points all three pointers at the resource uri", () => {
  const m = toolMeta("ui://x/1");
  assert.equal(m["ui/resourceUri"], "ui://x/1");
  assert.equal(m["openai/outputTemplate"], "ui://x/1");
  assert.equal(m.ui.resourceUri, "ui://x/1");
});
