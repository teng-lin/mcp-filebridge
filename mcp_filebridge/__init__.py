"""mcp-filebridge — a cross-language S3 file side-channel for remote MCP servers.

Submodules (import explicitly to keep package import light):
- s3_filebridge : the S3 presigning helper (offer_upload/offer_download/await_upload) + client factory
- oauth         : a single-tenant self-hosted OAuth 2.1 provider (static client + CIMD + password login)
- widget        : the inline MCP-Apps upload widget (render gates + registration)
- convert       : the convert-on-upload broker route (HMAC ticket + md_key derivation)
- gates         : the MCP-Apps render gates (claude.ai domain + _meta), shared by widgets
"""
__version__ = "0.1.0"
