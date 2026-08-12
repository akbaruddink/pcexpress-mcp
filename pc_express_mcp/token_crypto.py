"""Stateless bearer-token encoding for HTTP mode.

This server issues its own OAuth tokens to Claude as symmetrically-encrypted
envelopes carrying the tenant's PC Express credentials directly, rather than
storing them server-side and handing out an opaque lookup key (the earlier
design -- see git history). A token is decryptable only by whoever holds
PCEXPRESS_TOKEN_SECRET; see oauth_server.py's module docstring for what that
does and does not protect against (a stolen token is still a working bearer
credential against *this* server for its lifetime -- encryption protects the
PC credentials inside it from being extracted and reused *elsewhere*, it
does not make token theft harmless).

Fernet (AES128-CBC + HMAC-SHA256, both authenticated/tamper-evident, from
the well-reviewed `cryptography` package) rather than hand-rolled AES-GCM --
it's a misuse-resistant primitive with built-in TTL enforcement
(Fernet.decrypt(..., ttl=...) checks the token's own embedded timestamp),
which is exactly the shape of thing this needs and nothing more.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from . import config


class TokenSecretMissing(RuntimeError):
    """PCEXPRESS_TOKEN_SECRET isn't set -- HTTP mode can't issue or verify tokens without it."""


def _fernet() -> Fernet:
    if not config.TOKEN_SECRET:
        raise TokenSecretMissing(
            "PCEXPRESS_TOKEN_SECRET is not set. Generate one with "
            "`python scripts/generate_secret.py` and put it in your .env -- "
            "see README 'Remote/mobile access'."
        )
    try:
        return Fernet(config.TOKEN_SECRET.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise TokenSecretMissing(
            "PCEXPRESS_TOKEN_SECRET is set but isn't a valid Fernet key. "
            "Regenerate it with `python scripts/generate_secret.py`."
        ) from exc


def encode(payload: dict[str, Any]) -> str:
    """Encrypt `payload` into an opaque, tamper-evident token string."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decode(token: str, ttl_seconds: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Decrypt and return the payload, or None if the token is malformed,
    tampered with, encrypted under a different secret, or (when
    ttl_seconds is given) older than that many seconds since it was minted.
    """
    try:
        raw = _fernet().decrypt(token.encode("ascii"), ttl=ttl_seconds)
    except (InvalidToken, ValueError):
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None
