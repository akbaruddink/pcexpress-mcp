"""A self-service, multi-tenant OAuth 2.1 + PKCE authorization server for HTTP mode.

Why this exists: Claude's remote-connector flow expects OAuth by default.
On accounts where the simpler `static_headers` (bearer-token) option isn't
exposed in the "Add custom connector" UI, Claude instead tries OAuth
Dynamic Client Registration (DCR) against the server -- which fails outright
against a server with no /register endpoint. This module makes that flow
succeed *without* implementing full DCR, using the alternative Anthropic's
own docs describe for custom connectors: a client_id entered by hand into
"Advanced settings", no DCR, no client_secret (PKCE covers a public
client). See README "Remote/mobile access".

Security model, part 1 -- who gets approved: this server is stateless with
respect to client_id -- it does NOT pre-approve a fixed account. Whatever
`client_id` Claude sends (whoever added the connector typed it into
Advanced Settings) is treated as a *claim*, not a fact: /authorize requires
a real, fresh PC ID login (consolidated onto this same page -- see
authorize_get/authorize_post) and only approves the connector if the
account that just logged in matches the claimed client_id exactly (verified
live: that's `profile.id`, the login email -- a separate `customerId` field
is a UUID, easy to mistake for "the" identifier). Self-consistency between
claim and proof is the entire trust model -- there is no pre-configured
allowlist, so *any* PC Express account can self-provision access. This is
deliberately open self-service, a real scope decision -- see README "Remote
/mobile access" for the tradeoffs (shared infrastructure/abuse exposure,
borrowed-app-credential ban risk) before assuming this is "safe" in the way
a closed allowlist would be.

This is deliberately NOT "any successful PC ID login approves it" -- the
Android app's client_id/secret this project borrows are published in a
public repo (see config.py), so literally anyone can complete *a* PC ID
login with their own, freely-created PC Express account regardless of
whether this server exists. What makes self-service safe here specifically
is that each tenant can only ever act as *themselves*: the account that
logs in becomes the account that's provisioned, never anyone else's.

Security model, part 2 -- where credentials live: this server holds NO
persistent per-tenant state at all, on disk or otherwise. The OAuth tokens
handed back to Claude are self-contained encrypted envelopes -- built by
token_crypto.py, symmetrically encrypted under the single operator secret
PCEXPRESS_TOKEN_SECRET -- carrying that tenant's PC Express access/refresh
tokens directly. Verifying a request means decrypting the token it presents,
not looking anything up in server-side storage. This means: no tenant
directories, no credential files to chmod or leak, nothing to clean up if a
login attempt is abandoned partway, and this process can restart (or run as
several replicas behind a load balancer) without losing anyone's session --
as long as they all share the same PCEXPRESS_TOKEN_SECRET.

Not theft-proof: a stolen token is still a working bearer credential
against *this server* until it expires; encryption only stops the PC
credentials inside it being reused elsewhere. Mitigations (short TTL, PC ID
grant revocation, rotating PCEXPRESS_TOKEN_SECRET) are in docs/SECURITY.md.

One more consequence worth naming: PC ID refresh tokens are single-use and
rotate on every refresh (see auth.py) -- so a stolen *token* can't just be
decrypted-and-refreshed silently by us without that showing up as an actual
credential rotation the legitimate holder would also need to pick up. It's
also why an outer access token expires with the PC token inside it
(decode_access_token): refresh must happen through token_endpoint's
refresh_token grant, the one place a rotated PC ID refresh token gets
re-embedded, never mid-request -- see auth.EphemeralTokenManager.

Multi-tenant token flow: the SDK's native auth pipeline (TokenVerifier ->
AuthenticationMiddleware -> AuthContextMiddleware -> get_access_token(),
wired in by server.py's build_http_app()) is what makes a tool call in
server.py able to find out which tenant it's serving -- MultiTenantTokenVerifier
below returns an AccessToken whose client_id field carries the tenant key,
retrievable inside any tool function via
`mcp.server.auth.middleware.auth_context.get_access_token().client_id`.
This is SDK-native machinery, not a hand-rolled contextvar -- verified
against the real installed SDK source before building on it (see
tests/test_http_app.py for the live-request proof it actually propagates
through the SDK's internal task-group dispatch).

Other state -- genuinely ephemeral, in-memory, and never persisted:
- Authorization codes (ours, for the Claude<->us leg) live in _AUTH_CODES
  below, a plain in-memory dict. Short-lived (config.OAUTH_CODE_TTL_SECONDS),
  single-use, expired ones swept on each new login; losing them on a
  restart just means an in-flight login has to be retried.
- The PC ID PKCE verifier/state (for the us<->PC ID leg) is round-tripped
  through hidden form fields on the /authorize page rather than server-side
  session storage, since the browser is about to navigate away to
  accounts.pcid.ca and back to this same loaded page.
- server.py keeps an in-memory per-tenant session/cart cache (banner,
  active store, cart id) -- not security-sensitive, and self-healing (see
  server._rediscover_cart) if lost on restart.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import api_client, auth, config, token_crypto

# Short-lived; in-memory only (see module docstring).
_AUTH_CODES: dict[str, dict[str, Any]] = {}

# Claude's hosted surfaces (Claude.ai web, Desktop, mobile, Cowork) use this
# fixed callback. Claude Code uses a loopback redirect on an ephemeral port
# instead -- both are accepted, everything else is rejected to prevent
# redirect-based token theft.
_HOSTED_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


def _is_allowed_redirect_uri(uri: str) -> bool:
    if uri == _HOSTED_REDIRECT_URI:
        return True
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    # RFC 8252 loopback redirect, port ignored, for Claude Code. urlparse()
    # normalizes a bracketed IPv6 literal's .hostname to the bare "::1" (no
    # brackets), so that's the form to check for, not "[::1]".
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        and parsed.path == "/callback"
    )


def _issue_tokens(tenant: str, pc_access_token: str, pc_refresh_token: Optional[str], pc_expires_at: float) -> dict[str, Any]:
    """Mint a fresh Claude-facing access/refresh token pair, each an
    encrypted envelope carrying `tenant`'s current PC Express credentials
    (see token_crypto.py and the module docstring's "part 2").

    The access token carries the full snapshot (access + refresh + expiry)
    so a single request can self-heal a near-expiry PC access token without
    a second round trip; the refresh token only needs the PC refresh token,
    since a refresh always produces a brand new snapshot anyway.
    """
    access_payload = {
        "tenant": tenant,
        "pc_access_token": pc_access_token,
        "pc_refresh_token": pc_refresh_token,
        "pc_expires_at": pc_expires_at,
    }
    refresh_payload = {"tenant": tenant, "pc_refresh_token": pc_refresh_token}
    usable_for = pc_expires_at - config.TOKEN_REFRESH_SKEW_SECONDS - time.time()
    return {
        "access_token": token_crypto.encode(access_payload),
        "token_type": "Bearer",
        "expires_in": max(0, int(min(config.OAUTH_ACCESS_TOKEN_TTL_SECONDS, usable_for))),
        "refresh_token": token_crypto.encode(refresh_payload),
        "scope": "mcp offline_access",
    }


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """Decrypt+validate a Claude-facing access token, or None if it's
    malformed, tampered with, expired, or encrypted under a different
    PCEXPRESS_TOKEN_SECRET. Used by MultiTenantTokenVerifier (the SDK-native
    gate) and by server.py to recover the current request's PC Express
    credentials for an actual API call.
    """
    payload = token_crypto.decode(token, ttl_seconds=config.OAUTH_ACCESS_TOKEN_TTL_SECONDS)
    if not payload or "tenant" not in payload or "pc_access_token" not in payload:
        return None
    # Expire with the PC token inside, so Claude refreshes via /token (which
    # re-embeds PC ID's rotated single-use refresh token) rather than a tool
    # call refreshing mid-request and losing it -- see EphemeralTokenManager.
    if time.time() >= float(payload.get("pc_expires_at") or 0) - config.TOKEN_REFRESH_SKEW_SECONDS:
        return None
    return payload


def get_token_tenant(token: str) -> Optional[str]:
    """Return the tenant key a valid, non-expired access token belongs to,
    or None otherwise. Thin convenience wrapper around decode_access_token
    for callers that only need the tenant, not the full credential payload.
    """
    payload = decode_access_token(token)
    return payload["tenant"] if payload else None


class MultiTenantTokenVerifier(TokenVerifier):
    """Bridges this module's token encoding to the MCP SDK's native auth
    pipeline (TokenVerifier -> AuthenticationMiddleware ->
    AuthContextMiddleware -> get_access_token()) -- see module docstring.
    The returned AccessToken.client_id is what a tool function in
    server.py reads to know which tenant's request it's handling; the raw
    AccessToken.token string is what server.py re-decrypts to recover that
    tenant's actual PC Express credentials (see server._get_token_manager()).
    """

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        payload = decode_access_token(token)
        if payload is None:
            return None
        return AccessToken(token=token, client_id=payload["tenant"], scopes=[])


# --- discovery metadata --------------------------------------------------------


async def authorization_server_metadata(request: Request) -> JSONResponse:
    issuer = config.PUBLIC_URL
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            # No registration_endpoint -- deliberately no DCR, see module docstring.
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "scopes_supported": ["mcp", "offline_access"],
        }
    )


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """Kept alongside the SDK's own auto-generated equivalent (registered at
    a different path, /.well-known/oauth-protected-resource/mcp, once
    build_http_app() passes auth.resource_server_url -- see server.py) for
    backwards compatibility with this exact path, already verified live
    before the SDK-native wiring existed. Both are always valid
    simultaneously; harmless redundancy, not a conflict.
    """
    issuer = config.PUBLIC_URL
    return JSONResponse(
        {
            "resource": f"{issuer}/mcp",
            "authorization_servers": [issuer],
        }
    )


# --- /authorize ------------------------------------------------------------------


async def authorize_get(request: Request) -> Response:
    params = request.query_params
    client_id = params.get("client_id", "")
    if not config.sanitize_tenant_key(client_id):
        return PlainTextResponse("client_id must look like an email address.", status_code=400)
    if params.get("response_type") != "code":
        return PlainTextResponse("Only response_type=code is supported.", status_code=400)
    redirect_uri = params.get("redirect_uri", "")
    if not _is_allowed_redirect_uri(redirect_uri):
        return PlainTextResponse("redirect_uri not allowed.", status_code=400)
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        return PlainTextResponse("PKCE (code_challenge, S256) is required.", status_code=400)

    # A separate, fresh PC ID PKCE pair for the us<->PC-ID leg -- unrelated
    # to Claude's own PKCE params above, which are for the Claude<->us leg.
    # Round-tripped through hidden fields (not server-side session storage)
    # since the browser is about to navigate away to accounts.pcid.ca and
    # back to this same loaded page.
    pcid_url, pcid_verifier, pcid_state = auth.build_authorize_url()

    claude_hidden = "".join(
        f'<input type="hidden" name="claude_{html.escape(k)}" value="{html.escape(v)}">' for k, v in params.items()
    )
    return HTMLResponse(f"""<!doctype html>
