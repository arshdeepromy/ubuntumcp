"""OAuth-gating reverse proxy in front of the Playwright MCP HTTP server.

The Playwright MCP server (``playwright-mcp --port 8931``) has no authentication,
so it must never face the internet directly. This package puts the same OAuth 2.1
authorization flow the bridge uses (dynamic client registration, PKCE, an
operator consent screen, bearer tokens) in front of it, then streams authorized
``/mcp`` traffic through to the local Playwright server.
"""
