import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
import uvicorn
from fastmcp import Client
from starlette.applications import Starlette

from mattermost_mcp.server import create_app

PUBLIC_HOST = "mcp.example.test"


def run_app(app: Starlette) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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


@pytest.fixture
def server_url() -> Iterator[str]:
    yield from run_app(create_app())


@pytest.fixture
def public_server_url() -> Iterator[str]:
    yield from run_app(create_app(public_host=PUBLIC_HOST))


def send(
    method: str,
    url: str,
    extra_headers: dict[str, str] | None = None,
    body: str | None = None,
) -> tuple[int, bytes]:
    parts = urlsplit(url)
    assert parts.hostname is not None
    assert parts.port is not None
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request(method, parts.path, body=body, headers=extra_headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def post_empty_body(url: str, extra_headers: dict[str, str]) -> int:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **extra_headers,
    }
    status, _ = send("POST", url, headers, body="{}")
    return status


@pytest.mark.anyio
async def test_ping_over_http(server_url: str) -> None:
    async with Client(server_url) as client:
        result = await client.call_tool("ping")

    assert result.data == "pong"


def test_health_over_http(server_url: str) -> None:
    status, body = send("GET", server_url.removesuffix("/mcp") + "/health")

    assert status == 200
    assert json.loads(body) == {"status": "ok", "commit": "dev"}


def test_foreign_host_header_is_rejected(server_url: str) -> None:
    # Guards DNS rebinding protection: a request for another host name must not reach MCP.
    assert post_empty_body(server_url, {"Host": "evil.example"}) == 421


def test_foreign_origin_is_rejected(server_url: str) -> None:
    # Guards DNS rebinding protection: a web page from another origin must not reach MCP.
    assert post_empty_body(server_url, {"Origin": "https://evil.example"}) == 403


def test_public_host_is_accepted(public_server_url: str) -> None:
    assert post_empty_body(public_server_url, {"Host": PUBLIC_HOST}) != 421


def test_public_host_still_rejects_foreign_host(public_server_url: str) -> None:
    # Guards DNS rebinding protection: allowing the public host must not allow any host.
    assert post_empty_body(public_server_url, {"Host": "evil.example"}) == 421


def test_public_host_still_rejects_foreign_origin(public_server_url: str) -> None:
    # Guards DNS rebinding protection: allowing the public host must not allow any origin.
    assert post_empty_body(public_server_url, {"Origin": "https://evil.example"}) == 403
