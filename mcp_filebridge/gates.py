"""MCP-Apps render gates shared by the upload widgets.

The claude.ai domain gate is exact-match-critical: the host derives the same value from the
connector URL and refuses to render the widget on a mismatch. The TypeScript twin is
`widgetDomain` in ts/widget_gates.mjs (kept byte-compatible via the parity test)."""
import hashlib


def widget_domain(base_url: str) -> str:
    """claude.ai render gate: sha256("<base>/mcp")[:32] + ".claudemcpcontent.com"."""
    endpoint = f"{base_url.rstrip('/')}/mcp"
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32] + ".claudemcpcontent.com"
