"""Outbound webhook calls: which URLs we may call, how we connect, and how we retry.

The fence for M6 lives here. There is deliberately no setting that switches off the URL policy
or the address check, not even for tests: tests inject a fake resolver instead.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx2

from mattermost_mcp.request_log import quoted
from mattermost_mcp.store import Store, Subscription
from mattermost_mcp.webhooks import build_headers, parse_secret, serialise

logger = logging.getLogger("mattermost_mcp.events")

# The HTTP client logs every full URL at INFO and its headers at DEBUG, and the path of a callback
# URL works like a password. Pinned here, so that no change of the log level can reveal it.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)

DEFAULT_CALLBACK_HOSTS = frozenset({"connectors.api.openai.com"})
DEFAULT_MAX_SUBSCRIPTIONS = 5
MAX_URL_LENGTH = 2048
HTTPS_PORT = 443
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_VERIFICATION_ANSWER_BYTES = 4096
MAX_DELIVERY_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (1.0, 4.0)
MAX_LOGGED_TEXT = 253
MAX_LOGGED_NUMBER = 10**15

# Printable ASCII without space or backslash. urlsplit silently deletes tabs and newlines and
# treats a backslash as an ordinary character, so a URL holding them can mean one thing to the
# policy below and another to the HTTP client.
SAFE_URL = re.compile(r"[!-\[\]-~]+")

type Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    """Return every address the host resolves to."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def normalise_host(host: str) -> str:
    return host.strip().lower().removesuffix(".")


def callback_hosts_from_env() -> frozenset[str]:
    """Return MCP_EVENTS_CALLBACK_HOSTS. Empty means the default: it can never widen the policy."""
    raw = os.environ.get("MCP_EVENTS_CALLBACK_HOSTS") or ""
    hosts = frozenset(filter(None, map(normalise_host, raw.split(","))))
    return hosts or DEFAULT_CALLBACK_HOSTS


def max_subscriptions_from_env() -> int:
    """Return MCP_EVENTS_MAX_SUBSCRIPTIONS, or raise ValueError so that startup fails."""
    raw = os.environ.get("MCP_EVENTS_MAX_SUBSCRIPTIONS") or ""
    if not raw:
        return DEFAULT_MAX_SUBSCRIPTIONS
    maximum = int(raw)
    if maximum < 1:
        raise ValueError("MCP_EVENTS_MAX_SUBSCRIPTIONS must be at least 1")
    return maximum


@dataclass
class EventsRuntime:
    """Everything the events code talks to, so that every test can run offline."""

    store: Store
    resolver: Resolver = resolve_host
    transport: httpx2.AsyncBaseTransport | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    callback_hosts: frozenset[str] = field(default_factory=callback_hosts_from_env)
    max_subscriptions: int = field(default_factory=max_subscriptions_from_env)
    verification_times: deque[float] = field(default_factory=deque)


def iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def url_fingerprint(url: str) -> str:
    """Identify a URL in logs without revealing its path, which works like a password."""
    return hashlib.sha256(url.encode(errors="replace")).hexdigest()[:12]


def callback_host(url: str) -> str | None:
    """Return the host of a stored or submitted URL, only for showing it to a person."""
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _logged(value: object) -> object:
    """Bound and quote text: much of it comes from the client."""
    if value is None:
        return "-"
    # Python refuses to print an integer of thousands of digits, and JSON can deliver one.
    if isinstance(value, int | float) and abs(value) > MAX_LOGGED_NUMBER:
        return "out_of_range"
    return quoted(value[:MAX_LOGGED_TEXT]) if isinstance(value, str) else value


def log_event(
    action: str,
    *,
    event: str | None = None,
    subscription_id: str | None = None,
    host: str | None = None,
    url: str | None = None,
    requested_ttl_ms: float | None = None,
    granted_seconds: float | None = None,
    outcome: str | None = None,
    status: int | None = None,
    attempt: int | None = None,
) -> None:
    """Write one event line. Secrets, challenges, full URLs and bodies have no parameter here."""
    logger.info(
        "action=%s event=%s subscription_id=%s callback_host=%s url_fingerprint=%s "
        "requested_ttl_ms=%s granted_seconds=%s outcome=%s status=%s attempt=%s",
        action,
        _logged(event),
        _logged(subscription_id),
        _logged(host),
        "-" if url is None else url_fingerprint(url),
        _logged(requested_ttl_ms),
        _logged(granted_seconds),
        _logged(outcome),
        _logged(status),
        _logged(attempt),
    )