<html><head><title>pc-express-mcp authorization</title></head>
<body style="font-family: sans-serif; max-width: 34rem; margin: 4rem auto; line-height: 1.5;">
  <h3>Approve connector access?</h3>
  <p>This grants access to PC Express cart/search tools for
  <strong>{html.escape(client_id)}</strong> via this server. To approve,
  log into <strong>that same PC Express account</strong> and paste back
  the redirect -- this only approves the connector if that login matches.</p>
  <ol>
    <li><a href="{html.escape(pcid_url)}" target="_blank" rel="noopener">Log into PC Express</a> (opens in a new tab -- keep this page open)</li>
    <li>After logging in, your browser lands on an
      <code>accounts.pcid.ca/login/success?redirectURL=...</code> page and
      then tries (and fails) to open a <code>com.loblaw.pcx://...</code>
      one -- that failure is expected. Copy the address from either page and
      paste it below.</li>
  </ol>
  <form method="post">
    {claude_hidden}
    <input type="hidden" name="pcid_code_verifier" value="{html.escape(pcid_verifier)}">
    <input type="hidden" name="pcid_expected_state" value="{html.escape(pcid_state)}">
    <textarea name="pcid_redirect" rows="4" style="width: 100%; font-family: monospace; padding: 0.5rem;"
              placeholder="Paste the redirect URL (or bare code) here" autofocus required></textarea>
    <button type="submit" style="margin-top: 1rem; padding: 0.5rem 1.5rem;">Approve</button>
  </form>
