"""Constants for the PC ID OAuth flow and the pcx-bff API.

None of this is documented by Loblaw / Loblaw Digital. Every value below was
cross-checked against the actual, working source code (not just docs) of an
already-public reverse-engineering project -- we did not intercept or
decompile anything ourselves. Primary sources:

  - https://github.com/FireBall1725/pcexpress-mcp-server
    (pcid_config.py, pcid_token.py, pcexpress_mcp_server.py read directly.
    An earlier pass here was based only on that repo's docs summary and got
    the Site-Banner/baseSiteId value wrong -- see README "Research summary"
    for why the rest of this is still "believed correct, not independently
    verified against a live account by us".)
  - https://github.com/shmick/pcexpress-pickup (archived)
    (an unauthenticated pickup time-slots endpoint, now replaced by the
    website's checkout service -- see get_delivery_slots in api_client.py)

These are undocumented and unofficial. Loblaw can change endpoints, response
shapes, or rotate the Android app's client secret at any time without
notice -- if that happens, this whole tool breaks until someone re-derives
the new values. See README.md "Limitations & risks".
"""

from __future__ import annotations

import os

# --- PC ID (Loblaw's SSO / Oracle IDCS) OAuth2 + PKCE ---
PCID_AUTHORIZE_URL = "https://accounts.pcid.ca/oauth2/v1/authorize"
PCID_TOKEN_URL = "https://accounts.pcid.ca/oauth2/v1/token"

# The Android app's own redirect scheme. Desktop browsers can't "open" this
# after login -- you copy the URL your browser tried (and failed) to
# navigate to. See scripts/login.py.
PCID_REDIRECT_URI = "com.loblaw.pcx://pcx-android/login/appredirect"

# IDCS concatenates the resource audience ("grocery-prod") onto the
# "grocery-customer" scope with no separating space -- this is one string,
# not three separate scopes. offline_access is what yields a refresh token.
PCID_SCOPE = "openid grocery-prodgrocery-customer offline_access"

# The Android app's OAuth client id. This identifies the *app*, not you --
# functionally closer to a public API key than a password (it's the same
# value baked into every install of the official app), so we default it
# here for convenience. Overridable in case Loblaw ever issues a new one.
PCID_CLIENT_ID = os.environ.get("PCEXPRESS_CLIENT_ID", "ef9659ede6d44c7ab417f3485c11286c")

# Also defaulted, not treated as a per-user secret. This identifies the app,
# not you or your account -- and per the prior-art project's own notes, "a
# mobile confidential client can't really keep a secret" anyway, which is
# exactly why OAuth2 PKCE exists (it's designed for clients that can't
# protect a client_secret). This value is already permanently public in
# that project's repo; baking it in here adds no new exposure. Your actual
# credentials (access/refresh tokens) are never in source -- they live only
# in the gitignored state dir. See README "Why the OAuth client secret
# isn't in this repo" for the full reasoning.
PCID_CLIENT_SECRET = os.environ.get("PCEXPRESS_CLIENT_SECRET", "f470c525-c422-4070-832b-ae0a2490ea64")

# Headers the PC ID token endpoint expects in addition to the OAuth body
# (mimics the Android app's own OkHttp-based HTTP client).
PCID_TOKEN_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "okhttp/4.12.0",
    "source": "ANDROID",
    "relying-party": "pcexpress-android",
}

# --- pcx-bff (backend-for-frontend) API ---
PCX_BFF_BASE = "https://api.pcexpress.ca/pcx-bff/api/v1"

# A separate, unauthenticated endpoint (confirmed live via a real
# user-supplied curl capture, made while browsing a banner site without
# being logged into PC Express at all) that maps a postal code to the real
# per-banner courier fulfillment-location ids (e.g. "1024PCXD") -- NOT
# under pcx-bff's own path. Used by switch_cart_store to look up a real
# fulfillmentLocationId rather than guessing a banner-specific id suffix
# (confirmed to vary: "PCXD" for superstore/fortinos/loblaw, "PCXPD" for
# nofrills, in that same capture -- not assumed to generalize further).
PCX_DELIVERY_SERVICEABILITY_URL = "https://api.pcexpress.ca/v1/delivery/serviceability"

# Static web API key shipped in every banner site's client-side JS bundle
# (i.e. visible to any visitor's browser DevTools, not a per-user secret).
# Overridable in case Loblaw rotates it.
PCX_APIKEY = os.environ.get("PCEXPRESS_APIKEY", "C1xujSegT5j3ap3yexJjqhOfELwGKYvz")

# Banner -> public web domain (used for Origin/Referer headers and for
# building a checkout handoff URL). NOTE: the Site-Banner and baseSiteId
# *headers* sent on every pcx-bff request are the raw banner key itself
# (e.g. "loblaws", not "loblaw") -- confirmed against the working source,
# not just docs. See api_client.py.
# `checkout_lob` is the `lob` cookie the checkout service requires to book a
# slot -- confirmed only for superstore; other banners can list slots
# (their one-checkout hosts answer the same way) but not book until their
# value is captured.
BANNERS: dict[str, dict[str, str]] = {
    "superstore": {"domain": "www.realcanadiansuperstore.ca", "checkout_lob": "PCXSUPER"},
    "loblaws": {"domain": "www.loblaws.ca"},
    "nofrills": {"domain": "www.nofrills.ca"},
    "zehrs": {"domain": "www.zehrs.ca"},
    "independent": {"domain": "www.yourindependentgrocer.ca"},
    "tandt": {"domain": "www.tntsupermarket.com"},
}

DEFAULT_BANNER = os.environ.get("PCEXPRESS_BANNER", "superstore")
DEFAULT_STORE_ID = os.environ.get("PCEXPRESS_STORE_ID", "")

