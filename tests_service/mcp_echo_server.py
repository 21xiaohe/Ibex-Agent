"""A local MCP server used exclusively for transport verification."""
from mcp.server.fastmcp import FastMCP

server = FastMCP("ibex-local-verification")


@server.tool()
def verification_echo(text: str) -> str:
    """Return a non-sensitive test string."""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