class UrlRefused(ValueError):
    """The URL breaks the policy. `host` is the host we saw, if any, for the log."""

    def __init__(self, host: str | None = None) -> None:
        super().__init__("the callback URL is not allowed")
        self.host = host


@dataclass(frozen=True)
class CallbackTarget:
    """A URL that passed the policy. `host` is the normalised name for DNS, Host and SNI."""

    url: str = field(repr=False)
    host: str


def check_callback_url(url: object, allowed_hosts: frozenset[str]) -> CallbackTarget:
    """Return the target of an allowed URL, or raise UrlRefused."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH or not SAFE_URL.fullmatch(url):
        raise UrlRefused
    try:
        parts = urlsplit(url)
        port = parts.port
        httpx2.URL(url)
    except ValueError, httpx2.InvalidURL:
        raise UrlRefused from None
    host = normalise_host(parts.hostname or "")
    # "#" is tested on the URL, because urlsplit reports an empty fragment as no fragment.
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or "#" in url
        or port not in (None, HTTPS_PORT)
        or host not in allowed_hosts
    ):
        raise UrlRefused(host or None)
    return CallbackTarget(url=url, host=host)


class CallbackFailure(Exception):
    """An outbound request failed. `reason` is safe to log and to return to the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def pick_public_address(addresses: Iterable[str]) -> str:
    """Return the first address, if every one of them is public.

    One private address among public ones also refuses the host: a DNS answer that mixes them
    is what an attempt to reach our private network looks like.
    """
    checked = list(addresses)
    if not checked:
        raise CallbackFailure("unreachable")
    for address in checked:
        try:
            public = ipaddress.ip_address(address).is_global
        except ValueError:
            public = False
        if not public:
            raise CallbackFailure("blocked_address")
    return checked[0]


@dataclass(frozen=True)
class PostResult:
    status: int
    body: bytes | None
    """The answer, or None if it was longer than the limit."""


async def _read_capped(response: httpx2.Response, limit: int) -> bytes | None:
    if limit == 0:
        return b""
    received = bytearray()
    # aiter_raw, with identity requested below: a compressed answer can't get past the limit.
    async for chunk in response.aiter_raw():
        received += chunk
        if len(received) > limit:
            return None
    return bytes(received)


async def pinned_post(
    target: CallbackTarget,
    headers: dict[str, str],
    body: bytes,
    runtime: EventsRuntime,
    read_limit: int,
) -> PostResult:
    """POST to the checked address of the target, or raise CallbackFailure.

    Connecting to the address that was checked, rather than to the name, stops a DNS answer
    that changes between the check and the connection. Host and SNI keep the real name, so
    TLS still verifies the certificate for it.
    """
    try:
        # One deadline for the whole attempt: httpx timeouts apply per read, so an answer that
        # trickles in byte by byte would otherwise go on for as long as it likes.
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            address = pick_public_address(await runtime.resolver(target.host, HTTPS_PORT))
            # trust_env=False: no proxy, which would connect by name and undo the pinning.
            async with (
                httpx2.AsyncClient(
                    transport=runtime.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                ) as client,
                client.stream(
                    "POST",
                    httpx2.URL(target.url).copy_with(host=address),
                    content=body,
                    headers={**headers, "Host": target.host, "Accept-Encoding": "identity"},
                    extensions={"sni_hostname": target.host},
                ) as response,
            ):
                return PostResult(response.status_code, await _read_capped(response, read_limit))
    except TimeoutError, httpx2.TimeoutException:
        raise CallbackFailure("timeout") from None
    except httpx2.TransportError, OSError:
        raise CallbackFailure("unreachable") from None


