// Shared MCP-Apps host bridge for upload widgets — single-sourced across the Python (widget.py)
// and JS (server.mjs) widgets. Speaks the claude.ai postMessage handshake and the ChatGPT
// window.openai path. The embedding widget must define, BEFORE this runs:
//   window.onToolResult(payload)  — pull the URL out of a tool result + wire the UI; set
//                                   window.__gotResult = true once it has what it needs.
//   window.onReady(label)         — (optional) called once the host handshake completes.
// The bridge exposes window.wpost(msg) and window.wsize() for the widget to use.
// __CLIENT_NAME__ is substituted by each embedder.
(function () {
  var oai = window.openai, initialized = false;
  function post(m) { try { window.parent.postMessage(m, "*"); } catch (e) {} }
  function size() { post({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth } }); }
  function ready(h) { if (initialized) return; initialized = true; if (window.onReady) window.onReady(h || (oai ? "ChatGPT" : "host")); post({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} }); }
  window.wpost = post; window.wsize = size;
  post({ jsonrpc: "2.0", id: 1, method: "ui/initialize", params: { capabilities: {}, protocolVersion: "2026-01-26", clientInfo: { name: "__CLIENT_NAME__", version: "1" }, appCapabilities: { availableDisplayModes: ["inline"] } } });
  setTimeout(function () { ready(oai ? "ChatGPT" : null); }, 500);
  function consider(p) { if (p && window.onToolResult) window.onToolResult(p); }
  window.addEventListener("message", function (ev) {
    var d = ev.data; if (d == null) return; if (typeof d === "string") { try { d = JSON.parse(d); } catch (e) { return; } }
    if (d.result && !d.method) { ready(d.result.hostInfo && d.result.hostInfo.name); if (d.result.toolResult) consider(d.result.toolResult); return; }
    if (typeof d.method === "string") { if (d.method.indexOf("tool") >= 0) consider(d.params || {}); else if (d.id != null) post({ jsonrpc: "2.0", id: d.id, result: {} }); }
  });
  function pullOai() { if (oai && oai.toolOutput) consider(oai.toolOutput); }
  window.addEventListener("openai:set_globals", pullOai);
  var _pt = 0, _pi = setInterval(function () { pullOai(); if (window.__gotResult || ++_pt > 66) clearInterval(_pi); }, 300);
})();
