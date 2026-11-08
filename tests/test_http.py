import io
import json
import logging
import sys
import time
from collections.abc import Callable, Iterator

import pytest
from fastmcp import Client
from helpers import run_app, send
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mattermost_mcp.server import LOG_LEVELS, configure_logging, create_app

PUBLIC_HOST = "mcp.example.test"
BEHIND_PROXY = {
    "Host": PUBLIC_HOST,
    "Origin": f"https://{PUBLIC_HOST}",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": "203.0.113.9",
}


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


def create_crashing_app() -> Starlette:
    async def crash(request: Request) -> Response:
        raise RuntimeError("crash")

    app = create_app()
    app.router.routes.append(Route("/crash", crash))
    return app


@pytest.fixture
def crashing_server_url() -> Iterator[str]:
    yield from run_app(create_crashing_app())


@pytest.fixture
def production_log(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    # The handler binds to stdout when it is created, and the server thread writes to it later.
    # The loggers are global and caplog needs them to propagate, so they are restored afterwards.
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    loggers = [logging.getLogger(name) for name in LOG_LEVELS]
    saved = [(logger, logger.handlers[:], logger.level, logger.propagate) for logger in loggers]
    configure_logging()
    yield stream
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture
def crashing_server_url_with_production_log(production_log: io.StringIO) -> Iterator[str]:
    # Logging is configured first, as in main(), so a server that overwrites it fails the test.
    yield from run_app(create_crashing_app())


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


def test_unhandled_error_is_logged_as_500(
    crashing_server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    status, _ = send("GET", crashing_server_url.removesuffix("/mcp") + "/crash")

    assert status == 500
    [line] = read_request_log()
    assert "status=500 " in line


def test_exception_is_logged_on_one_line(
    crashing_server_url_with_production_log: str, production_log: io.StringIO
) -> None:
    # The case M5 left open: uvicorn's "Exception in ASGI application" with its traceback.
    url = crashing_server_url_with_production_log.removesuffix("/mcp") + "/crash"
    status, _ = send("GET", url)
    assert status == 500

    # uvicorn logs the exception just after the client has its response.
    deadline = time.monotonic() + 5
    while "RuntimeError" not in production_log.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)

    lines = production_log.getvalue().splitlines()
    [line] = [line for line in lines if "RuntimeError" in line]
    assert " uvicorn.error " in line
    assert "Traceback (most recent call last):\\n" in line
    assert "RuntimeError: crash" in line
    assert not [line for line in lines if line.startswith("  File")]


@pytest.mark.parametrize("name", LOG_LEVELS)
def test_every_configured_logger_keeps_a_traceback_on_one_line(
    name: str, production_log: io.StringIO
) -> None:
    try:
        raise ValueError("first\nsecond")
    except ValueError:
        logging.getLogger(name).exception("failed")

    [line] = production_log.getvalue().splitlines()
    assert "ValueError: first\\nsecond" in line


def test_container_health_check_is_not_logged(
    server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    base_url = server_url.removesuffix("/mcp")
    status, _ = send("GET", base_url + "/health")
    assert status == 200

    # read_request_log waits for a line, so give it one that proves the log is working.
    send("GET", base_url + "/marker")
    [line] = read_request_log()
    assert 'path="/marker"' in line


def test_health_check_through_the_proxy_is_logged(
    public_server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    # The deploy job's check comes through Caddy, so it has the caller's address.
    status, _ = send("GET", public_server_url.removesuffix("/mcp") + "/health", BEHIND_PROXY)
    assert status == 200

    [line] = read_request_log()
    assert 'path="/health"' in line
    assert "client=203.0.113.9 " in line


def test_failing_health_check_from_loopback_is_logged(
    server_url: str, read_request_log: Callable[[], list[str]]
) -> None:
    status, _ = send("GET", server_url.removesuffix("/mcp") + "/health", {"Host": "evil.example"})
    assert status == 421

    [line] = read_request_log()
    assert 'path="/health"' in line
    assert "status=421 " in line


def test_mcp_method_is_logged(server_url: str, read_request_log: Callable[[], list[str]]) -> None:
    post_empty_body(server_url, {"Mcp-Method": "tools/call"})

    [line] = read_request_log()
    assert 'mcp_method="tools/call"' in line
