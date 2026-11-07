import http.client
import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
import uvicorn
from fastmcp import Client

from mattermost_mcp.server import create_app


@pytest.fixture
def server_url() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("the test server did not start within 5 seconds")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}/mcp"

    server.should_exit = True
    thread.join()


def post_empty_body(url: str, extra_headers: dict[str, str]) -> int:
    parts = urlsplit(url)
    assert parts.hostname is not None
    assert parts.port is not None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **extra_headers,
    }
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request("POST", parts.path, body="{}", headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()


@pytest.mark.anyio
async def test_ping_over_http(server_url: str) -> None:
    async with Client(server_url) as client:
        result = await client.call_tool("ping")

    assert result.data == "pong"


def test_foreign_host_header_is_rejected(server_url: str) -> None:
    # Guards DNS rebinding protection: a request for another host name must not reach MCP.
    assert post_empty_body(server_url, {"Host": "evil.example"}) == 421


def test_foreign_origin_is_rejected(server_url: str) -> None:
    # Guards DNS rebinding protection: a web page from another origin must not reach MCP.
    assert post_empty_body(server_url, {"Origin": "https://evil.example"}) == 403
