import asyncio
import base64
import json
import logging
import sqlite3
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from helpers import run_app, send
from standardwebhooks import Webhook

import mattermost_mcp.callbacks as callbacks
import mattermost_mcp.events_cli as events_cli
import mattermost_mcp.server as server
from mattermost_mcp.callbacks import (
    CallbackFailure,
    EventsRuntime,
    UrlRefused,
    callback_hosts_from_env,
    check_callback_url,
    deliver_to_all,
    iso_utc,
    max_subscriptions_from_env,
    pick_public_address,
)
from mattermost_mcp.events import EVENT_NAME, subscription_id
from mattermost_mcp.server import create_app, runtime_from_env
from mattermost_mcp.store import Store, StoreError, Subscription
from mattermost_mcp.webhooks import InvalidSecret, parse_secret


def make_secret(size: int = 32, fill: bytes = b"k") -> str:
    return "whsec_" + base64.b64encode(fill * size).decode()


HOST = "connectors.api.openai.com"
PRIVATE_PATH = "/hooks/private-path-123"
URL = f"https://{HOST}{PRIVATE_PATH}"
SECRET = make_secret()
OTHER_SECRET = make_secret(fill=b"z")
PUBLIC_ADDRESS = "8.8.8.8"
HOUR = 3600.0

INVALID_PARAMS = -32602
UNKNOWN_EVENT = -32011
LIMIT_EXCEEDED = -32013
UNSUPPORTED_DELIVERY = -32014
CALLBACK_ENDPOINT_ERROR = -32015

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


class Clock:
    def __init__(self) -> None:
        # Starts at the real time, because standardwebhooks checks a window around it.
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


type Step = Callable[[httpx2.Request], Awaitable[httpx2.Response]]


class BodyStream(httpx2.AsyncByteStream):
    """A response body that has to be read, like one from the network, in small chunks."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.content), 1024):
            yield self.content[start : start + 1024]


def streamed(status: int, payload: object = None, content: bytes = b"", **kwargs: Any) -> Any:
    body = content if payload is None else json.dumps(payload).encode()
    return httpx2.Response(status, stream=BodyStream(body), **kwargs)


def answer(status: int, payload: object = None, content: bytes = b"", **kwargs: Any) -> Step:
    async def step(request: httpx2.Request) -> httpx2.Response:
        return streamed(status, payload, content, **kwargs)

    return step


def fail_with(error: type[httpx2.TransportError]) -> Step:
    async def step(request: httpx2.Request) -> httpx2.Response:
        raise error("fake failure", request=request)

    return step


async def correct_but_too_long(request: httpx2.Request) -> httpx2.Response:
    challenge = json.loads(request.content)["challenge"]
    return streamed(200, {"challenge": challenge, "padding": "y" * 5120})


async def trickle(request: httpx2.Request) -> httpx2.Response:
    await asyncio.sleep(5)
    return streamed(200)


class FakeChatGPT:
    """Answers like ChatGPT, unless a test scripts the next answers."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.script: list[Step] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.script:
            return await self.script.pop(0)(request)
        body = json.loads(request.content)
        if body.get("type") == "verification":
            return streamed(200, {"challenge": body["challenge"]})
        return streamed(200)


@dataclass
class World:
    runtime: EventsRuntime
    chatgpt: FakeChatGPT
    clock: Clock
    addresses: list[str]
    resolved: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)


@pytest.fixture
def world(tmp_path: Path) -> World:
    chatgpt = FakeChatGPT()
    clock = Clock()
    new = World(
        runtime=EventsRuntime(store=Store(tmp_path / "events.sqlite3")),
        chatgpt=chatgpt,
        clock=clock,
        addresses=[PUBLIC_ADDRESS],
    )

    async def resolve(host: str, port: int) -> list[str]:
        new.resolved.append(host)
        return new.addresses

    async def sleep(seconds: float) -> None:
        new.sleeps.append(seconds)
        clock.advance(seconds)

    new.runtime = replace(
        new.runtime,
        resolver=resolve,
        transport=httpx2.MockTransport(chatgpt),
        clock=clock,
        sleep=sleep,
        callback_hosts=frozenset({HOST}),
        max_subscriptions=5,
    )
    return new


