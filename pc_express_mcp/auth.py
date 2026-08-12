"""PC ID OAuth token lifecycle management.

Loads/saves the local auth state file, refreshes access tokens transparently
(access tokens are ~1hr per the reverse-engineered docs), and exchanges an
authorization code for tokens during the one-time login (scripts/login.py).
TokenManager (file-backed) is used by stdio mode; EphemeralTokenManager
(in-memory only, seeded from a decrypted OAuth token) is used by HTTP mode's
stateless tenants -- see server.py's _get_token_manager() and
token_crypto.py/oauth_server.py for why HTTP mode has no per-tenant file at
all.

PC ID refresh tokens are single-use and rotate on every refresh -- this
module always persists (TokenManager) or re-embeds (EphemeralTokenManager)
the *newest* refresh token returned. If a refresh call fails outright, that
almost always means the presented refresh token was already consumed (e.g.
two server instances running at once) or has expired from being idle; the
fix is re-authenticating (scripts/login.py for stdio, reconnecting the
connector for HTTP), never guessing.

Nothing in this module ever logs or prints a raw token value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

from . import config


class PcidAuthError(RuntimeError):
    """Credentials are missing, expired, or were rejected by PC ID.

    MCP tools should catch this and surface the message directly -- it is
    already written to tell the user exactly what to do (usually: run
    scripts/login.py again).
    """


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for OAuth2 PKCE, method S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def extract_pcid_redirect(pasted: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Pull (code, state, error) out of whatever a user pastes after PC ID login.

    Handles three real shapes users end up copying, all confirmed against
    actual PC ID behaviour:
      1. The bare authorization code, pasted directly.
      2. The `com.loblaw.pcx://pcx-android/login/appredirect?code=...&state=...`
         URL a desktop browser fails to navigate to (no app installed to
         handle the custom scheme) -- `code`/`state` are top-level query
         params here.
      3. The `https://accounts.pcid.ca/login/success?redirectURL=com.loblaw.pcx://...`
         interstitial page PC ID shows *before* attempting that failed
         navigation -- copyable earlier/more reliably than (2). Its
         `redirectURL` value is NOT URL-escaped, so naive top-level query
         parsing finds `state` (which "leaks" out as a top-level param
         because the inner URL's own unescaped `&` breaks the outer query
         string apart) but NOT `code` (which stays embedded inside the
         `redirectURL` value's own query string). This was missed in an
         earlier version of this parser -- caught from a real user pasting
         a real interstitial URL, not anticipated in advance.
    """
    pasted = pasted.strip()
    qs = parse_qs(urlparse(pasted).query)

    if not qs:
        return pasted, None, None  # no query string at all -- assume a bare code

    if "error" in qs:
        err = qs["error"][0]
        desc = (qs.get("error_description") or [""])[0]
        return None, None, f"{err} {desc}".strip()

    if "redirectURL" in qs:
        inner_qs = parse_qs(urlparse(qs["redirectURL"][0]).query)
        code = (inner_qs.get("code") or qs.get("code") or [None])[0]
        state = (inner_qs.get("state") or qs.get("state") or [None])[0]
        return code, state, None

    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    return code, state, None


@dataclass
class AuthState:
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    expires_at: float = 0.0  # unix timestamp

    def is_access_token_valid(self) -> bool:
        if not self.access_token:
            return False
        return time.time() < (self.expires_at - config.TOKEN_REFRESH_SKEW_SECONDS)


