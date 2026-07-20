// MCP-Apps render gates for a raw @modelcontextprotocol/sdk widget. The domain gate MUST stay
// byte-compatible with mcp_filebridge/gates.py widget_domain (the parity test proves it).
import { createHash } from "node:crypto";

// claude.ai render gate: sha256("<base>/mcp")[:32] + ".claudemcpcontent.com".
export function widgetDomain(baseUrl) {
  const endpoint = String(baseUrl || "").replace(/\/+$/, "") + "/mcp";
  return createHash("sha256").update(endpoint).digest("hex").slice(0, 32) + ".claudemcpcontent.com";
}

// _meta for the widget RESOURCE (claude.ai reads ui.*; ChatGPT reads openai/widgetCSP).
export function resourceMeta(domain, connectDomains) {
  return {
    "openai/widgetCSP": { connect_domains: connectDomains, resource_domains: [] },
    ui: { domain, csp: { connectDomains }, prefersBorder: true },
  };
}

// _meta for the TOOL that renders the widget (claude.ai reads ui/resourceUri + ui.resourceUri;
// ChatGPT reads openai/outputTemplate). All point at the one mcp-app resource.
export function toolMeta(uri) {
  return { "ui/resourceUri": uri, "openai/outputTemplate": uri, ui: { resourceUri: uri, visibility: ["model"] } };
}
