"""MCP Events over webhooks, for the one hand-fired event `test.message`."""

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Any

from fastmcp.server.extensions import MethodBinding, ServerExtension
from fastmcp.server.middleware import Middleware
from mcp.shared.exceptions import MCPError
from mcp_types import RequestParams
from pydantic import BaseModel, Field

from mattermost_mcp.callbacks import (
    CallbackFailure,
    EventsRuntime,
    UrlRefused,
    callback_host,
    check_callback_url,
    iso_utc,
    log_event,
    verify_callback,
)
from mattermost_mcp.store import Subscription
from mattermost_mcp.webhooks import InvalidSecret, parse_secret

EVENT_NAME = "test.message"
MAX_TEXT_LENGTH = 1000

TEST_MESSAGE_EVENT: dict[str, Any] = {
    "name": EVENT_NAME,
    "description": (
        "A test message that the owner of this server sends by hand from the server, to try "
        "out event delivery. It carries only the text the owner typed. It has no filters."
    ),
    "delivery": ["webhook"],
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "payloadSchema": {
        "type": "object",
        "properties": {"text": {"type": "string", "maxLength": MAX_TEXT_LENGTH}},
        "required": ["text"],
        "additionalProperties": False,
    },
}

# There are no logins until M7. Everything is keyed by this value, so M7 only has to replace it.
PRINCIPAL = "anonymous"

INVALID_PARAMS = -32602
UNKNOWN_EVENT = -32011
LIMIT_EXCEEDED = -32013
UNSUPPORTED_DELIVERY = -32014
CALLBACK_ENDPOINT_ERROR = -32015

MIN_TTL_MS = 10 * 60 * 1000
MAX_TTL_MS = 60 * 60 * 1000
SECRET_OVERLAP_SECONDS = 60 * 60
VERIFICATION_VALID_SECONDS = 24 * 60 * 60
MAX_VERIFICATIONS_PER_MINUTE = 10


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def subscription_id(principal: str, url: str, name: str, arguments: object) -> str:
    """Derive the id, so that subscribing twice finds the same subscription."""
    digest = hashlib.sha256(canonical_json([principal, url, name, arguments]).encode())
    return "sub_" + digest.hexdigest()[:32]


def granted_ttl_ms(requested: float | None) -> float:
    """Clamp a requested lifetime. A request for no expiry (null) gets the maximum."""
    return MAX_TTL_MS if requested is None else max(MIN_TTL_MS, min(MAX_TTL_MS, requested))


# The fields are Any and checked by hand on purpose. A pydantic validation error quotes the
# offending input, and these params carry the signing secret.
class SubscribeParams(RequestParams):
    name: Any = None
    arguments: Any = Field(default_factory=dict)
    delivery: Any = None
    cursor: Any = None
    ttl_ms: Any = None


class UnsubscribeParams(RequestParams):
    name: Any = None
    arguments: Any = Field(default_factory=dict)
    delivery: Any = None