</body></html>""")


async def authorize_post(request: Request) -> Response:
    form = await request.form()
    claimed_client_id = str(form.get("claude_client_id", ""))
    tenant_key = config.sanitize_tenant_key(claimed_client_id)
    if not tenant_key:
        return PlainTextResponse("client_id must look like an email address.", status_code=400)
    redirect_uri = str(form.get("claude_redirect_uri", ""))
    if not _is_allowed_redirect_uri(redirect_uri):
        return PlainTextResponse("redirect_uri not allowed.", status_code=400)

    pasted = str(form.get("pcid_redirect", ""))
    code, pcid_state, error = auth.extract_pcid_redirect(pasted)
    if error:
        return HTMLResponse(f"PC ID login failed: {html.escape(error)}. Go back and try again.", status_code=401)

    expected_pcid_state = str(form.get("pcid_expected_state", ""))
    if pcid_state and pcid_state != expected_pcid_state:
        return HTMLResponse(
            "That doesn't match the login attempt this page started (stale "
            "or reused link?). Reload this page and try again from the start.",
            status_code=401,
        )
    if not code:
        return HTMLResponse(
            "Couldn't find a PC ID authorization code in what you pasted. Go "
            "back and paste the full redirect URL.",
            status_code=400,
        )

    pcid_verifier = str(form.get("pcid_code_verifier", ""))
    with httpx.Client(timeout=30.0) as client:
        try:
            payload = auth.raw_exchange_authorization_code(client, code, pcid_verifier)
        except auth.PcidAuthError as exc:
            return HTMLResponse(f"PC ID login failed: {html.escape(str(exc))}", status_code=401)

        logged_in_email = api_client.fetch_customer_id(client, payload["access_token"], config.DEFAULT_BANNER)

    logged_in_tenant_key = config.sanitize_tenant_key(logged_in_email) if logged_in_email else None
    if not logged_in_tenant_key or logged_in_tenant_key != tenant_key:
        return HTMLResponse(
            f"The account you logged into doesn't match the client_id "
            f"({html.escape(claimed_client_id)}) this connector claimed. "
            "Log out of accounts.pcid.ca and try again with the matching account.",
            status_code=403,
        )

    # Verified: self-consistent claim, real login. Stash the freshly-exchanged
    # PC Express credentials in the (in-memory, short-lived) auth code entry
    # -- token_endpoint embeds them directly into the outer tokens it mints
    # below, never onto disk. See module docstring "part 2".
    # Sweep abandoned codes so their embedded PC credentials don't linger.
    for stale in [c for c, e in _AUTH_CODES.items() if e["expires_at"] < time.time()]:
        del _AUTH_CODES[stale]
    claude_code = secrets.token_urlsafe(32)
    _AUTH_CODES[claude_code] = {
        "redirect_uri": redirect_uri,
        "code_challenge": form.get("claude_code_challenge", ""),
        "expires_at": time.time() + config.OAUTH_CODE_TTL_SECONDS,
        "tenant": tenant_key,
        "pc_access_token": payload["access_token"],
        "pc_refresh_token": payload.get("refresh_token"),
        "pc_expires_at": time.time() + float(payload.get("expires_in", 3600)),
    }
    claude_state = form.get("claude_state")
    location = f"{redirect_uri}?code={quote(claude_code)}"
    if claude_state:
        location += f"&state={quote(str(claude_state))}"
    return RedirectResponse(location, status_code=302)


# --- /token ------------------------------------------------------------------------


async def token_endpoint(request: Request) -> JSONResponse:
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = str(form.get("code", ""))
        entry = _AUTH_CODES.pop(code, None)
        if not entry or entry["expires_at"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # RFC 6749/OAuth 2.1 defense-in-depth: the redirect_uri presented
        # here must match the one used at /authorize for this code. PKCE
        # (below) already covers the primary code-interception risk, but a
        # thorough implementation checks this too rather than relying on
        # PKCE alone.
        presented_redirect_uri = form.get("redirect_uri")
        if presented_redirect_uri is not None and presented_redirect_uri != entry["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        verifier = str(form.get("code_verifier", ""))
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if not secrets.compare_digest(challenge, entry["code_challenge"]):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return JSONResponse(
            _issue_tokens(entry["tenant"], entry["pc_access_token"], entry["pc_refresh_token"], entry["pc_expires_at"])
        )

    if grant_type == "refresh_token":
        presented = str(form.get("refresh_token", ""))
        payload = token_crypto.decode(presented, ttl_seconds=config.OAUTH_REFRESH_TOKEN_TTL_SECONDS)
        # Access tokens carry the same keys plus pc_access_token -- reject
        # them, or a leaked 1h access token would work as a 90-day refresh token.
        if not payload or "tenant" not in payload or not payload.get("pc_refresh_token") or "pc_access_token" in payload:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # PC ID refresh tokens are single-use and rotate on every refresh
        # (see auth.py) -- this call consumes the one embedded in the
        # presented outer refresh token, so the *new* outer tokens issued
        # below are the only place the newly-rotated one now lives. Claude
        # is expected to adopt them and discard the old refresh token, same
        # as any standard OAuth client.
        with httpx.Client(timeout=30.0) as client:
            try:
                pc_payload = auth.raw_refresh_token(client, payload["pc_refresh_token"])
            except auth.PcidAuthError:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

        pc_expires_at = time.time() + float(pc_payload.get("expires_in", 3600))
        return JSONResponse(
            _issue_tokens(
                payload["tenant"],
                pc_payload["access_token"],
                pc_payload.get("refresh_token", payload["pc_refresh_token"]),
                pc_expires_at,
            )
        )

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
