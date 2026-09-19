"""Family Assistant as an MCP server for external clients.

See docs/design/mcp-adapter.md. ``install_mcp_adapter`` mounts the Streamable
HTTP endpoint and the OAuth authorization server on a FastAPI app; the returned
adapter's ``run()`` must be entered for the app's lifetime.
"""

from family_assistant.web.mcp_adapter.server import MCPAdapter, install_mcp_adapter

__all__ = ["MCPAdapter", "install_mcp_adapter"]
