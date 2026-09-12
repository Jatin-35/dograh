"""Shared ASGI app for the Dograh MCP server.

Hoisted out of `api/app.py` into its own module for a stable import path.
This is the external-client-facing MCP surface (Claude Code, Cursor, etc.)
mounted at `/api/v1/mcp` — the in-product AI assistant
(`api/services/workflow_gen/`) does not go through this at all; it calls
`WorkflowGenToolbox` directly. (An earlier version of this feature tried
reaching this app in-process via `httpx.ASGITransport` to avoid a network
hop — that breaks the MCP session handshake, since Streamable HTTP's
session/SSE semantics don't survive being reconstructed through a fresh
`httpx.AsyncClient` per connection. Don't reintroduce that.)
"""

from api.mcp_server.server import mcp

mcp_app = mcp.http_app(path="/", stateless_http=True)
