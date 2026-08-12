# pc-express-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
<!-- Add a CI status badge here once this is pushed to GitHub:
[![tests](https://github.com/<you>/pc-express-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/<you>/pc-express-mcp/actions/workflows/tests.yml) -->

An unofficial [MCP](https://modelcontextprotocol.io) server that wraps PC Express
(Loblaws' online grocery platform — Real Canadian Superstore, Loblaws, No
Frills, Zehrs, Your Independent Grocer, T&T) so an LLM can search products
and manage your cart for you.

**Not affiliated with, endorsed by, sponsored by, or officially connected
with Loblaw Companies Limited, PC Express, or any of their banners/brands
in any way.** All trademarks referenced belong to their respective owners.
This talks to an undocumented, reverse-engineered API and can break
without notice. See [Limitations & risks](#limitations--risks) and
[docs/LEGAL.md](docs/LEGAL.md) before you rely on it, fork it, or host it
for anyone but yourself.

**This tool does not place orders or submit payment.** It fills your cart
and gives you a checkout link to finish yourself, in the PC Express app or a
browser — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#place_order-never-submits-payment).

**If you turn on the optional remote/HTTP mode, read
[Remote/mobile access](#remotemobile-access-optional-for-the-claude-iosandroid-app)
in full first** — it's open self-service multi-tenancy by default (anyone
with a PC Express account can provision themselves access to whatever
you're hosting this on), which is a materially different risk profile from
the default local mode.

<details>
<summary><strong>Contents</strong></summary>

- [Quick start](#quick-start)
- [Usage](#usage)
- [Remote/mobile access](#remotemobile-access-optional-for-the-claude-iosandroid-app)
- [Limitations & risks](#limitations--risks)
- [Development](#development)
- [Learn more](#learn-more)
- [Project layout](#project-layout)

</details>

## Quick start

**1. Install dependencies.** [`uv`](https://docs.astral.sh/uv/) is
recommended — it's what this project is built and tested with, and the only
option that installs the exact, fully-pinned dependency versions in
`uv.lock` (see [Dependency policy](#dependency-policy)):

```bash
cd pc-express-mcp
uv sync
```

Or plain `pip`:

```bash
cd pc-express-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
# or: pip install -r requirements.txt
```

Requires Python 3.10+. Add the `http` extra (`-e ".[http]"` / `uv sync
--extra http`) only if you want [remote/mobile access](#remotemobile-access-optional-for-the-claude-iosandroid-app).

**2. Configure `.env`:**

```bash
cp .env.example .env
```

- `PCEXPRESS_BANNER` / `PCEXPRESS_STORE_ID` — `store_id` has no search API
  (see [docs/RESEARCH.md](docs/RESEARCH.md)); find yours once via your
  banner's store locator (e.g.
  `https://www.realcanadiansuperstore.ca/store-locator`), or set it later
  via the `set_active_store` tool.
- `PCEXPRESS_CLIENT_ID` / `PCEXPRESS_CLIENT_SECRET` — already have working
  defaults baked into `config.py` (see
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#why-the-oauth-client-idsecret-are-baked-into-configpy));
  you normally don't need to set either.
- Load `.env` into your shell however you prefer (`export $(cat .env |
  xargs)`, `direnv`, or your MCP client's env-var passthrough).

**3. One-time login** (opens a real browser to the real PC ID login page —
your password never touches this codebase):

```bash
python scripts/login.py
```

Follow the printed instructions: log in, then paste back the
`com.loblaw.pcx://...` redirect URL it fails to open. Tokens get written to
`~/.pcexpress-mcp/auth_state.json` (or `$PCEXPRESS_STATE_DIR`), `chmod 600`.

**4. Run the server:**

```bash
python -m pc_express_mcp.server
```

**5. Point an MCP client at it.** Claude Desktop
(`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pc-express": {
      "command": "/absolute/path/to/pc-express-mcp/.venv/bin/python",
      "args": ["-m", "pc_express_mcp.server"],
      "env": {
        "PCEXPRESS_BANNER": "superstore",
        "PCEXPRESS_STORE_ID": "1531",
        "PCEXPRESS_STATE_DIR": "/absolute/path/to/home/.pcexpress-mcp"
      }
    }
  }
}
```

Run `scripts/login.py` from a normal terminal first (it needs a real
browser) — Claude Desktop launches the server directly and can't complete
the interactive login for you.

Want this reachable from the Claude iOS/Android app instead of a local
subprocess? See [Remote/mobile access](#remotemobile-access-optional-for-the-claude-iosandroid-app).

## Usage

Search results and cart items carry a ready-to-paste `photo_markdown`
field, so products actually show up as photos in the chat instead of
plain text — see [docs/RESEARCH.md](docs/RESEARCH.md#product-photos-in-chat-why-plain-markdown-not-mcp-imageui-features)
for why that's plain markdown rather than an MCP-specific image/UI
mechanism. `interactive_product_search` goes a step further on clients
that support it — see below and [docs/RESEARCH.md](docs/RESEARCH.md#interactive-product-search-widget-mcp-apps).

| Tool | Spends money? | Notes |
|---|---|---|
| `list_stores` | No | Returns known/validated stores; no search API exists |
| `set_active_store(store_id, banner?)` | No | Validates against the live pickup-locations endpoint. Warns (`cart_note`) if this account's cart is currently bound to a *different* store — call `switch_cart_store` to fix it; see [docs/RESEARCH.md](docs/RESEARCH.md#the-real-fix-switch_cart_store-found-from-a-user-supplied-real-capture) |
| `get_store_hours(store_id?)` | No | Open/closed + today's hours, fetched fresh (deliberately not cached) |
| `get_loyalty_status` | No | Real PC Optimum points balance + stamp-card status. **Not** an available-offers feed — no such endpoint exists in this API, see [docs/RESEARCH.md](docs/RESEARCH.md#loyalty-offers-no-dedicated-endpoint-found) |
| `search_products(query, size?, offset?)` | No | Requires an active store. Real, working pagination (`offset`, `total_results`, `has_more`) — see [docs/RESEARCH.md](docs/RESEARCH.md#search-pagination). Enriched per-result data (description, all product photo angles, barcode, unit price, deals) — no nutrition facts/ingredients from PC Express itself; pair a result's `barcode` with `get_nutrition_info` below |
| `interactive_product_search(query, size?)` | No | Same search, but on a client that renders [MCP Apps](https://github.com/modelcontextprotocol/ext-apps) UI (confirmed live, Add-to-Cart round trip included: Claude Desktop, claude.ai web, and the Claude mobile app), shows results as an interactive widget with per-item Add-to-Cart buttons instead of text. Degrades automatically to full `search_products`-equivalent text/photos on any client that doesn't support it, so it's always safe to call. See [docs/RESEARCH.md](docs/RESEARCH.md#interactive-product-search-widget-mcp-apps) |
| `get_nutrition_info(barcode)` | No | Nutrition facts, ingredients, allergens, Nutri-Score/NOVA grade via [Open Food Facts](https://openfoodfacts.org) (a separate, free database — not PC Express data). "Not found" is common and expected, not a bug — see [docs/RESEARCH.md](docs/RESEARCH.md#nutrition-enrichment-open-food-facts) |
| `get_cart` | No | |
| `add_to_cart(items)` | No | `items`: list of `{product_code, quantity?, fulfillment_method?}` — add/increase several products in one call. Fails with a clear `cart_store_mismatch` error (not a raw platform error) if this account's cart is bound to a different store than the active one — real PC Express constraint (one cart per account); call `switch_cart_store` to fix it, see [docs/RESEARCH.md](docs/RESEARCH.md#the-real-fix-switch_cart_store-found-from-a-user-supplied-real-capture) |
| `remove_from_cart(product_codes)` | No | `product_codes`: list — remove several products in one call |
| `update_quantity(items)` | No | `items`: list of `{product_code, quantity}` — set several quantities in one call; `quantity=0` removes that item |
| `switch_cart_store(store_id, postal_code)` | No | Re-binds the account's existing cart to a different store — the real fix for `cart_store_mismatch`, no app needed. Changes the real cart immediately (no confirm step); see [docs/RESEARCH.md](docs/RESEARCH.md#the-real-fix-switch_cart_store-found-from-a-user-supplied-real-capture) |
| `get_available_slots` | No | Least-verified endpoint in this server — treat as advisory |
| `place_order(confirm)` | **No** | Requires `confirm=True`; only validates cart + returns a checkout handoff link. Never submits payment. |
| `get_order_status(order_id?, limit?)` | No | Order history (capped by `limit`, default 10) / a specific past order |

All 15 tools carry proper MCP tool annotations (`readOnlyHint`/`destructiveHint`/
`idempotentHint`/`openWorldHint`), so any MCP client can categorize them the
way it would Gmail/other well-built MCP servers — `place_order` is flagged
non-read-only *and* destructive on purpose, since it's the closest thing to
a "spends money" action here even though it never actually submits payment.

Every response is trimmed of low-value bulk before being returned to the
model (a raw order detail is ~15x smaller after simplification, a raw store
lookup ~35x) — see [docs/RESEARCH.md](docs/RESEARCH.md#response-size-verification).

## Remote/mobile access (optional, for the Claude iOS/Android app)

By default this server runs over **stdio** (a local subprocess) — that's
what Claude Desktop and Claude Code use, and it's all you need on a laptop.
**Claude's mobile apps cannot spawn local processes; they only support
*remote* MCP servers**, added as a custom connector via claude.ai on the
web (settings then sync to mobile). To use this from the iOS/Android app,
the server has to run somewhere internet-reachable instead.

`python -m pc_express_mcp.server --http` (or `PCEXPRESS_HTTP=1`) serves the
same tools over **Streamable HTTP**, gated by a self-service, multi-tenant
OAuth 2.1 + PKCE authorization server this project runs itself
(`oauth_server.py`). Full design rationale (why not a static bearer header,
why open self-service instead of an allowlist, and the four design
iterations that got here) is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#http-mode--multi-tenant-oauth-server-design-history)
— the summary:

**Open self-service, no allowlist, no Dynamic Client Registration.**
Whatever email is typed into Claude's "Add custom connector" > Advanced
settings > OAuth Client ID field (no client_secret — PKCE covers a
public/native client, leave it blank) is treated as a *claim*, not a fact.
`/authorize` consolidates a real PC ID login into the same page and only
approves the connector if the account that logs in there matches the
claimed email exactly. On a match, that person's PC Express credentials are
encrypted directly into the OAuth token minted for them — **never mixed
with anyone else's, never written to this server's disk at all** (see
[docs/SECURITY.md](docs/SECURITY.md) for exactly what that does and doesn't
protect against). **No operator action is needed per new person** — anyone
who can log into a PC Express account can start using the server as that
account.

> **Read this before turning HTTP mode on.** Self-service multi-tenancy is
> a real scope change from "personal tool for my own account" — anyone
> with a (free, instantly-creatable) PC Express account can self-provision
> access to whatever server you're running this on. That means your
> compute, your bandwidth, and your IP/hosting reputation are now exposed
> to however many strangers' Loblaws activity ends up flowing through it.
> It also means every tenant's login/refresh traffic shares the same
> borrowed Android app `client_id`/secret — if Loblaw's fraud systems flag
> unusual volume on that credential and rotate or ban it, **every tenant
> loses access simultaneously**, not just whoever triggered it. There is no
> rate limiting, no per-tenant quota, and no abuse detection implemented
> (see [docs/SECURITY.md](docs/SECURITY.md)). If you only want to share
> this with a specific few people you trust, doing that safely would need a
> smaller change (an allowlist checked before approving `/authorize`) that
> isn't what's built here — this is genuinely open to anyone who finds the
> URL and has a PC Express account. **Deploying your own instance never
> shares infrastructure with anyone else's** — "self-hosting" here means
> exactly that: your server, your `PCEXPRESS_TOKEN_SECRET`, your tenants.

Install the extra dependency first: `pip install -e ".[http]"` (or `uv sync
--extra http`), then generate the token-encryption secret once:

```bash
python scripts/generate_secret.py
# put the output in your .env as PCEXPRESS_TOKEN_SECRET=...
```

```bash
PCEXPRESS_PUBLIC_URL=https://pcexpress.yourdomain.com \
PCEXPRESS_TOKEN_SECRET=<output from generate_secret.py> \
python -m pc_express_mcp.server --http
```

- `PCEXPRESS_PUBLIC_URL` — your server's public `https://` origin, no
  trailing slash. Required: used to build the OAuth discovery metadata
  Claude fetches, which must exactly match the URL Claude used to reach you.
- `PCEXPRESS_TOKEN_SECRET` — required. The single key that encrypts every
  tenant's PC Express credentials into their OAuth token. Generate it once
  with `scripts/generate_secret.py`; never commit it; rotating it logs
  everyone out at once.
- `PCEXPRESS_HTTP_HOST` still defaults to `127.0.0.1` (put a reverse proxy
  in front — see hosting options below); only set it to `0.0.0.0` if that
  proxy runs on a *different* host from this process.
- No account needs to be pre-configured — that's the whole point of
  self-service. There is no `PCEXPRESS_OAUTH_CLIENT_ID` anymore.

### Setting it up in Claude

1. On claude.ai web: Settings → Connectors → Add custom connector.
2. URL: `https://pcexpress.yourdomain.com/mcp` (note the `/mcp` path).
3. Open "Advanced settings" → **OAuth Client ID**: your own PC Express
   login email → leave **OAuth Client Secret blank**.
4. Claude redirects you to `/authorize`, which shows PC ID login
   instructions right there on the page: a link to the real PC ID login
   (opens in a new tab), and a box to paste back the redirect once you're
   done. If the account you log into matches step 3, it redirects back to
   Claude with a working connection. If it's a *different* PC Express
   account, it's rejected.
5. It'll sync to the iOS/Android app automatically (custom connectors can
   only be *added* on web/desktop, not from mobile — but work from mobile
   once added).

Claude Code can use the same OAuth flow directly (it declares its own
loopback callback, which `/authorize` also accepts) if you'd rather verify
against a client with a friendlier debugging story than the mobile app
before fighting the web UI.

### Before exposing this to the internet at all

- Put a real reverse proxy with a valid TLS certificate in front of it
  (Caddy, nginx + certbot, Cloudflare Tunnel). This server does not
  terminate TLS itself.
- If your hosting supports IP allowlisting, Anthropic's MCP traffic
  originates from `160.79.104.0/21` — restricting inbound access to that
  range (plus your own IP for testing) meaningfully shrinks the exposure
  versus leaving it open to the whole internet.
- Understand what's now at stake: this is genuinely internet-reachable, and
  every connected tenant's tokens flow through whatever host runs this. See
  [Limitations & risks](#limitations--risks) and
  [docs/SECURITY.md](docs/SECURITY.md).

### Self-hosting with Docker

`docker build .` alone (or `docker run` with no extra flags) defaults to
**stdio mode** — the same safe-by-default posture as running this package
directly with no arguments: no network exposure, no OAuth server. HTTP mode
is opt-in, one level up, in `docker-compose.yml`, so that choice is
explicit rather than something a container defaults into silently.

```bash
cp .env.example .env
# fill in PCEXPRESS_PUBLIC_URL, PCEXPRESS_TOKEN_SECRET
# (python scripts/generate_secret.py), and PCEXPRESS_DOMAIN (bare domain,
# for Caddy's automatic HTTPS)

docker compose up -d --build
```

This starts two containers: the app itself (no host ports published — only
reachable from Caddy over the compose network) and Caddy, which requests a
Let's Encrypt certificate for `PCEXPRESS_DOMAIN` and reverse-proxies to the
app. Point that domain's DNS at this host first.

**No volume is mounted for the app, and that's deliberate**: HTTP mode
keeps zero persistent state on disk — every tenant's PC Express credentials
live only inside the encrypted OAuth token they're holding. The app
container is fully disposable: `docker compose up -d --build` again after a
code change recreates it with zero data loss beyond forcing currently-
connected tenants to reconnect.

Verify: `curl https://your-domain/health` → `{"status":"ok"}`, then point
Claude's "Add custom connector" at `https://your-domain/mcp`.

Want stdio mode in a container instead (e.g. so Claude Desktop doesn't need
a local Python install)? Use the plain `Dockerfile` build, with
`~/.pcexpress-mcp` mounted so `scripts/login.py`'s token survives container
restarts:

```bash
docker build -t pc-express-mcp .
docker run -i --rm -v ~/.pcexpress-mcp:/home/pcexpress/.pcexpress-mcp pc-express-mcp
```

### One-click deploy (Fly.io)

`fly.toml` is checked in, pre-configured for HTTP mode — chosen because its
free/hobby tier supports this project's Dockerfile directly with no extra
build config, and needs **no persistent volume at all** (thanks to the
stateless credential design), which several alternatives require a paid
tier to get.

```bash
# Install: https://fly.io/docs/flyctl/install/
fly auth login
fly launch --no-deploy   # detects fly.toml; pick your own app name when asked

fly secrets set \
  PCEXPRESS_PUBLIC_URL=https://<your-app-name>.fly.dev \
  PCEXPRESS_TOKEN_SECRET=$(python scripts/generate_secret.py)

fly deploy
```

Verify the same way: `curl https://<your-app-name>.fly.dev/health`, then
`https://<your-app-name>.fly.dev/mcp` as the connector URL in Claude.

Want a custom domain? `fly certs add pcexpress.yourdomain.com`, point a
CNAME at your Fly app, then update the `PCEXPRESS_PUBLIC_URL` secret to
match.

Railway, Render, and similar PaaS platforms should work too (same
Dockerfile, same "no volume needed, set two env vars" shape) — Fly.io is
just the one this project ships config for.

### Hosting on a home machine: Cloudflare Tunnel

If you're running this on a machine you already own and control, a
Cloudflare Tunnel gets you a real HTTPS URL **without opening any inbound
port on your router** — `cloudflared` makes an outbound-only connection to
Cloudflare, which proxies HTTPS traffic to it. Requires a domain added to
Cloudflare (free plan is fine).

```bash
# 1. Install cloudflared (see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)

# 2. Authenticate and create a named tunnel
cloudflared tunnel login
cloudflared tunnel create pcexpress-mcp

# 3. ~/.cloudflared/config.yml
cat <<'EOF' > ~/.cloudflared/config.yml
tunnel: <tunnel-id-from-step-2>
credentials-file: /home/you/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: pcexpress.yourdomain.com
    service: http://127.0.0.1:8090
  - service: http_status:404
EOF

# 4. Point DNS at the tunnel
cloudflared tunnel route dns pcexpress-mcp pcexpress.yourdomain.com

# 5. Run the tunnel (add --service install-style if you want it to survive reboots)
cloudflared tunnel run pcexpress-mcp
```

In a separate terminal (or a systemd unit), run the MCP server itself.
**Keep `PCEXPRESS_HTTP_HOST` at its default (`127.0.0.1`)** — `cloudflared`
connects to it locally, so it should never need to listen on
`0.0.0.0`/your LAN. `0.0.0.0` is only for a reverse proxy or container on a
*separate* host:

```bash
PCEXPRESS_PUBLIC_URL=https://pcexpress.yourdomain.com \
python -m pc_express_mcp.server --http
```

Verify from an outside network (e.g. your phone on cellular data) before
touching Claude at all: `curl https://pcexpress.yourdomain.com/health` →
`{"status":"ok"}`.

### Hosting on a VPS with a public IP (Caddy)

If you already have a VPS (no NAT/router to work around), point an A
record at the VPS's IP, run Caddy as a reverse proxy in front of the server
(still bound to `127.0.0.1`, since Caddy runs on the same box), and Caddy
handles Let's Encrypt automatically:

```
# /etc/caddy/Caddyfile
pcexpress.yourdomain.com {
	reverse_proxy 127.0.0.1:8090
}
```

Run the MCP server itself as a systemd service so it survives reboots and
restarts on crash — an `EnvironmentFile` pointed at a `chmod 600` file
holding `PCEXPRESS_PUBLIC_URL` and `PCEXPRESS_TOKEN_SECRET` (plus
`PCEXPRESS_HTTP_HOST=127.0.0.1`/`PCEXPRESS_HTTP_PORT=8090`, both already
defaults) keeps the unit file itself free of anything sensitive.
`sudo systemctl reload caddy` after editing the Caddyfile; it obtains the
cert on first request to the new domain. This exact setup is what this
project's own live deployment runs — see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the real bugs that surfaced
building it and the full verification record.

## Limitations & risks

- **HTTP mode is a materially different risk than the default stdio mode.**
  Only run `--http` if you've read [Remote/mobile access](#remotemobile-access-optional-for-the-claude-iosandroid-app)
  in full and deliberately want your PC Express tokens on an
  internet-reachable host. Default to stdio unless you specifically need
  mobile access.
- **Reverse-engineered, undocumented API.** Every endpoint and OAuth
  constant here was sourced from third-party writeups, not official docs
  (see [docs/RESEARCH.md](docs/RESEARCH.md)). Loblaw can change endpoints,
  response shapes, or rotate the Android app's client secret at any time,
  silently breaking this tool.
- **Terms of Service risk.** Automated access to a retailer's ordering
  platform via a reverse-engineered API is very likely outside what
  Loblaws' website/app Terms of Use contemplate as acceptable use, even for
  personal, low-volume use on your own account. This project does not
  attempt to bypass Akamai or any other bot-detection system (the one-time
  login is a real human, in a real browser); nonetheless, using an
  unofficial API at all carries some risk of rate-limiting, CAPTCHA
  challenges, or account action if Loblaw's systems flag the traffic
  pattern. Use at your own risk, on your own account, at low volume. See
  [docs/LEGAL.md](docs/LEGAL.md) for the full disclaimer, no-affiliation
  statement, and what this project deliberately does and doesn't do.
- **`get_available_slots` is the least verified** piece here — see
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#verification-status-precisely)
  for exactly what has and hasn't been confirmed against a live account.
  `search_products`/`get_cart` are both now verified against real,
  non-empty results (`search_products` was extended with real pagination
  and enriched per-product data after a direct request; `get_cart` was
  fixed after a real bug report — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).
- **No nutrition facts or ingredients list is available anywhere in this
  API.** `search_products` results were checked for this specifically
  (`ingredients` is null on every real result seen) and the only
  single-product-detail endpoint this client has confirmed does not work
  (see `api_client.get_product`'s docstring). If you need nutrition data,
  pair a result's `barcode` with an external source.
- **No store-search API.** `list_stores` can only show stores you've
  already validated via `set_active_store`.
- **Single account, single session assumption** (stdio mode). Running two
  instances of this server (or this server plus `login.py`) concurrently
  against the same refresh token will cause one of them to fail with an
  auth error, because refresh tokens are single-use/rotating.
- **One active cart per PC Express account, account-wide** — a real
  platform constraint confirmed live. If your account is shared across
  locations (e.g. family members ordering from different stores), this
  tool will tell you clearly when that's the problem (`cart_note` from
  `set_active_store`, `cart_store_mismatch` from a failed add) and can now
  actually fix it: call `switch_cart_store(store_id, postal_code)` to
  re-bind the cart to the store you need, no app step required. See
  [docs/RESEARCH.md](docs/RESEARCH.md#the-real-fix-switch_cart_store-found-from-a-user-supplied-real-capture).
- **Rate limiting is unknown and undocumented**, and this project doesn't
  implement any beyond a single 401-triggered token refresh retry. Keep
  usage to the "occasional grocery order" volume this was built for. See
  [docs/SECURITY.md](docs/SECURITY.md) for the full threat model, including
  what HTTP mode does and doesn't protect against.

## Development

```bash
uv sync --extra dev --extra http   # installs the exact versions in uv.lock
uv run pytest
```

Plain `pip`/`venv` work too (`pip install -e ".[dev,http]"` then `pytest`),
but only `uv sync` reproduces the exact, fully-pinned dependency tree this
project was actually tested against.

- `tests/test_auth_pkce.py` — pure logic (PKCE generation, banner lookup),
  no network or credentials needed.
- `tests/test_oauth_server.py` — the full OAuth flow (discovery metadata,
  `/authorize`'s PC-ID-account-match consent, PKCE-verified `/token`
  exchange, single-use codes, refresh rotation, the mismatched-account
  rejection path, and the stateless token encryption itself — tampering,
  wrong secret, PC-ID-driven refresh rotation) against a dummy inner app via
  `httpx`'s ASGI transport. The PC ID exchange itself is mocked — never
  hits the real network or touches real credentials.
- `tests/test_http_app.py` — the same flow end-to-end against the *real*
  composed app (`server.build_http_app()`, including the actual
  `mcp.streamable_http_app()`), with its lifespan driven the same way
  uvicorn drives it. This is what caught the lifespan bug in
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- `tests/test_simplifiers.py` — the response-simplifier functions against
  fixture data reconstructed from real, live-verified response shapes.
- `tests/test_snapshot_shapes.py` — the same simplifiers against real,
  anonymized API response captures checked into
  `tests/fixtures/raw_responses/` (see that directory's README and
  `scripts/capture_snapshots.py`) — a regression guard against exactly the
  failure mode that shipped once already: a simplifier silently returning
  nulls against a real response its own hand-written test fixture never
  actually exercised.

None of these need network access or real credentials (the snapshot files
are static, already-captured data, not a live call). There is no
integration test suite against the live PC Express API, for obvious
reasons — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#verification-status-precisely)
for what's been verified manually instead.

Coverage: `uv run pytest --cov=pc_express_mcp --cov-report=term-missing`
(also runs in CI, uploaded as a build artifact). Coverage is concentrated in
the pure-logic modules (crypto, auth parsing, OAuth flow, response
simplifiers) by design — the tool bodies and the live API client are
exercised by manual live verification instead, not mocked out into a false
sense of coverage.

### Dependency policy

Every dependency in `pyproject.toml` is pinned exact (`==`), not `>=` —
this project does not track new releases automatically. `uv.lock`
(committed) pins the *entire* transitive dependency tree the same way; CI
installs from it with `uv sync --locked`, which fails loudly if the lock
file and `pyproject.toml` ever drift apart instead of silently
re-resolving. Bumping a dependency is a deliberate action here: update the
version, run `uv lock`, re-run the full test suite, and (for anything
touching the HTTP/OAuth/crypto path) a live smoke check — never an
incidental side effect of a fresh install some time later.
`requirements.txt` mirrors the direct dependencies for non-uv `pip install
-r requirements.txt` users, but can't pin the transitive closure the way
`uv.lock` does — prefer `uv sync` or `pip install -e .` when you can.

Want to contribute? See [CONTRIBUTING.md](CONTRIBUTING.md).

## Learn more

- [docs/RESEARCH.md](docs/RESEARCH.md) — how every undocumented endpoint
  and constant here was sourced, and confidence levels per endpoint.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — authentication design, why
  `place_order` never submits payment, and the multi-tenant OAuth server's
  full design history (including the stateless-credential redesign).
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — real bugs found deploying to a
  real VPS, and the precise, honest record of what has and hasn't been
  verified against a live account/deployment.
- [docs/SECURITY.md](docs/SECURITY.md) — threat model, what's deliberately
  not implemented and why, and the audit findings from this project's
  security review.
- [docs/LEGAL.md](docs/LEGAL.md) — no-affiliation/trademark disclaimer,
  what this project does and deliberately doesn't do, Terms of Service
  risk, and no-warranty/liability statement. Not legal advice.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute.

## Project layout

```
pc-express-mcp/
├── pc_express_mcp/
│   ├── config.py         # OAuth + API constants (see docs/RESEARCH.md)
│   ├── auth.py            # PKCE login, token refresh/rotation, local state
│   ├── token_crypto.py    # stateless encrypted-token encode/decode (HTTP mode)
│   ├── oauth_server.py    # self-service multi-tenant OAuth 2.1 + PKCE server
│   ├── api_client.py      # pcx-bff HTTP client
│   ├── session_state.py   # active banner/store/cart (non-secret)
│   └── server.py          # MCP tool definitions
├── scripts/
│   ├── login.py            # one-time interactive PC ID login
│   └── generate_secret.py  # generates PCEXPRESS_TOKEN_SECRET for HTTP mode
├── tests/
├── docs/                  # research, architecture, deployment, security
├── Dockerfile             # defaults to stdio mode
├── docker-compose.yml     # HTTP mode + Caddy (automatic HTTPS)
├── fly.toml               # one-click deploy config (Fly.io)
├── .env.example
└── .gitignore             # excludes .env and all local state/token files
```
