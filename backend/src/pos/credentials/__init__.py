"""Credentials backed by 1Password: agents use secrets without ever seeing them.

The owner keeps a registry of named credentials (a 1Password secret reference
op://vault/item/field, how it is injected, where it may go; never a value).
An agent that holds a grant `cred:<name>` (owner only; the Access manager is
refused by pos.access) names the credential when it runs a command or an
HTTP call; the value is resolved at execution time with the 1Password
service account, injected into that one subprocess or request, and redacted
(plain, base64, URL-encoded, hex; also across output chunks) from everything
that goes back to the model, a log or storage. Every resolution is logged;
too many in an hour pause the grant. No token, no vault, or 1Password down:
every use fails closed. See docs/CREDENTIALS.md.

Modules:
    onepassword  the SDK seam, the short in-memory cache (tests swap the provider)
    redact       redaction, also for chunked output (a copy lives in the worker)
    store        tables (created on first use)
    service      registry, grants, requests -> ask_owner, resolution, the use log, HTTP
    mcp          credentials_list and credential_http on the pos MCP server
    api          /api/credentials (web app) and /api/worker/credentials (the worker's runner)
"""