def _ensure_state_dir(state_dir: str) -> None:
    os.makedirs(state_dir, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass  # best-effort; not fatal (e.g. some filesystems don't support it)


def load_auth_state(path: Optional[str] = None) -> AuthState:
    """`path` defaults to config.AUTH_STATE_PATH (the single-tenant/stdio-mode
    session) -- the only mode that uses this at all. HTTP mode's tenants use
    EphemeralTokenManager instead (no file, seeded from a decrypted OAuth
    token) -- see server.py's _get_token_manager().
    """
    path = path or config.AUTH_STATE_PATH
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        return AuthState(
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            id_token=data.get("id_token"),
            expires_at=data.get("expires_at", 0.0),
        )
    # First run: allow bootstrapping from an env var instead of requiring the
    # state file to already exist. Once we successfully refresh, the rotated
    # token gets written to disk and the env var is no longer consulted.
    # Only applies to the default (single-tenant) path -- there's no sensible
    # per-tenant equivalent of a single global env var.
    if path == config.AUTH_STATE_PATH:
        env_refresh = os.environ.get("PCEXPRESS_REFRESH_TOKEN")
        if env_refresh:
            return AuthState(refresh_token=env_refresh)
    return AuthState()


def save_auth_state(state: AuthState, path: Optional[str] = None) -> None:
    path = path or config.AUTH_STATE_PATH
    _ensure_state_dir(os.path.dirname(path))
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(asdict(state), f)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def build_authorize_url() -> tuple[str, str, str]:
    """Return (authorize_url, code_verifier, state) for the login script."""
    if not config.PCID_CLIENT_ID:
        raise PcidAuthError(
            "PCEXPRESS_CLIENT_ID is not set. See README.md 'Authentication "
            "setup' for where to obtain the PC Express Android app's OAuth "
            "client id, then put it in your .env."
        )
    verifier, challenge = generate_pkce_pair()
    state = generate_state()
    nonce = secrets.token_urlsafe(16)
    params = {
        "client_id": config.PCID_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.PCID_REDIRECT_URI,
        "scope": config.PCID_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
    }
    url = httpx.URL(config.PCID_AUTHORIZE_URL, params=params)
    return str(url), verifier, state


def raw_exchange_authorization_code(client: httpx.Client, code: str, code_verifier: str) -> dict:
    """Exchange a PC ID authorization code for tokens -- returns the raw JSON
    payload, does NOT persist anything or touch any AuthState.

    Used by oauth_server.py's account-verification flow (see its module
    docstring), which must inspect *whose* PC Express account this login
    belongs to before deciding whether to trust/persist it -- unlike
    scripts/login.py, which runs over your own SSH session and can trust
    the result unconditionally (TokenManager.exchange_code wraps this and
    persists immediately for that case).

    The Android app omits redirect_uri from this call; some IDCS configs
    reportedly require it anyway, so mirror the prior-art project's
    approach of trying app-exact first and retrying with redirect_uri
    included if that's rejected.
    """
    if not config.PCID_CLIENT_ID or not config.PCID_CLIENT_SECRET:
        raise PcidAuthError("PCEXPRESS_CLIENT_ID / PCEXPRESS_CLIENT_SECRET are not set.")
    base_body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.PCID_CLIENT_ID,
        "client_secret": config.PCID_CLIENT_SECRET,
        "code_verifier": code_verifier,
    }
    last_resp = None
    for body in (base_body, {**base_body, "redirect_uri": config.PCID_REDIRECT_URI}):
        resp = client.post(config.PCID_TOKEN_URL, data=body, headers=config.PCID_TOKEN_HEADERS)
        if resp.status_code == 200:
            return resp.json()
        last_resp = resp
    raise PcidAuthError(
        f"PC ID rejected the authorization code (HTTP {last_resp.status_code}): {last_resp.text[:300]}"
    )


def raw_refresh_token(client: httpx.Client, refresh_token: str) -> dict:
    """Refresh a PC ID refresh token for a raw token payload -- does NOT
    persist anything, same spirit as raw_exchange_authorization_code.

    Used by both TokenManager (which persists the result itself, see
    _refresh below) and oauth_server.py's stateless /token endpoint /
    EphemeralTokenManager, which have nowhere on disk to persist to and
    instead re-embed the result directly into a new encrypted outer token.
    """
    if not config.PCID_CLIENT_ID or not config.PCID_CLIENT_SECRET:
        raise PcidAuthError("PCEXPRESS_CLIENT_ID / PCEXPRESS_CLIENT_SECRET are not set.")
    resp = client.post(
        config.PCID_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.PCID_CLIENT_ID,
            "client_secret": config.PCID_CLIENT_SECRET,
        },
        headers=config.PCID_TOKEN_HEADERS,
    )
    if resp.status_code != 200:
        raise PcidAuthError(
            f"PC ID rejected the refresh token (HTTP {resp.status_code}). "
            "It may have expired, already been used, or been revoked."
        )
    return resp.json()


def _auth_state_from_payload(payload: dict, fallback_refresh_token: Optional[str]) -> AuthState:
    return AuthState(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", fallback_refresh_token),
        id_token=payload.get("id_token"),
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
    )