async def verify_callback(
    target: CallbackTarget, subscription_id: str, key: bytes, runtime: EventsRuntime
) -> None:
    """Prove that the callback wants our events, or raise CallbackFailure."""
    challenge = secrets.token_urlsafe(32)
    body = serialise({"type": "verification", "challenge": challenge})
    message_id = f"msg_verification_{secrets.token_hex(16)}"
    headers = build_headers(message_id, int(runtime.clock()), body, subscription_id, [key])
    result = await pinned_post(target, headers, body, runtime, MAX_VERIFICATION_ANSWER_BYTES)

    # A redirect lands here too: it is not followed, so it is a wrong answer like any other.
    if not 200 <= result.status < 300:
        raise CallbackFailure("bad_status")
    if result.body is None:
        raise CallbackFailure("challenge_failed")
    try:
        answer = json.loads(result.body)
    except ValueError, RecursionError:
        raise CallbackFailure("challenge_failed") from None
    received = answer.get("challenge") if isinstance(answer, dict) else None
    # Bytes, because compare_digest refuses a str with non-ASCII characters.
    if not isinstance(received, str) or not hmac.compare_digest(
        received.encode(errors="replace"), challenge.encode()
    ):
        raise CallbackFailure("challenge_failed")


def signing_keys(subscription: Subscription, now: float) -> list[bytes]:
    """Return the current key and, during a rotation, the previous one."""
    keys = [parse_secret(subscription.secret)]
    until = subscription.previous_secret_until
    if subscription.previous_secret is not None and until is not None and until > now:
        keys.append(parse_secret(subscription.previous_secret))
    return keys


@dataclass(frozen=True)
class DeliveryResult:
    subscription_id: str
    status: int | None
    attempts: int
    delivered: bool


async def deliver(
    subscription: Subscription, data: dict[str, Any], runtime: EventsRuntime
) -> DeliveryResult:
    """Send one event to one subscription, retrying transient failures."""
    log_fields: dict[str, Any] = {
        "event": subscription.event_name,
        "subscription_id": subscription.id,
        "host": callback_host(subscription.callback_url),
        "url": subscription.callback_url,
    }
    try:
        # Checked again here, because the allowlist may have changed since the subscribe.
        target = check_callback_url(subscription.callback_url, runtime.callback_hosts)
    except UrlRefused:
        log_event("delivery", **log_fields, outcome="url_refused")
        runtime.store.record_delivery(subscription.id, runtime.clock(), "url_refused")
        return DeliveryResult(subscription.id, None, 0, False)

    event_id = f"evt_{uuid.uuid4().hex}"
    # Serialised once: a retry sends these same bytes, and only the signature is renewed.
    body = serialise(
        {
            "eventId": event_id,
            "name": subscription.event_name,
            "timestamp": iso_utc(runtime.clock()),
            "data": data,
            "cursor": None,
        }
    )

    status: int | None = None
    outcome = "unreachable"
    attempts = 0
    for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        attempts = attempt
        now = runtime.clock()
        keys = signing_keys(subscription, now)
        headers = build_headers(event_id, int(now), body, subscription.id, keys)
        try:
            status = (await pinned_post(target, headers, body, runtime, read_limit=0)).status
        except CallbackFailure as failure:
            status, outcome = None, failure.reason
            retry = failure.reason != "blocked_address"
        else:
            outcome = "delivered" if 200 <= status < 300 else "gone" if status == 410 else "failed"
            retry = status in (408, 429) or status >= 500
        log_event("delivery", **log_fields, outcome=outcome, status=status, attempt=attempt)
        if not retry or attempt == MAX_DELIVERY_ATTEMPTS:
            break
        await runtime.sleep(RETRY_DELAYS_SECONDS[attempt - 1])

    if status == 410:
        runtime.store.delete(subscription.id)
    else:
        recorded = outcome if status is None else str(status)
        runtime.store.record_delivery(subscription.id, runtime.clock(), recorded)
    return DeliveryResult(subscription.id, status, attempts, outcome == "delivered")


async def deliver_to_all(
    event_name: str, data: dict[str, Any], runtime: EventsRuntime
) -> list[DeliveryResult]:
    """Send one event to every active subscription of that event, one after the other."""
    active = runtime.store.list_active(runtime.clock())
    return [
        await deliver(subscription, data, runtime)
        for subscription in active
        if subscription.event_name == event_name
    ]
