import http.client
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit

import pytest
import uvicorn
from fastmcp import Client
from starlette.applications import Starlette

from mattermost_mcp.server import create_app, uvicorn_settings

PUBLIC_HOST = "mcp.example.test"
BEHIND_PROXY = {
    "Host": PUBLIC_HOST,
    "Origin": f"https://{PUBLIC_HOST}",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": "203.0.113.9",
}


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


@pytest.fixture
def server_url() -> Iterator[str]:
    yield from run_app(create_app())


@pytest.fixture
def public_server_url() -> Iterator[str]:
    yield from run_app(create_app(public_host=PUBLIC_HOST))


@pytest.fixture
def untrusted_client_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # The test client connects from 127.0.0.1, which this setting no longer trusts.
    monkeypatch.setenv("MCP_TRUSTED_PROXIES", "192.0.2.1")
    yield from run_app(create_app(public_host=PUBLIC_HOST))


@pytest.fixture
def read_request_log(caplog: pytest.LogCaptureFixture) -> Callable[[], list[str]]:
    caplog.set_level(logging.INFO, logger="mattermost_mcp.http")

    def read() -> list[str]:
        # The line is written just after the client has its response, so wait for it, then
        # a little longer so that a duplicate line would be seen too.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(record.name == "mattermost_mcp.http" for record in caplog.records):
                break
            time.sleep(0.01)
        time.sleep(0.05)
        return [r.getMessage() for r in caplog.records if r.name == "mattermost_mcp.http"]

    return read


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


def test_rejected_request_is_logged_once(
    server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    assert post_empty_body(server_url, {"Origin": "https://evil.example"}) == 403

    [line] = read_request_log()
    assert "status=403" in line
    assert "https://evil.example" in line


def test_credentials_are_not_logged(
    server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    post_empty_body(
        server_url, {"Authorization": "Bearer secret-123", "Cookie": "session=cookie-456"}
    )

    [line] = read_request_log()
    assert "secret-123" not in line
    assert "cookie-456" not in line


def test_path_cannot_break_the_log_line(
    server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    send("GET", server_url + "%0Astatus=200")

    [line] = read_request_log()
    assert "\n" not in line
    assert 'path="/mcp\\nstatus=200"' in line


def test_https_origin_is_accepted_through_trusted_proxy(
    public_server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    # The bug this guards against: behind Caddy the app saw http, so https origins got 403.
    assert post_empty_body(public_server_url, BEHIND_PROXY) != 403

    [line] = read_request_log()
    assert "client=203.0.113.9 " in line
    assert "scheme=https " in line


def test_proxy_headers_are_ignored_from_untrusted_address(
    untrusted_client_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    # Guards the trust boundary: anyone else must not be able to claim https or an address.
    assert post_empty_body(untrusted_client_url, BEHIND_PROXY) == 403

    [line] = read_request_log()
    assert "client=127.0.0.1 " in line
    assert "scheme=http " in line