STATE_DIR = os.path.expanduser(os.environ.get("PCEXPRESS_STATE_DIR", "~/.pcexpress-mcp"))
# Used only by stdio mode (Claude Desktop / Claude Code) and scripts/login.py
# -- a single global PC Express session, no tenant concept. HTTP mode has no
# on-disk equivalent at all: each tenant's PC Express credentials live only
# inside the encrypted OAuth tokens this server issues to them (see
# token_crypto.py/oauth_server.py), and their session/cart state lives in an
# in-memory dict in server.py. Nothing tenant-specific ever touches disk.
AUTH_STATE_PATH = os.path.join(STATE_DIR, "auth_state.json")
SESSION_STATE_PATH = os.path.join(STATE_DIR, "session_state.json")


def sanitize_tenant_key(email: str) -> str | None:
    """Turn a claimed client_id into a normalized tenant key, or None if it
    doesn't look like a usable email address. Defensive: `email` is
    attacker-supplied at the point this is first called (the /authorize
    `client_id` param, before PC ID login verification) -- this used to also
    guard against path-traversal since tenant keys built filesystem paths;
    that's no longer true (HTTP mode has no per-tenant files at all), but
    the shape validation itself is still exactly what's needed here, so it
    stays as-is rather than being loosened.
    """
    key = email.strip().lower()
    if not key or "@" not in key:
        return None
    if "/" in key or "\\" in key or ".." in key or "\x00" in key:
        return None
    return key


# --- A minimal OAuth 2.1 + PKCE authorization server for HTTP mode, run by
# this project itself (see oauth_server.py). This is NOT PC Express auth --
# it's what makes Claude's remote-connector flow (which expects OAuth, not
# a bare bearer header, on accounts where the simpler `static_headers`
# option isn't exposed) able to talk to this server at all. It gates
# access to *this* server's /mcp endpoint; PC Express auth (above) is
# separate and unaffected.
#
# No Dynamic Client Registration (no /register endpoint) -- deliberately.
# Instead, Claude sends whatever's typed into "Add custom connector" >
# Advanced settings > OAuth Client ID (Client Secret left blank -- this is
# a public/PKCE client) as the client_id parameter on every /authorize
# request. This server does NOT pre-validate that value against a fixed
# allowlist -- ANY email works, self-service, no operator action needed
# per new user. /authorize consolidates a real PC ID login into itself and
# only approves the connector if the account that logs in there matches
# the claimed client_id exactly (see oauth_server.py's module docstring
# for why that match is the actual security boundary, and why "any
# successful PC ID login" alone would not be safe). On a match, that
# person's PC Express credentials are encrypted directly into the tokens
# handed back to Claude (see token_crypto.py) -- never written to disk,
# never mixed with anyone else's.

# Your server's own public https:// origin, e.g. https://pcexpress.example.com
# (no trailing slash). Required for HTTP mode -- used to build absolute URLs
# in the OAuth discovery metadata Claude fetches, which must exactly match
# what Claude used to reach the server.
PUBLIC_URL = os.environ.get("PCEXPRESS_PUBLIC_URL", "").rstrip("/")

# The single symmetric key that encrypts every tenant's PC Express
# credentials into the OAuth tokens this server hands out -- generate one
# with `python scripts/generate_secret.py`. Required for HTTP mode. This is
# an operator secret (like a Django SECRET_KEY), not per-user data: there is
# exactly one of these per deployment, never committed, never logged.
# Rotating it invalidates every currently-issued token at once (everyone
# has to reconnect) -- see token_crypto.py.
TOKEN_SECRET = os.environ.get("PCEXPRESS_TOKEN_SECRET", "")

# Kept <= PC ID's own ~1hr access-token lifetime on purpose: PC ID refresh
# tokens are single-use and rotate on every refresh (see auth.py), so a
# same-request opportunistic refresh can't safely be relied on as the
# normal path -- refresh needs to routinely go through /token's
# refresh_token grant (the one place a freshly-rotated PC ID refresh token
# can be re-embedded in a new outer token Claude will actually hold onto).
OAUTH_ACCESS_TOKEN_TTL_SECONDS = 3600
OAUTH_REFRESH_TOKEN_TTL_SECONDS = 90 * 86400
OAUTH_CODE_TTL_SECONDS = 120

# Access tokens are documented as 1hr-lived; refresh this many seconds early.
TOKEN_REFRESH_SKEW_SECONDS = 60


# --- Open Food Facts (nutrition/ingredient enrichment, unrelated to PC
# Express -- a separate, free, ODbL-licensed community database, not a
# Loblaw system). Rate limit and User-Agent requirement per their own docs:
# https://openfoodfacts.github.io/openfoodfacts-server/api/ ---
OPENFOODFACTS_BASE = "https://world.openfoodfacts.org/api/v2"
# Their docs require a descriptive User-Agent ("always use a custom
# User-Agent to identify your app, to not risk being identified as a
# bot") and suggest the format "AppName/Version (ContactEmail)". No email
# is hardcoded here -- this project is self-hosted by many different
# people, not operated centrally, so there's no single correct contact
# address to bake in. Override via PCEXPRESS_OPENFOODFACTS_USER_AGENT if
# you want your own contact info included (courteous, not required).
OPENFOODFACTS_USER_AGENT = os.environ.get(
    "PCEXPRESS_OPENFOODFACTS_USER_AGENT",
    "pc-express-mcp/0.1 (unofficial, open-source PC Express MCP server; self-hosted)",
)


def banner_info(banner: str) -> dict[str, str]:
    try:
        return BANNERS[banner]
    except KeyError as exc:
        valid = ", ".join(sorted(BANNERS))
        raise ValueError(f"Unknown banner {banner!r}. Valid banners: {valid}") from exc
