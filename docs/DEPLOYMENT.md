# Deployment notes and verification history

Actionable hosting steps (Cloudflare Tunnel, VPS + Caddy, Docker, Fly.io)
live in the README's ["Remote/mobile
access"](../README.md#remotemobile-access-optional-for-the-claude-iosandroid-app)
section. This document is the deeper "what we hit while building and
verifying this" narrative that doesn't need to be in your way when you're
just trying to get the thing running.

## Real bugs found deploying to a real VPS

**This exact VPS + Caddy setup was built and verified end-to-end** (real
VPS, real domain, real Let's Encrypt cert, real systemd service, real OAuth
flow against the live server) while building this project, which caught two
real bugs neither unit tests nor source-reading alone found:

1. `mcp`'s `streamable_http_app()` silently enables a Host-header allowlist
   (`127.0.0.1`/`localhost`/`::1` only) whenever you don't override its
   `host=` parameter — which rejects every request coming through a reverse
   proxy with a real hostname (`421 Invalid Host header`), since Caddy/nginx
   forward the original `Host` header by default. This is unrelated to (and
   not fixed by) the more commonly-known "DNS rebinding protection" toggle —
   it's a separate check in `mcp/server/lowlevel/server.py`'s
   `streamable_http_app()`. `build_http_app()` in `server.py` works around
   this by explicitly passing
   `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)`
   — safe here specifically because `RequireAuthMiddleware`/
   `oauth_server.MultiTenantTokenVerifier` (see `build_http_app()`) already
   gate every request before it reaches that layer, which is exactly the
   protection this check exists to provide for servers that *don't* have
   their own auth. If you fork this to remove that gate, put an equivalent
   protection back before disabling this.
2. Mounting `mcp.streamable_http_app()` inside an outer Starlette app via
   `Mount()` silently drops its lifespan — Starlette only runs the
   *outermost* app's own lifespan, never a mounted sub-app's, and the mcp
   SDK wires `session_manager.run()` into that specific inner app's
   lifespan. Every real request 500'd with `RuntimeError: Task group is not
   initialized. Make sure to use run().` the moment it reached the session
   manager, even though `/health` and the OAuth endpoints worked fine
   (they never touch that code path). Fixed by explicitly entering the
   inner app's `router.lifespan_context` from the outer app's own lifespan
   — see the comment above `build_http_app()` in `server.py`, and
   `tests/test_http_app.py`, which reproduces this exact failure against a
   deliberately-broken composition to confirm the regression test is real.

Verified working end-to-end, over real TLS through Caddy: unauthenticated
`/mcp` → `401` with the `WWW-Authenticate` OAuth-discovery header; the full
`/authorize` (PC ID login, account-matched) → `/token` (PKCE-verified code
exchange) → authenticated `/mcp` `initialize` call chain → a valid MCP
protocol response, using a token minted by this project's own OAuth server.

## Verification status, precisely

`uv` + a real `mcp` SDK install: the package installs cleanly, the full
test suite passes (PKCE math, the full OAuth authorization_code+PKCE+refresh
flow including the stateless token encryption itself, the real composed
HTTP app with its lifespan properly driven, and the response simplifiers
against real-shaped fixtures), all 13 tools register correctly against the
real `mcp.server.mcpserver.MCPServer` API (the SDK moved on from the older,
more commonly-documented `FastMCP` class since this project started —
caught by actually installing it).

As a full production deployment: a real instance on a real VPS, real
domain, real Let's Encrypt cert, and the entire chain from HTTPS through
Caddy through this project's own OAuth server into a live MCP `initialize`
handshake — confirmed end-to-end, including after the stateless-credential
redesign (new `PCEXPRESS_TOKEN_SECRET` generated, service restarted,
`/health` and an unauthenticated `/mcp` → `401` reverified over real TLS
post-restart, journal checked for errors).

**And**, against a real PC Express account after a real `scripts/login.py`
login: `get_profile()` (auto-discovers `cartId`/`customerId`),
`get_historical_orders()`/`get_historical_order()` (order list +
single-order detail — this is what caught the response-bloat problem
`_simplify_order_detail` now fixes), and `get_pickup_location()` (store
details + `openNowResponseData` — what `_simplify_store` and
`get_store_hours` are built from) all returned real `200`s with the
expected fields present, confirming the PC ID OAuth flow, the pcx-bff
headers (`Site-Banner`/`baseSiteId` as the raw banner key, `x-apikey`,
etc.), and these response shapes are correct against production, not just
plausible-looking. The stateless redesign's `EphemeralTokenManager` was
separately verified against this same real account: encrypt → decrypt →
live API call → forced token refresh → second live API call, all
succeeding, with the newly-rotated PC ID refresh token correctly persisted
back afterward so the account's saved session wasn't broken by the test.

**Update**: `_simplify_cart` was initially shipped unverified (the test
account's cart was empty throughout the original build) and, as expected,
was wrong on first real contact — a real user's non-empty cart (3 real
items) came back with every item's `code`/`name`/`total_price` null. The
real response nests the product two levels deeper than assumed
(`entry.offer.product`, not `entry.product`) and prices live under
`entry.prices.totalSalePrice`, not `entry.totalPrice`; order totals live on
`order.totals.totalPrice`, not the cart object itself. Fixed and
re-verified against that same real cart (see `_simplify_cart`'s docstring
in `server.py` and `tests/test_simplifiers.py` for the corrected shape).

**Still unverified**: `search_products` — never exercised against a real
account's search results during this build. `_simplify_product` (in
`server.py`) is the most likely place a field name is slightly off, on the
same pattern that `_simplify_cart` just demonstrated. `get_available_slots`
remains the
least-verified tool in this server regardless (different, unrelated
project's endpoint — see its docstring in `api_client.py`).

Docker: `docker build`, `docker run` (both stdio and HTTP mode), and
`docker compose up` were all run for real against the actual `Dockerfile`/
`docker-compose.yml` in this repo — not just written and assumed correct.
Confirmed: stdio mode answers a real MCP `initialize` call, HTTP mode
serves `/health`/`/mcp` correctly and passes its own Docker healthcheck,
and the container runs as the unprivileged `pcexpress` user
(`uid=1000`), not root. `fly.toml` was validated for TOML syntax and
schema shape but **not deploy-tested** against a real Fly.io account (that
needs your own Fly login) — if something's off, `fly launch`/`fly deploy`
will tell you plainly.
