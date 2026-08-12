# Architecture

## Authentication design (PC Express / PC ID)

Given prior art and the goal of "get everything into the cart, finish
checkout yourself in the phone app," this uses **pure token-based auth** —
no persistent browser automation at all:

1. **One-time login, in your own real browser.** `scripts/login.py` opens
   the actual PC ID login page (OAuth2 + PKCE). You log in normally; your
   password never touches this codebase. PC ID redirects to a
   `com.loblaw.pcx://...` URL your desktop browser can't open — you copy
   that URL and paste it back into the script.
2. The script exchanges the authorization code for an **access token**
   (~1hr, per the docs) and a **refresh token**, and writes both to a local
   state file (`~/.pcexpress-mcp/auth_state.json` by default), `chmod 600`.
3. From then on, the MCP server refreshes the access token itself before it
   expires (or immediately on any HTTP 401). PC ID refresh tokens are
   **single-use and rotate on every refresh** — the server persists the new
   one every time. You should not need to run `login.py` again unless a
   refresh is explicitly rejected (expired/revoked/used twice), at which
   point every tool call returns a clear `{"error": "auth_required", ...}`
   telling you to re-run it.

Secrets are **never** hardcoded, never logged, and never printed in full —
`login.py` only prints masked previews (`abcd...wxyz (87 chars)`).

### Why the OAuth client id/secret are baked into config.py

Both `client_id` and `client_secret` identify the *PC Express Android app*
to PC ID — not you, not your account. They're the same two values shipped
inside every install of the real app, extracted once by FireBall1725's
project (Android emulator + mitmproxy + forcing a token refresh; see their
`AUTH_NOTES.md`) and permanently public in that repo since. Per that
project's own notes, "a mobile confidential client can't really keep a
secret" anyway — that's the whole reason OAuth2 PKCE exists, which is what
actually protects this flow. Baking these two values in here adds no new
exposure beyond what's already public, and matches what prior art itself
does. Both remain overridable via `PCEXPRESS_CLIENT_ID` /
`PCEXPRESS_CLIENT_SECRET` env vars if Loblaw ever rotates them.

**What's genuinely yours and genuinely secret** is the access/refresh
token pair produced by *your* login in `scripts/login.py` — those are
never hardcoded, never defaulted, never logged, and live only in the
gitignored state dir (`~/.pcexpress-mcp/auth_state.json`, `chmod 600`).
That's the distinction this project draws: app-identity constants vs.
your-account credentials.

## HTTP mode / multi-tenant OAuth server design history

`oauth_server.py` — the self-service OAuth 2.1 + PKCE authorization server
that gates HTTP mode — went through four designs before landing where it
is now, each change forced by hitting a real failure or a real design gap
rather than anticipated in advance:

1. **A fixed API key/bearer header** (`static_headers`, Claude's built-in
   auth type for this) was tried first, but the "Add custom connector"
   dialog on a Free/Pro/Max account doesn't expose a header field at all —
   Claude went straight to attempting OAuth Dynamic Client Registration
   instead, which fails outright against a server with no `/register`
   endpoint.
2. **A real OAuth server with an arbitrary passphrase** as the `/authorize`
   consent gate was built next — worked, but meant remembering yet another
   secret to protect a proxy that already sits behind PC Express's own
   login.
3. **A single fixed account, gated by a real PC ID login instead of a
   passphrase** replaced that — no separate secret, `/authorize` just
   required logging into one pre-configured PC Express account. Replaced
   again by open self-service multi-tenancy, to support more than one
   person using the same server.
4. **Per-tenant credential files on disk** (`~/.pcexpress-mcp/tenants/<email>/`)
   was multi-tenancy's first shape — it worked, but meant a real credential
   file per person, filesystem permissions to get right, and directories to
   clean up. It also directly caused a real bug: a precomputed
   `TENANTS_DIR`-style path constant didn't respect test monkeypatching of
   `STATE_DIR`, and test tenant directories leaked into the *real*
   production state dir on the live deployment before being caught. That
   bug shaped the design that replaced it.
5. **Stateless, encrypted-token credentials (current design).** No
   server-side tenant storage at all. Each tenant's PC Express credentials
   are encrypted directly into the OAuth token Claude holds for them
   (`token_crypto.py`, Fernet/AES128-CBC+HMAC-SHA256 under a single
   operator secret, `PCEXPRESS_TOKEN_SECRET`) — decrypting that token *is*
   the lookup, there's no server-side record to look anything up in, and
   the entire bug class above (per-tenant file permissions, TOCTOU races,
   leaked test directories) is eliminated by construction rather than
   patched. Session/cart state (non-sensitive) lives in an in-memory
   per-tenant cache instead, since it's fine to lose and rebuild on
   restart. See `oauth_server.py`'s module docstring for the full security
   model — what encryption does and doesn't protect against, why a stolen
   token is still a working bearer credential against the server itself,
   and why revocation is deferred to the short access-token TTL plus PC
   ID's own account-security page rather than reimplemented here.

This design was verified against real, live PC Express credentials during
development: encrypt → decrypt round trip, a real `get_profile()` API call
through the resulting `EphemeralTokenManager`, and a real PC ID token
refresh cycle (confirming the newly-rotated refresh token gets correctly
re-embedded, since PC ID's refresh tokens are single-use) — not just
exercised against mocks.

## `place_order` never submits payment

You told us you're fine completing checkout yourself on your phone, so
`place_order`:

1. Requires `confirm=True` — calling it without that returns an error and
   does nothing, so no tool-use accident can trigger it.
2. Validates the cart is non-empty.
3. Returns a cart summary plus a checkout URL (`https://{your-banner-domain}/checkout`)
   for you to open on your phone or in a browser where you're already
   logged in, to pick a slot, confirm substitutions, and pay.

It never calls a payment/order-submission endpoint, because no such
endpoint is documented anywhere we could find. If you later want true
end-to-end automated ordering, that would mean reverse-engineering the
checkout flow live against your own account (through your own browser's
DevTools Network tab) — a materially higher-risk undertaking (real money,
undocumented fraud/anti-bot checks) that's out of scope for this build.

## Response simplification

Every tool's raw pcx-bff response is trimmed by a `_simplify_*` function in
`server.py` before being returned — see [Research summary: response-size
verification](RESEARCH.md#response-size-verification) for the measured
before/after sizes, and each simplifier's own docstring for exactly which
fields were verified live vs. speculative.
