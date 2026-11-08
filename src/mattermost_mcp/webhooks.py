"""Standard Webhooks signing.

Signing is one HMAC call, so it is written here by hand. The tests check every signature with
the reference `standardwebhooks` library, so two independent implementations have to agree.
"""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Sequence
from typing import Any

SECRET_PREFIX = "whsec_"  # noqa: S105 - a format marker, not a secret
MIN_SECRET_BYTES = 24
MAX_SECRET_BYTES = 64


class InvalidSecret(ValueError):
    """The signing secret is malformed. The message never contains the secret."""


def parse_secret(secret: object) -> bytes:
    """Return the key bytes of a `whsec_` secret, or raise InvalidSecret.

    Stricter than the reference library, which adds padding but checks neither the alphabet
    nor the length.
    """
    if not isinstance(secret, str) or not secret.startswith(SECRET_PREFIX):
        raise InvalidSecret("the secret must start with whsec_")
    encoded = secret.removeprefix(SECRET_PREFIX)
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        key = base64.b64decode(padded, validate=True)
    except binascii.Error, ValueError:
        # `from None`: an exception chain has no business carrying anything about the secret.
        raise InvalidSecret("the secret is not valid base64") from None
    if not MIN_SECRET_BYTES <= len(key) <= MAX_SECRET_BYTES:
        raise InvalidSecret("the secret must decode to 24-64 bytes")
    return key


def serialise(payload: dict[str, Any]) -> bytes:
    """Turn a JSON body into the bytes that are both signed and sent.

    json.dumps escapes non-ASCII by default, so the result is valid UTF-8 even for text that
    holds lone surrogates, which a command-line argument can.
    """
    return json.dumps(payload, separators=(",", ":")).encode()


def sign(key: bytes, message_id: str, timestamp: int, body: bytes) -> str:
    """Return one `v1,` signature over the message id, the timestamp and the exact body bytes."""
    signed_content = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, signed_content, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def build_headers(
    message_id: str, timestamp: int, body: bytes, subscription_id: str, keys: Sequence[bytes]
) -> dict[str, str]:
    """Return the webhook headers. Several keys give space-separated signatures (rotation)."""
    return {
        "Content-Type": "application/json",
        "webhook-id": message_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": " ".join(sign(key, message_id, timestamp, body) for key in keys),
        "X-MCP-Subscription-Id": subscription_id,
    }
