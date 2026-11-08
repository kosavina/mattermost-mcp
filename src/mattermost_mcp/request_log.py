"""One log line per HTTP request."""

import json
import logging
import time

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("mattermost_mcp.http")

LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})


def quoted(value: str | None) -> str:
    """Quote a value the client controls, so that it cannot break or forge a log line."""
    return "-" if value is None else json.dumps(value)


def is_container_health_check(scope: Scope, status: int | None) -> bool:
    """Recognise a passing HEALTHCHECK from inside the container, which runs every 10 seconds.

    Checks through Caddy arrive with the caller's address, so they do not match, and neither
    does a failing check: those are the lines worth reading.
    """
    client = scope.get("client")
    return (
        scope["method"] == "GET"
        and scope["path"] == "/health"
        and status == 200
        and client is not None
        and client[0] in LOOPBACK_ADDRESSES
    )


class RequestLogMiddleware:
    """Log every HTTP request once its response has ended, except the container's health check.

    Added outermost, so it also logs the requests that the Host/Origin guard rejects. It reads
    only the headers named below: bodies, credentials and all other headers never reach the log.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status: int | None = None

        async def send_and_record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_record_status)
        except Exception:
            # Starlette's ServerErrorMiddleware, outside this one, answers these with a 500.
            if status is None:
                status = 500
            raise
        finally:
            if not is_container_health_check(scope, status):
                headers = Headers(scope=scope)
                client = scope.get("client")
                logger.info(
                    "method=%s path=%s status=%s duration_ms=%.1f client=%s scheme=%s origin=%s "
                    "user_agent=%s mcp_protocol_version=%s mcp_method=%s mcp_session_id=%s",
                    scope["method"],
                    quoted(scope["path"]),
                    "-" if status is None else status,
                    (time.perf_counter() - start) * 1000,
                    client[0] if client else "-",
                    scope["scheme"],
                    quoted(headers.get("origin")),
                    quoted(headers.get("user-agent")),
                    quoted(headers.get("mcp-protocol-version")),
                    quoted(headers.get("mcp-method")),
                    "yes" if "mcp-session-id" in headers else "no",
                )
