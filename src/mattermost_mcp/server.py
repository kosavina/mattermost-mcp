"""The mattermost-mcp MCP server, served over Streamable HTTP.

Environment variables:

- MCP_HOST: the address to listen on. Default 127.0.0.1 (this machine only); the container
  sets 0.0.0.0.
- MCP_PUBLIC_HOST: the public host name that the Host/Origin guard accepts besides localhost.
  Unset locally.
- MCP_TRUSTED_PROXIES: the addresses or CIDR ranges (comma-separated) whose X-Forwarded-For and
  X-Forwarded-Proto headers are believed. Default 127.0.0.1; production sets the subnet of the
  private Docker network, where only Caddy connects from.
- APP_COMMIT: the commit the image was built from, reported by /health. Default "dev".
"""

import logging
import os
import sys
from importlib.metadata import version
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.middleware.logging import LoggingMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from mattermost_mcp import health
from mattermost_mcp.request_log import RequestLogMiddleware

PORT = 8000

mcp = FastMCP("mattermost-mcp", version=version("mattermost-mcp"))
mcp.add_middleware(LoggingMiddleware(include_payloads=False))


@mcp.tool(
    title="Ping",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
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
    app = mcp.http_app(
        host_origin_protection=True,
        allowed_hosts=[public_host] if public_host else None,
    )
    app.add_middleware(RequestLogMiddleware)
    return app


def uvicorn_settings(host: str, port: int = PORT) -> dict[str, Any]:
    """Return the uvicorn settings that main() uses and the HTTP tests run under."""
    return {
        "host": host,
        "port": port,
        "forwarded_allow_ips": os.environ.get("MCP_TRUSTED_PROXIES") or "127.0.0.1",
        # RequestLogMiddleware already writes one line per request, without the query string.
        "access_log": False,
    }


def configure_logging() -> None:
    """Print the request log and FastMCP's method log to stdout, one plain line per event."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    for name in ("mattermost_mcp", "fastmcp"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False


def main() -> None:
    configure_logging()
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    public_host = os.environ.get("MCP_PUBLIC_HOST") or None
    uvicorn.run(create_app(public_host), **uvicorn_settings(host))