class TokenManager:
    """Owns the current AuthState and knows how to refresh/exchange it.
    File-backed -- used only by stdio mode, where one instance is shared by
    the whole MCP server process (auth_state_path defaults to
    config.AUTH_STATE_PATH). HTTP mode uses EphemeralTokenManager instead
    (below) -- see server.py's _get_token_manager(). api_client.py calls
    get_access_token() before every request and force_refresh() after a 401
    against either class equally (same public interface, duck-typed).
    """

    def __init__(self, auth_state_path: Optional[str] = None) -> None:
        self._auth_state_path = auth_state_path or config.AUTH_STATE_PATH
        self._state = load_auth_state(self._auth_state_path)

    def get_access_token(self, client: httpx.Client) -> str:
        if not self._state.is_access_token_valid():
            self._refresh(client)
        assert self._state.access_token is not None
        return self._state.access_token

    def force_refresh(self, client: httpx.Client) -> str:
        self._refresh(client)
        assert self._state.access_token is not None
        return self._state.access_token

    def _require_client_credentials(self) -> None:
        if not config.PCID_CLIENT_ID or not config.PCID_CLIENT_SECRET:
            raise PcidAuthError(
                "PCEXPRESS_CLIENT_ID / PCEXPRESS_CLIENT_SECRET are not set. "
                "See README.md 'Authentication setup'."
            )

    def _refresh(self, client: httpx.Client) -> None:
        if not self._state.refresh_token:
            raise PcidAuthError(
                "No PC Express refresh token on file. Run "
                "`python scripts/login.py` once to authenticate, then "
                "restart the MCP server."
            )
        self._require_client_credentials()
        try:
            payload = raw_refresh_token(client, self._state.refresh_token)
        except PcidAuthError as exc:
            raise PcidAuthError(f"{exc} Run `python scripts/login.py` again to re-authenticate.") from exc
        self._apply_token_response(payload)

    def exchange_code(self, client: httpx.Client, code: str, code_verifier: str) -> None:
        """Used by scripts/login.py for the initial code -> token exchange.

        Unconditionally trusts and persists the result -- appropriate here
        because scripts/login.py only ever runs over your own SSH session.
        oauth_server.py's account-verification flow needs the opposite
        (inspect *whose* account this is before deciding whether to trust
        it), so it calls raw_exchange_authorization_code() directly instead.
        """
        payload = raw_exchange_authorization_code(client, code, code_verifier)
        self._apply_token_response(payload)

    def _apply_token_response(self, payload: dict) -> None:
        self._state = _auth_state_from_payload(payload, self._state.refresh_token)
        save_auth_state(self._state, self._auth_state_path)


class EphemeralTokenManager:
    """Same public interface as TokenManager (get_access_token/force_refresh)
    but holds PC ID credentials purely in memory, seeded from an already-
    decrypted token payload rather than a file -- used for HTTP mode's
    stateless tenants (see token_crypto.py/oauth_server.py).

    A refresh here updates this instance's in-memory state for the
    remainder of the current request only; it is never persisted anywhere,
    since there is nowhere stateless to persist it to. That's fine: the
    durable source of truth is the encrypted outer token itself, kept in
    sync via oauth_server.token_endpoint's own refresh_token grant (which
    mints a fresh outer token embedding the newly-rotated PC credentials)
    -- not this class. This class only exists to cover the rare case where
    PC ID's own access token happens to need a mid-request refresh (e.g.
    clock skew) before that normal outer-token refresh cycle catches up.
    See server.py's _get_token_manager().
    """

    def __init__(self, access_token: str, refresh_token: Optional[str], expires_at: float) -> None:
        self._state = AuthState(access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)

    def get_access_token(self, client: httpx.Client) -> str:
        if not self._state.is_access_token_valid():
            self._refresh(client)
        assert self._state.access_token is not None
        return self._state.access_token

    def force_refresh(self, client: httpx.Client) -> str:
        self._refresh(client)
        assert self._state.access_token is not None
        return self._state.access_token

    def _refresh(self, client: httpx.Client) -> None:
        if not self._state.refresh_token:
            raise PcidAuthError(
                "No PC Express refresh token available for this session. "
                "Reconnect the connector (repeat the /authorize login) to get a fresh one."
            )
        if not config.PCID_CLIENT_ID or not config.PCID_CLIENT_SECRET:
            raise PcidAuthError("PCEXPRESS_CLIENT_ID / PCEXPRESS_CLIENT_SECRET are not set.")
        payload = raw_refresh_token(client, self._state.refresh_token)
        self._state = _auth_state_from_payload(payload, self._state.refresh_token)
