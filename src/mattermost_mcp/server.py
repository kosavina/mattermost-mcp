"""The mattermost-mcp MCP server, served over Streamable HTTP.

Environment variables:

- MCP_HOST: the address to listen on. Default 127.0.0.1 (this machine only); the container
  sets 0.0.0.0.
- MCP_PUBLIC_HOST: the public host name that the Host/Origin guard accepts besides localhost.
  Unset locally.
- APP_COMMIT: the commit the image was built from, reported by /health. Default "dev".
"""

import os
from importlib.metadata import version

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from mattermost_mcp import health

PORT = 8000

mcp = FastMCP("mattermost-mcp", version=version("mattermost-mcp"))


@mcp.tool
def ping() -> str:
    """Check that the server is up. Always answers "pong"."""
    return "pong"


@mcp.custom_route("/health", methods=["GET"])
async def health_route(request: Request) -> JSONResponse:
    """Plain HTTP health check for Docker, the deploy job and you."""
    return JSONResponse({**health(), "commit": os.environ.get("APP_COMMIT", "dev")})


def create_app(public_host: str | None = None) -> Starlette:
    """Build the ASGI app that serves the MCP endpoint at /mcp and the /health route.

    The Host/Origin guard is on for every route: only localhost and, if given, public_host
    are accepted. Requests with any other Host or Origin header are rejected, which protects
    the server from DNS rebinding attacks.
    """
    return mcp.http_app(
        host_origin_protection=True,
        allowed_hosts=[public_host] if public_host else None,
    )


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    public_host = os.environ.get("MCP_PUBLIC_HOST") or None
    uvicorn.run(create_app(public_host), host=host, port=PORT)
