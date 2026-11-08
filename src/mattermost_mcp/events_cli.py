"""The `mattermost-mcp-events` command, run by hand inside the app container.

Sending an event needs SSH and sudo on the server, which is the only authentication M6 has.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from mattermost_mcp.callbacks import (
    EventsRuntime,
    callback_host,
    deliver_to_all,
    iso_utc,
    url_fingerprint,
)
from mattermost_mcp.events import EVENT_NAME, MAX_TEXT_LENGTH
from mattermost_mcp.server import configure_logging, runtime_from_env
from mattermost_mcp.store import StoreError


def list_subscriptions(runtime: EventsRuntime) -> int:
    now = runtime.clock()
    subscriptions = runtime.store.list_active(now)
    if not subscriptions:
        print("no active subscriptions")
    for subscription in subscriptions:
        print(
            f"id={subscription.id} event={subscription.event_name} "
            f"host={callback_host(subscription.callback_url)} "
            f"url_fingerprint={url_fingerprint(subscription.callback_url)} "
            f"created={iso_utc(subscription.created_at)} "
            f"expires_in_min={int((subscription.expires_at - now) / 60)} "
            f"verified={'yes' if subscription.verified_at is not None else 'no'} "
            f"last_delivery={subscription.last_delivery_status or '-'}"
        )
    return 0


def send_test(runtime: EventsRuntime, text: str) -> int:
    if len(text) > MAX_TEXT_LENGTH:
        print(f"the text is longer than {MAX_TEXT_LENGTH} characters", file=sys.stderr)
        return 2
    results = asyncio.run(deliver_to_all(EVENT_NAME, {"text": text}, runtime))
    if not results:
        print("no active subscriptions", file=sys.stderr)
        return 1
    for result in results:
        status = "-" if result.status is None else result.status
        print(f"{result.subscription_id} status={status} attempts={result.attempts}")
    return 0 if all(result.delivered for result in results) else 1


def remove(runtime: EventsRuntime, subscription_id: str | None) -> int:
    if subscription_id is None:
        print(f"removed {runtime.store.delete_all()} subscriptions")
        return 0
    if not runtime.store.delete(subscription_id):
        print(f"no subscription {subscription_id}", file=sys.stderr)
        return 1
    print(f"removed {subscription_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mattermost-mcp-events")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show the active subscriptions")
    send = commands.add_parser("send-test", help="send a test.message to every subscription")
    send.add_argument("text")
    removal = commands.add_parser("remove", help="delete one subscription, or all of them")
    target = removal.add_mutually_exclusive_group(required=True)
    target.add_argument("id", nargs="?")
    target.add_argument("--all", action="store_true")
    return parser


def run(argv: Sequence[str], runtime: EventsRuntime | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if runtime is None:
        try:
            runtime = runtime_from_env()
        except (StoreError, ValueError) as error:
            print(f"mattermost-mcp-events: {error}", file=sys.stderr)
            return 1
    if arguments.command == "list":
        return list_subscriptions(runtime)
    if arguments.command == "send-test":
        return send_test(runtime, arguments.text)
    return remove(runtime, None if arguments.all else arguments.id)


def main() -> None:
    # stderr, so that stdout stays the report. These lines stay with this command: `docker logs`
    # shows only the output of the container's main process.
    configure_logging(sys.stderr)
    sys.exit(run(sys.argv[1:]))