@pytest.fixture
def mcp_url(world: World) -> Iterator[str]:
    yield from run_app(create_app(events=world.runtime))


def rpc(url: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one JSON-RPC request the way ChatGPT does: `_meta` envelope and Mcp-Method header."""
    # The status is not checked: the SDK answers invalid params with HTTP 400 and a JSON-RPC error.
    _, body = send(
        "POST",
        url,
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Method": method,
            "Mcp-Protocol-Version": "2026-07-28",
        },
        body=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": {"_meta": META, **(params or {})},
            }
        ),
    )
    return json.loads(body)


def subscribe_params(url: str = URL, secret: object = SECRET, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": EVENT_NAME,
        "arguments": {},
        "delivery": {"mode": "webhook", "url": url, "secret": secret},
        "cursor": None,
    }
    return params | overrides


def subscribe(mcp_url: str, url: str = URL, secret: object = SECRET, **overrides: Any) -> Any:
    return rpc(mcp_url, "events/subscribe", subscribe_params(url, secret, **overrides))


def assert_error(response: dict[str, Any], code: int, data: dict[str, str] | None = None) -> None:
    assert "result" not in response
    assert response["error"]["code"] == code
    if data is not None:
        assert response["error"]["data"] == data


def callback_url(number: int) -> str:
    return f"https://{HOST}/hooks/{number}"


def seed(world: World, url: str = URL, secret: str = SECRET, **overrides: Any) -> Subscription:
    """Put an active, verified subscription straight into the store."""
    now = world.clock()
    subscription = Subscription(
        id=subscription_id("anonymous", url, EVENT_NAME, {}),
        principal="anonymous",
        event_name=EVENT_NAME,
        arguments="{}",
        callback_url=url,
        secret=secret,
        previous_secret=None,
        previous_secret_until=None,
        created_at=now,
        refreshed_at=now,
        expires_at=now + HOUR,
        verified_at=now,
        last_delivery_at=None,
        last_delivery_status=None,
    )
    subscription = replace(subscription, **overrides)
    assert world.runtime.store.save(subscription, now, 1000)
    return subscription


def send_event(world: World, text: str = "hello") -> list[callbacks.DeliveryResult]:
    return asyncio.run(deliver_to_all(EVENT_NAME, {"text": text}, world.runtime))


def active(world: World) -> list[Subscription]:
    return world.runtime.store.list_active(world.clock())


# Capabilities


def test_discover_advertises_events(mcp_url: str) -> None:
    capabilities = rpc(mcp_url, "server/discover")["result"]["capabilities"]

    assert capabilities["events"] == {}


def test_initialize_advertises_events(mcp_url: str) -> None:
    status, body = send(
        "POST",
        mcp_url,
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        body=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        ),
    )

    assert status == 200
    data = next(line for line in body.decode().splitlines() if line.startswith("data:"))
    assert json.loads(data.removeprefix("data:"))["result"]["capabilities"]["events"] == {}


def test_events_list_returns_the_definition(mcp_url: str) -> None:
    assert rpc(mcp_url, "events/list")["result"]["events"] == [
        {
            "name": "test.message",
            "description": (
                "A test message that the owner of this server sends by hand from the server, "
                "to try out event delivery. It carries only the text the owner typed. It has "
                "no filters."
            ),
            "delivery": ["webhook"],
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "payloadSchema": {
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 1000}},
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    ]


# Subscribe


def test_subscribe_verifies_the_callback_and_stores_the_subscription(
    mcp_url: str, world: World
) -> None:
    result = subscribe(mcp_url)["result"]

    expected_id = subscription_id("anonymous", URL, EVENT_NAME, {})
    assert result["id"] == expected_id
    assert result["id"].startswith("sub_")
    assert len(result["id"]) == 36
    assert result["refreshBefore"] == iso_utc(world.clock() + HOUR)
    assert result["refreshBefore"].endswith("Z")
    assert result["cursor"] is None
    assert result["truncated"] is False

    [request] = world.chatgpt.requests
    body = json.loads(request.content)
    assert set(body) == {"type", "challenge"}
    assert body["type"] == "verification"
    assert request.headers["webhook-id"].startswith("msg_verification_")
    assert request.headers["x-mcp-subscription-id"] == expected_id
    Webhook(SECRET).verify(request.content, dict(request.headers))

    [stored] = active(world)
    assert stored.id == expected_id
    assert stored.secret == SECRET
    assert stored.verified_at is not None


def test_connection_is_pinned_to_the_checked_address(mcp_url: str, world: World) -> None:
    subscribe(mcp_url)

    [request] = world.chatgpt.requests
    assert request.url.host == PUBLIC_ADDRESS
    assert request.url.path == PRIVATE_PATH
    assert request.headers["host"] == HOST
    assert request.extensions["sni_hostname"] == HOST


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"name": "other.event"}, UNKNOWN_EVENT),
        ({"name": 5}, INVALID_PARAMS),
        ({"arguments": {"filter": "x"}}, INVALID_PARAMS),
        ({"arguments": None}, INVALID_PARAMS),
        ({"delivery": "whsec_not-an-object"}, INVALID_PARAMS),
        ({"delivery": {"mode": "poll", "url": URL, "secret": SECRET}}, UNSUPPORTED_DELIVERY),
        ({"ttlMs": "soon"}, INVALID_PARAMS),
        ({"ttlMs": True}, INVALID_PARAMS),
    ],
)
def test_invalid_subscribe_params_are_refused(
    mcp_url: str, world: World, overrides: dict[str, Any], code: int
) -> None:
    assert_error(rpc(mcp_url, "events/subscribe", subscribe_params(**overrides)), code)
    assert world.chatgpt.requests == []
    assert active(world) == []


def test_unknown_fields_are_ignored(mcp_url: str) -> None:
    assert "result" in subscribe(mcp_url, maxAgeMs=1000, somethingNew={"a": 1})


def test_arguments_default_to_empty(mcp_url: str) -> None:
    params = subscribe_params()
    del params["arguments"]

    assert "result" in rpc(mcp_url, "events/subscribe", params)


def test_error_does_not_echo_the_secret(mcp_url: str) -> None:
    response = rpc(mcp_url, "events/subscribe", subscribe_params(delivery=SECRET))

    assert SECRET not in json.dumps(response)


# Verification failures


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        (answer(200, {"challenge": "wrong"}), "challenge_failed"),
        (answer(200, content=b"not json"), "challenge_failed"),
        (answer(200, ["challenge"]), "challenge_failed"),
        (answer(200, {"challenge": "é"}), "challenge_failed"),
        (answer(200, content=b"[" * 4000), "challenge_failed"),
        (correct_but_too_long, "challenge_failed"),
        (answer(500, {"challenge": "x"}), "bad_status"),
        (answer(302, headers={"Location": "https://evil.example/"}), "bad_status"),
        (fail_with(httpx2.ReadTimeout), "timeout"),
        (fail_with(httpx2.ConnectError), "unreachable"),
    ],
)
def test_failed_verification_is_refused(
    mcp_url: str, world: World, step: Step, reason: str
) -> None:
    world.chatgpt.script.append(step)

    response = subscribe(mcp_url)

    assert_error(response, CALLBACK_ENDPOINT_ERROR, {"reason": reason})
    # One request only: in particular, the 302 is not followed.
    assert len(world.chatgpt.requests) == 1
    assert active(world) == []


def test_the_callbacks_answer_is_not_in_the_error(mcp_url: str, world: World) -> None:
    world.chatgpt.script.append(answer(500, content=b"secret-looking-answer"))

    assert "secret-looking-answer" not in json.dumps(subscribe(mcp_url))


def test_one_deadline_covers_the_whole_attempt(
    mcp_url: str, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(callbacks, "REQUEST_TIMEOUT_SECONDS", 0.05)
    world.chatgpt.script.append(trickle)

    assert_error(subscribe(mcp_url), CALLBACK_ENDPOINT_ERROR, {"reason": "timeout"})


# URL policy


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/hook",
        "https://other.example/hook",
        f"https://{HOST}.evil.example/hook",
        f"https://user:password@{HOST}/hook",
        f"https://user@{HOST}/hook",
        f"https://{HOST}:8443/hook",
        "https://8.8.8.8/hook",
        f"https://{HOST}/hook#fragment",
        f"https://{HOST}/hook#",
        f"https://{HOST}/" + "a" * (2049 - len(f"https://{HOST}/")),
        f"https://{HOST}\t.evil.example/hook",
        f"https://evil.example\\@{HOST}/hook",
        f"https://{HOST}/hook\n",
        f"https://{HOST}/é",
        f"https://{HOST}:notaport/hook",
        "https://[broken/hook",
        "not a url",
    ],
)
def test_url_policy_refuses(mcp_url: str, world: World, url: str) -> None:
    assert_error(subscribe(mcp_url, url), INVALID_PARAMS)
    assert world.chatgpt.requests == []
    assert world.resolved == []


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST.upper()}/hook",
        f"https://{HOST}./hook",
        f"https://{HOST}:443/hook",
        f"https://{HOST}/" + "a" * (2048 - len(f"https://{HOST}/")),
    ],
)
def test_url_policy_accepts(mcp_url: str, world: World, url: str) -> None:
    assert "result" in subscribe(mcp_url, url)

    [request] = world.chatgpt.requests
    assert world.resolved == [HOST]
    assert request.headers["host"] == HOST
    assert request.extensions["sni_hostname"] == HOST


def test_refused_host_is_logged(mcp_url: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="mattermost_mcp.events")

    subscribe(mcp_url, "https://other.example/private-path")

    assert 'callback_host="other.example"' in caplog.text
    assert "private-path" not in caplog.text


def test_non_string_url_is_refused() -> None:
    with pytest.raises(UrlRefused):
        check_callback_url(None, frozenset({HOST}))


@pytest.mark.parametrize("value", ["", " , ,"])
def test_empty_host_setting_means_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MCP_EVENTS_CALLBACK_HOSTS", value)

    assert callback_hosts_from_env() == frozenset({HOST})


def test_host_setting_replaces_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EVENTS_CALLBACK_HOSTS", "A.example., b.example")

    assert callback_hosts_from_env() == frozenset({"a.example", "b.example"})


def test_wildcard_in_the_host_setting_matches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EVENTS_CALLBACK_HOSTS", "*")

    with pytest.raises(UrlRefused):
        check_callback_url("https://evil.example/hook", callback_hosts_from_env())


def test_invalid_cap_setting_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EVENTS_MAX_SUBSCRIPTIONS", "0")

    with pytest.raises(ValueError, match="at least 1"):
        max_subscriptions_from_env()


# Address check


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.1"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fd00::1"],
        ["100.64.0.1"],
        ["::ffff:127.0.0.1"],
        ["203.0.113.5"],
        [PUBLIC_ADDRESS, "10.0.0.1"],
        ["10.0.0.1", PUBLIC_ADDRESS],
        ["not-an-address"],
    ],
)
def test_non_public_address_is_refused(mcp_url: str, world: World, addresses: list[str]) -> None:
    world.addresses = addresses

    assert_error(subscribe(mcp_url), CALLBACK_ENDPOINT_ERROR, {"reason": "blocked_address"})
    assert world.chatgpt.requests == []
    assert active(world) == []


def test_host_without_addresses_is_unreachable() -> None:
    with pytest.raises(CallbackFailure, match="unreachable"):
        pick_public_address([])


def test_first_checked_address_is_used() -> None:
    assert pick_public_address([PUBLIC_ADDRESS, "1.1.1.1"]) == PUBLIC_ADDRESS


# Secrets


@pytest.mark.parametrize(
    "secret",
    [
        base64.b64encode(b"k" * 32).decode(),
        "whsec_not*base64!",
        make_secret(23),
        make_secret(65),
        None,
        "",
        "whsec_",
    ],
)
def test_invalid_secret_is_refused(mcp_url: str, world: World, secret: object) -> None:
    assert_error(subscribe(mcp_url, secret=secret), INVALID_PARAMS)
    assert world.chatgpt.requests == []


@pytest.mark.parametrize("size", [24, 64])
def test_secret_sizes_at_the_limits_are_accepted(size: int) -> None:
    assert parse_secret(make_secret(size)) == b"k" * size


def test_missing_base64_padding_is_added() -> None:
    assert parse_secret(make_secret(32).rstrip("=")) == b"k" * 32


def test_secret_error_does_not_contain_the_secret() -> None:
    with pytest.raises(InvalidSecret) as caught:
        parse_secret("whsec_not*base64!")

    assert "not*base64" not in str(caught.value)
    assert caught.value.__cause__ is None


# Idempotency and refresh


def test_id_does_not_depend_on_argument_key_order() -> None:
    assert subscription_id("p", URL, "e", {"a": 1, "b": 2}) == subscription_id(
        "p", URL, "e", {"b": 2, "a": 1}
    )


def test_subscribing_twice_is_one_subscription_verified_once(mcp_url: str, world: World) -> None:
    first = subscribe(mcp_url)["result"]
    world.clock.advance(60)
    second = subscribe(mcp_url)["result"]

    assert first["id"] == second["id"]
    assert second["refreshBefore"] == iso_utc(world.clock() + HOUR)
    assert len(active(world)) == 1
    assert len(world.chatgpt.requests) == 1


def test_refresh_after_24_hours_verifies_again(mcp_url: str, world: World) -> None:
    seed(world, verified_at=world.clock() - 24 * HOUR)

    assert "result" in subscribe(mcp_url)

    assert len(world.chatgpt.requests) == 1


def test_refresh_with_a_new_secret_signs_with_both_for_an_hour(mcp_url: str, world: World) -> None:
    subscribe(mcp_url)
    assert "result" in subscribe(mcp_url, secret=OTHER_SECRET)
    [stored] = active(world)
    assert stored.secret == OTHER_SECRET
    assert stored.previous_secret == SECRET

    send_event(world)
    delivery = world.chatgpt.requests[-1]
    assert len(delivery.headers["webhook-signature"].split(" ")) == 2
    Webhook(OTHER_SECRET).verify(delivery.content, dict(delivery.headers))
    Webhook(SECRET).verify(delivery.content, dict(delivery.headers))

    # Refreshing half-way keeps the subscription alive past the end of the overlap.
    world.clock.advance(HOUR / 2)
    subscribe(mcp_url, secret=OTHER_SECRET)
    world.clock.advance(HOUR / 2 + 1)

    send_event(world)
    assert len(world.chatgpt.requests[-1].headers["webhook-signature"].split(" ")) == 1


def test_failed_reverification_keeps_the_old_subscription(mcp_url: str, world: World) -> None:
    seed(world, verified_at=world.clock() - 24 * HOUR)
    world.chatgpt.script.append(answer(500))

    assert_error(subscribe(mcp_url, secret=OTHER_SECRET), CALLBACK_ENDPOINT_ERROR)

    [stored] = active(world)
    assert stored.secret == SECRET


# Limits


def test_sixth_subscription_is_refused_and_refresh_at_the_cap_works(
    mcp_url: str, world: World
) -> None:
    for number in range(5):
        assert "result" in subscribe(mcp_url, callback_url(number))

    requests_before = len(world.chatgpt.requests)
    assert_error(subscribe(mcp_url, callback_url(5)), LIMIT_EXCEEDED, {"limit": "subscriptions"})
    assert len(world.chatgpt.requests) == requests_before

    assert "result" in subscribe(mcp_url, callback_url(0))
    assert len(active(world)) == 5


def test_expired_rows_free_the_cap(mcp_url: str, world: World) -> None:
    for number in range(5):
        seed(world, callback_url(number))
    world.clock.advance(HOUR + 1)

    assert "result" in subscribe(mcp_url, callback_url(5))
    assert len(active(world)) == 1


def test_expired_row_is_not_refreshed_past_the_cap(mcp_url: str, world: World) -> None:
    for number in range(5):
        seed(world, callback_url(number))
    seed(world, callback_url(5), expires_at=world.clock() - 1)

    response = subscribe(mcp_url, callback_url(5))

    assert_error(response, LIMIT_EXCEEDED, {"limit": "subscriptions"})
    assert len(active(world)) == 5


def test_cap_is_checked_again_when_the_row_is_written(world: World) -> None:
    # What two concurrent subscribes look like once both have passed verification.
    store, now = world.runtime.store, world.clock()
    template = seed(world, callback_url(0))
    store.delete(template.id)

    assert store.save(replace(template, id="sub_a"), now, 1)
    assert not store.save(replace(template, id="sub_b"), now, 1)
    assert store.save(replace(template, id="sub_a"), now, 1)


def test_verification_rate_limit(mcp_url: str, world: World) -> None:
    world.runtime.max_subscriptions = 100
    for number in range(10):
        assert "result" in subscribe(mcp_url, callback_url(number))

    assert_error(
        subscribe(mcp_url, callback_url(10)), LIMIT_EXCEEDED, {"limit": "verification_rate"}
    )
    assert len(world.chatgpt.requests) == 10

    world.clock.advance(61)
    assert "result" in subscribe(mcp_url, callback_url(10))


@pytest.mark.parametrize(
    ("overrides", "granted"),
    [
        ({}, HOUR),
        ({"ttlMs": None}, HOUR),
        ({"ttlMs": 60_000}, 600.0),
        ({"ttlMs": 36_000_000}, HOUR),
        ({"ttlMs": 1_800_000}, 1800.0),
        ({"ttlMs": 10**400}, HOUR),
        ({"ttlMs": -5}, 600.0),
    ],
)
def test_ttl_is_clamped(
    mcp_url: str, world: World, overrides: dict[str, Any], granted: float
) -> None:
    result = subscribe(mcp_url, **overrides)["result"]

    assert result["refreshBefore"] == iso_utc(world.clock() + granted)


def test_a_huge_requested_lifetime_can_still_be_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="mattermost_mcp.events")

    callbacks.log_event("subscribe", requested_ttl_ms=10**5000)

    assert "requested_ttl_ms=out_of_range" in caplog.text


# Unsubscribe


def unsubscribe_params(url: object = URL, name: object = EVENT_NAME) -> dict[str, Any]:
    return {"name": name, "arguments": {}, "delivery": {"mode": "webhook", "url": url}}


def test_unsubscribe_twice_answers_empty_both_times(mcp_url: str, world: World) -> None:
    subscribe(mcp_url)

    assert rpc(mcp_url, "events/unsubscribe", unsubscribe_params())["result"].get("id") is None
    assert active(world) == []
    response = rpc(mcp_url, "events/unsubscribe", unsubscribe_params())
    assert "error" not in response


def test_unsubscribe_unknown_event_is_an_error(mcp_url: str) -> None:
    response = rpc(mcp_url, "events/unsubscribe", unsubscribe_params(name="other.event"))

    assert_error(response, UNKNOWN_EVENT)


def test_unsubscribe_works_for_a_host_no_longer_allowed(mcp_url: str, world: World) -> None:
    subscribe(mcp_url)
    world.runtime.callback_hosts = frozenset({"other.example"})

    rpc(mcp_url, "events/unsubscribe", unsubscribe_params())

    assert active(world) == []


@pytest.mark.parametrize("url", [None, 5, "not a url"])
def test_unsubscribe_with_a_bad_url_answers_empty(mcp_url: str, url: object) -> None:
    assert "error" not in rpc(mcp_url, "events/unsubscribe", unsubscribe_params(url=url))


# Storage


def test_new_store_on_the_same_file_sees_the_subscription(tmp_path: Path, world: World) -> None:
    seed(world)

    assert [row.id for row in Store(world.runtime.store.path).list_active(world.clock())] == [
        subscription_id("anonymous", URL, EVENT_NAME, {})
    ]


def test_database_file_settings(world: World) -> None:
    path = world.runtime.store.path
    seed(world)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    finally:
        connection.close()


def test_existing_file_with_a_wider_mode_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    path.touch(mode=0o644)

    Store(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_schema_version_fails(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(StoreError, match="schema version 99"):
        Store(path)


def test_unusable_folder_fails_fast(tmp_path: Path) -> None:
    (tmp_path / "file").touch()

    with pytest.raises(StoreError, match="cannot use the database"):
        Store(tmp_path / "file" / "events.sqlite3")


def test_server_refuses_to_start_without_a_usable_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "file").touch()
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path / "file"))
    monkeypatch.setattr(server, "configure_logging", lambda: None)

    with pytest.raises(SystemExit, match="cannot use the database"):
        server.main()


def test_data_dir_setting_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path / "data"))

    assert runtime_from_env().store.path == tmp_path / "data" / "events.sqlite3"


def test_expired_subscriptions_are_neither_listed_nor_delivered(world: World) -> None:
    seed(world)
    world.clock.advance(HOUR + 1)

    assert active(world) == []
    assert send_event(world) == []
    assert world.chatgpt.requests == []


# Delivery


def test_delivery_is_signed_and_has_the_expected_shape(world: World) -> None:
    subscription = seed(world)

    [result] = send_event(world, "héllo")

    assert result.delivered
    assert result.status == 200
    assert result.attempts == 1

    [request] = world.chatgpt.requests
    # verify() recomputes the signature over request.content, the bytes that were sent.
    Webhook(SECRET).verify(request.content, dict(request.headers))
    body = json.loads(request.content)
    assert set(body) == {"eventId", "name", "timestamp", "data", "cursor"}
    assert body["eventId"].startswith("evt_")
    assert request.headers["webhook-id"] == body["eventId"]
    assert request.headers["webhook-timestamp"] == str(int(world.clock()))
    assert request.headers["x-mcp-subscription-id"] == subscription.id
    assert request.headers["content-type"] == "application/json"
    assert body["name"] == "test.message"
    assert body["timestamp"] == iso_utc(world.clock())
    assert body["data"] == {"text": "héllo"}
    assert body["cursor"] is None
    assert request.url.host == PUBLIC_ADDRESS
    assert request.headers["host"] == HOST
    assert active(world)[0].last_delivery_status == "200"


def test_retry_keeps_the_event_id_and_renews_the_signature(world: World) -> None:
    seed(world)
    world.chatgpt.script.append(answer(503))

    [result] = send_event(world)

    assert result.delivered
    assert result.attempts == 2
    assert world.sleeps == [1.0]
    first, second = world.chatgpt.requests
    assert first.headers["webhook-id"] == second.headers["webhook-id"]
    assert first.content == second.content
    assert first.headers["webhook-signature"] != second.headers["webhook-signature"]
    assert int(second.headers["webhook-timestamp"]) > int(first.headers["webhook-timestamp"])
    Webhook(SECRET).verify(second.content, dict(second.headers))


def test_three_attempts_at_most(world: World) -> None:
    seed(world)
    world.chatgpt.script.extend([answer(500), answer(502), answer(503), answer(200)])

    [result] = send_event(world)

    assert not result.delivered
    assert result.status == 503
    assert result.attempts == 3
    assert world.sleeps == [1.0, 4.0]
    assert active(world)[0].last_delivery_status == "503"


@pytest.mark.parametrize(
    "step",
    [
        answer(408),
        answer(429),
        fail_with(httpx2.ReadTimeout),
        fail_with(httpx2.ConnectError),
    ],
)
def test_transient_failures_are_retried(world: World, step: Step) -> None:
    seed(world)
    world.chatgpt.script.append(step)

    [result] = send_event(world)

    assert result.delivered
    assert result.attempts == 2


@pytest.mark.parametrize("status", [413, 400, 401, 404, 302])
def test_other_statuses_are_not_retried(world: World, status: int) -> None:
    seed(world)
    world.chatgpt.script.append(answer(status))

    [result] = send_event(world)

    assert not result.delivered
    assert result.attempts == 1
    assert len(active(world)) == 1


def test_410_deletes_the_subscription(world: World) -> None:
    seed(world)
    world.chatgpt.script.append(answer(410))

    [result] = send_event(world)

    assert not result.delivered
    assert result.attempts == 1
    assert active(world) == []


def test_delivery_checks_the_address_again_on_each_attempt(world: World) -> None:
    seed(world)
    world.chatgpt.script.append(answer(503))

    send_event(world)

    assert world.resolved == [HOST, HOST]


def test_delivery_to_a_private_address_is_not_sent_or_retried(world: World) -> None:
    seed(world)
    world.addresses = ["10.0.0.1"]

    [result] = send_event(world)

    assert not result.delivered
    assert result.attempts == 1
    assert world.chatgpt.requests == []
    assert active(world)[0].last_delivery_status == "blocked_address"


def test_delivery_applies_the_url_policy_again(world: World) -> None:
    seed(world)
    world.runtime.callback_hosts = frozenset({"other.example"})

    [result] = send_event(world)

    assert not result.delivered
    assert result.attempts == 0
    assert world.chatgpt.requests == []
    assert world.resolved == []


# Log hygiene


def test_logs_never_contain_the_secret_challenge_or_url_path(
    mcp_url: str, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    subscribe(mcp_url)
    subscribe(mcp_url, secret=OTHER_SECRET)
    send_event(world)
    rpc(mcp_url, "events/unsubscribe", unsubscribe_params())
    time.sleep(0.05)

    challenge = json.loads(world.chatgpt.requests[0].content)["challenge"]
    event_lines = [r.getMessage() for r in caplog.records if r.name == "mattermost_mcp.events"]
    actions = [line.split(" ")[0] for line in event_lines]
    assert actions == [
        "action=verification",
        "action=subscribe",
        "action=refresh",
        "action=delivery",
        "action=unsubscribe",
    ]
    assert all("\n" not in line for line in event_lines)
    assert 'callback_host="connectors.api.openai.com"' in event_lines[0]
    assert "granted_seconds=3600.0" in event_lines[1]
    assert "status=200 attempt=1" in event_lines[3]

    for value in (SECRET, OTHER_SECRET, SECRET.removeprefix("whsec_"), challenge, PRIVATE_PATH):
        assert value not in caplog.text


# The command


def test_cli_list_shows_the_summary_but_never_the_secret_or_url(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    subscription = seed(world)

    assert events_cli.run(["list"], world.runtime) == 0

    output = capsys.readouterr().out
    assert subscription.id in output
    assert f"host={HOST}" in output
    assert callbacks.url_fingerprint(URL) in output
    assert "expires_in_min=60" in output or "expires_in_min=59" in output
    assert "verified=yes" in output
    assert SECRET not in output
    assert PRIVATE_PATH not in output


def test_cli_list_without_subscriptions(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert events_cli.run(["list"], world.runtime) == 0

    assert "no active subscriptions" in capsys.readouterr().out


def test_cli_send_test_prints_one_line_each_and_succeeds(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    subscription = seed(world)

    assert events_cli.run(["send-test", "hello"], world.runtime) == 0

    assert capsys.readouterr().out.splitlines() == [f"{subscription.id} status=200 attempts=1"]


def test_cli_send_test_without_subscriptions_exits_1(world: World) -> None:
    assert events_cli.run(["send-test", "hello"], world.runtime) == 1


def test_cli_send_test_with_a_failed_delivery_exits_1(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    subscription = seed(world)
    world.chatgpt.script.append(answer(404))

    assert events_cli.run(["send-test", "hello"], world.runtime) == 1

    assert capsys.readouterr().out.splitlines() == [f"{subscription.id} status=404 attempts=1"]


def test_cli_send_test_refuses_long_text(world: World) -> None:
    seed(world)

    assert events_cli.run(["send-test", "x" * 1001], world.runtime) == 2
    assert world.chatgpt.requests == []


def test_cli_send_test_accepts_1000_characters(world: World) -> None:
    seed(world)

    assert events_cli.run(["send-test", "x" * 1000], world.runtime) == 0


def test_cli_remove_one(world: World) -> None:
    subscription = seed(world)

    assert events_cli.run(["remove", subscription.id], world.runtime) == 0
    assert active(world) == []
    assert events_cli.run(["remove", subscription.id], world.runtime) == 1


def test_cli_remove_all(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    seed(world, callback_url(1))
    seed(world, callback_url(2))

    assert events_cli.run(["remove", "--all"], world.runtime) == 0

    assert "removed 2" in capsys.readouterr().out
    assert active(world) == []


@pytest.mark.parametrize("argv", [["remove"], ["remove", "sub_x", "--all"]])
def test_cli_remove_needs_exactly_one_target(world: World, argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        events_cli.run(argv, world.runtime)
