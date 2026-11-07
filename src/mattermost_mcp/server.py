"""The mattermost-mcp MCP server, served over Streamable HTTP on this machine only."""

from importlib.metadata import version

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette

HOST = "127.0.0.1"
PORT = 8000

mcp = FastMCP("mattermost-mcp", version=version("mattermost-mcp"))


@mcp.tool
def ping() -> str:
    """Check that the server is up. Always answers "pong"."""
    return "pong"


def create_app() -> Starlette:
    """Build the ASGI app that serves the MCP endpoint at /mcp.

    The Host/Origin guard is on: requests with a foreign Host or Origin header are
    rejected, which protects this localhost server from DNS rebinding attacks.
    """
    return mcp.http_app(host_origin_protection=True)


def main() -> None:
    uvicorn.run(create_app(), host=HOST, port=PORT)
