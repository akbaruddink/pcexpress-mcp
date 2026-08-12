"""Non-secret local state: active banner/store and cached cart/customer ids.

Deliberately kept in a separate file from auth_state.json (which holds
tokens) so this one can be freely inspected, edited, or deleted without
touching credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import config


@dataclass
class SessionState:
    banner: str = config.DEFAULT_BANNER
    store_id: Optional[str] = None
    customer_id: Optional[str] = None
    cart_id: Optional[str] = None
    # store_id -> last-fetched pickup-location details, so list_stores() has
    # something to show without a store-search API (see README "Finding
    # your store_id").
    known_stores: dict[str, dict] = field(default_factory=dict)


def load(path: Optional[str] = None) -> SessionState:
    """`path` defaults to config.SESSION_STATE_PATH (single-tenant/stdio
    mode) -- the only mode that uses this module at all. HTTP mode keeps
    each tenant's session/cart state in an in-memory dict instead (see
    server.py's _session_states), never on disk.

    PCEXPRESS_STORE_ID's env-var default only applies to the single flat
    default path -- there's no sensible per-tenant equivalent of one global
    env var, so a new tenant just starts with no store_id until they call
    set_active_store.
    """
    path = path or config.SESSION_STATE_PATH
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        return SessionState(
            banner=data.get("banner", config.DEFAULT_BANNER),
            store_id=data.get("store_id") or (config.DEFAULT_STORE_ID if path == config.SESSION_STATE_PATH else None) or None,
            customer_id=data.get("customer_id"),
            cart_id=data.get("cart_id"),
            known_stores=data.get("known_stores", {}),
        )
    default_store_id = config.DEFAULT_STORE_ID if path == config.SESSION_STATE_PATH else None
    return SessionState(store_id=default_store_id or None)


def save(state: SessionState, path: Optional[str] = None) -> None:
    path = path or config.SESSION_STATE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(asdict(state), f, indent=2)
    # Not password-level secret, but it does hold customer_id/cart_id and
    # known store addresses/phone numbers -- chmod to match every other
    # state file in the project (auth_state.json, oauth_state.json, tenant
    # meta.json) rather than leaving it at default/umask permissions.
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