class EventsExtension(ServerExtension):
    """Serves events/list, events/subscribe and events/unsubscribe."""

    identifier = "dev.kosavina/events"

    def __init__(self) -> None:
        self.runtime: EventsRuntime | None = None

    def methods(self) -> Sequence[MethodBinding]:
        return [
            MethodBinding("events/list", RequestParams, self.list_events),
            MethodBinding("events/subscribe", SubscribeParams, self.subscribe),
            MethodBinding("events/unsubscribe", UnsubscribeParams, self.unsubscribe),
        ]

    def _runtime(self) -> EventsRuntime:
        if self.runtime is None:
            raise RuntimeError("create_app() was not given an EventsRuntime")
        return self.runtime

    def _allow_verification(self, runtime: EventsRuntime, now: float) -> bool:
        """Count one attempt against the limit, or return False if the limit is reached."""
        times = runtime.verification_times
        while times and now - times[0] >= 60:
            times.popleft()
        if len(times) >= MAX_VERIFICATIONS_PER_MINUTE:
            return False
        times.append(now)
        return True

    async def list_events(self, ctx: Any, params: Any) -> dict[str, Any]:
        return {"events": [TEST_MESSAGE_EVENT]}

    async def subscribe(self, ctx: Any, params: SubscribeParams) -> dict[str, Any]:
        runtime = self._runtime()
        name, delivery, ttl_ms = params.name, params.delivery, params.ttl_ms

        logged_name = name if isinstance(name, str) else None

        def refuse(
            code: int, message: str, outcome: str, data: dict[str, str] | None = None, **log: Any
        ) -> MCPError:
            log_event("subscribe", event=logged_name, outcome=outcome, **log)
            return MCPError(code=code, message=message, data=data)

        if not isinstance(name, str):
            raise refuse(INVALID_PARAMS, "name must be a string", "invalid_name")
        if name != EVENT_NAME:
            raise refuse(UNKNOWN_EVENT, "unknown event", "unknown_event")
        if params.arguments != {}:
            raise refuse(INVALID_PARAMS, "this event takes no arguments", "invalid_arguments")
        if not isinstance(delivery, dict):
            raise refuse(INVALID_PARAMS, "delivery must be an object", "invalid_delivery")
        if delivery.get("mode") != "webhook":
            raise refuse(UNSUPPORTED_DELIVERY, "only webhook delivery is supported", "bad_mode")
        # bool is an int to Python, and json can produce NaN and Infinity, which don't compare.
        if ttl_ms is not None and (
            isinstance(ttl_ms, bool)
            or not isinstance(ttl_ms, int | float)
            or (isinstance(ttl_ms, float) and not math.isfinite(ttl_ms))
        ):
            raise refuse(INVALID_PARAMS, "ttlMs must be a number or null", "invalid_ttl")

        secret = delivery.get("secret")
        try:
            key = parse_secret(secret)
        except InvalidSecret:
            raise refuse(
                INVALID_PARAMS, "the signing secret is invalid", "invalid_secret"
            ) from None

        try:
            target = check_callback_url(delivery.get("url"), runtime.callback_hosts)
        except UrlRefused as refused:
            # The host is logged so that we can tell if ChatGPT calls from an unexpected one.
            raise refuse(
                INVALID_PARAMS, "the callback URL is not allowed", "url_refused", host=refused.host
            ) from None

        sub_id = subscription_id(PRINCIPAL, target.url, name, params.arguments)
        log: dict[str, Any] = {
            "subscription_id": sub_id,
            "host": target.host,
            "url": target.url,
            "requested_ttl_ms": ttl_ms,
        }

        now = runtime.clock()
        # An expired row is treated as new. Refreshing it would bring back one more active
        # subscription without passing the cap.
        existing = runtime.store.get_active(sub_id, now)
        action = "subscribe" if existing is None else "refresh"
        if existing is None:
            runtime.store.delete_expired(now)
            if len(runtime.store.list_active(now)) >= runtime.max_subscriptions:
                raise refuse(
                    LIMIT_EXCEEDED,
                    "too many subscriptions",
                    "limit_subscriptions",
                    {"limit": "subscriptions"},
                    **log,
                )

        verified_at = None if existing is None else existing.verified_at
        if verified_at is None or now - verified_at >= VERIFICATION_VALID_SECONDS:
            if not self._allow_verification(runtime, now):
                raise refuse(
                    LIMIT_EXCEEDED,
                    "too many verification attempts",
                    "limit_verification_rate",
                    {"limit": "verification_rate"},
                    **log,
                )
            try:
                await verify_callback(target, sub_id, key, runtime)
            except CallbackFailure as failure:
                log_event("verification", event=name, outcome=failure.reason, **log)
                # Only our own category goes back: never anything from the callback's answer.
                raise MCPError(
                    code=CALLBACK_ENDPOINT_ERROR,
                    message="callback verification failed",
                    data={"reason": failure.reason},
                ) from None
            log_event("verification", event=name, outcome="ok", **log)
            verified_at = runtime.clock()

        now = runtime.clock()
        granted_seconds = granted_ttl_ms(ttl_ms) / 1000
        expires_at = now + granted_seconds
        previous_secret = None if existing is None else existing.previous_secret
        previous_secret_until = None if existing is None else existing.previous_secret_until
        if existing is not None and existing.secret != secret:
            previous_secret = existing.secret
            previous_secret_until = now + SECRET_OVERLAP_SECONDS

        saved = runtime.store.save(
            Subscription(
                id=sub_id,
                principal=PRINCIPAL,
                event_name=name,
                arguments=canonical_json(params.arguments),
                callback_url=target.url,
                secret=secret,
                previous_secret=previous_secret,
                previous_secret_until=previous_secret_until,
                created_at=now if existing is None else existing.created_at,
                refreshed_at=now,
                expires_at=expires_at,
                verified_at=verified_at,
                last_delivery_at=None if existing is None else existing.last_delivery_at,
                last_delivery_status=None if existing is None else existing.last_delivery_status,
            ),
            now,
            runtime.max_subscriptions,
        )
        if not saved:
            raise refuse(
                LIMIT_EXCEEDED,
                "too many subscriptions",
                "limit_subscriptions",
                {"limit": "subscriptions"},
                **log,
            )

        log_event(action, event=name, outcome="ok", granted_seconds=granted_seconds, **log)
        return {
            "id": sub_id,
            "refreshBefore": iso_utc(expires_at),
            "cursor": None,
            "truncated": False,
        }

    async def unsubscribe(self, ctx: Any, params: UnsubscribeParams) -> dict[str, Any]:
        runtime = self._runtime()
        name = params.name
        if name != EVENT_NAME:
            log_event(
                "unsubscribe",
                event=name if isinstance(name, str) else None,
                outcome="unknown_event",
            )
            raise MCPError(code=UNKNOWN_EVENT, message="unknown event")

        delivery = params.delivery
        url = delivery.get("url") if isinstance(delivery, dict) else None
        # A subscription can only exist with empty arguments and a string URL. The URL policy is
        # not applied: someone must be able to leave a host that has since been removed from it.
        if not isinstance(url, str) or params.arguments != {}:
            log_event("unsubscribe", event=name, outcome="not_found")
            return {}

        sub_id = subscription_id(PRINCIPAL, url, name, params.arguments)
        removed = runtime.store.delete(sub_id)
        log_event(
            "unsubscribe",
            event=name,
            subscription_id=sub_id,
            host=callback_host(url),
            url=url,
            outcome="removed" if removed else "not_found",
        )
        return {}


def _with_events_capability(result: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """Add `events` to the capabilities. The SDK's typed model would drop the unknown key."""
    wire = (
        result
        if isinstance(result, dict)
        else result.model_dump(by_alias=True, mode="json", exclude_none=True)
    )
    wire["capabilities"]["events"] = {}
    return wire


class AdvertiseEvents(Middleware):
    """Advertise `events` to current clients (server/discover) and older ones (initialize)."""

    async def on_discover(self, context: Any, call_next: Any) -> Any:
        return _with_events_capability(await call_next(context))

    async def on_initialize(self, context: Any, call_next: Any) -> Any:
        result = await call_next(context)
        return None if result is None else _with_events_capability(result)
