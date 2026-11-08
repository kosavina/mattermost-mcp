"""Running the real app over real HTTP, shared by the test modules."""

import http.client
import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
import uvicorn
from starlette.applications import Starlette

from mattermost_mcp.server import uvicorn_settings


def run_app(app: Starlette) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, **uvicorn_settings("127.0.0.1", port)))
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
