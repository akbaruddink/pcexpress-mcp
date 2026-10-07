# Security

This document covers the threat model for HTTP mode and the manual
security review this project went through before being made public. There
is no dedicated security team behind this project — treat this as "what was
actually checked and fixed," not a certification.

## Reporting a vulnerability

Open a [GitHub issue](../../issues). If it's something actively exploitable
against someone else's live deployment (e.g. an auth-bypass in
`oauth_server.py`), consider giving the maintainer a heads-up via the
contact info on their GitHub profile first, so there's a chance to ship a
fix before the details are public. This is an unofficial, hobby-scale
project — response times are best-effort, not guaranteed.

## Threat model summary

- **Stdio mode** (the default: Claude Desktop / Claude Code) has no network
  exposure at all. Your PC Express tokens live in a `chmod 600` file on your
  own machine, readable only by your own OS user. The main risk here is the
  same as any locally-stored OAuth token: full compromise of your own
  machine.
- **HTTP mode** is a materially different risk category — see the README's
  ["Read this before turning HTTP mode
  on"](../README.md#remotemobile-access-optional-for-the-claude-iosandroid-app)
  warning and [Architecture: HTTP mode design
  history](ARCHITECTURE.md#http-mode--multi-tenant-oauth-server-design-history)
  for the full reasoning behind its current design. In short:
  - Every tenant's PC Express credentials are encrypted directly into the
    OAuth token they hold (`token_crypto.py`), under a single operator
    secret (`PCEXPRESS_TOKEN_SECRET`) that never leaves the server. A
    leaked token can't be decrypted and reused directly against PC
    ID/Loblaws by whoever finds it.
  - A stolen token is still a **working bearer credential against this
    server** for as long as it's valid (`OAUTH_ACCESS_TOKEN_TTL_SECONDS`,
    ~1hr) — encryption does not change that, and nothing here claims
    otherwise. Mitigations: a short TTL bounds the exposure window; for a
    token known to be compromised, revoke the PC ID grant from your
    account's security page (kills refresh capability at the source);
    rotating `PCEXPRESS_TOKEN_SECRET` invalidates every issued token at
    once (blunt, not per-token, but always available).
  - Self-service means **any** PC Express account can provision itself
    access to whatever server you're running — see the README warning
    about shared blast radius (borrowed Android app credential, bandwidth,
    hosting reputation) before turning this on for anyone but yourself.

## What's *not* implemented, on purpose

- **No rate limiting or per-tenant quota.** A single abusive tenant (or a
  bug in a client) can consume this server's resources without any
  built-in backoff beyond a single 401-triggered token-refresh retry
  against PC Express itself. Acceptable for the "personal tool, occasional
  grocery order" scale this was built for; not acceptable if you're
  fronting this for a larger or untrusted audience.
- **Minimal tool-argument validation.** Tool inputs (product codes, store
  IDs, quantities) are passed close to directly into pcx-bff request
  bodies/URLs, relying on PC Express's own API to reject anything
  malformed rather than validating shapes defensively here. Judged
  acceptable because: (a) every value ends up in a JSON body or URL path
  segment sent over HTTPS via `httpx`, never interpolated into a shell
  command, SQL query, or filesystem path — the classic injection classes
  don't apply to this data flow; (b) each tenant's credentials are
  isolated (see above), so a malformed or adversarial argument can only
  ever affect the calling tenant's own account/cart, never another
  tenant's.

## Audit findings and fixes (most recent pass)

A manual review (the repo's `security-review` skill couldn't run — its
precondition diffs against a `git diff origin/HEAD...`, which needs a
configured remote this project didn't have at the time) covering
`oauth_server.py`, `auth.py`, `session_state.py`, `config.py`, and
`server.py`'s HTTP-mode paths found four confirmed issues, all fixed:

1. **`session_state.py`: `save()` didn't `chmod` the state file.** Every
   other state file in this project (`auth_state.json`, the tenant meta
   file that existed before the stateless redesign) was explicitly
   `chmod 600`; this one relied on the process umask alone. Not
   password-level secret (it holds `customer_id`/`cart_id` and cached store
   addresses/phone numbers, not tokens) but inconsistent with everything
   else here. **Fixed**: explicit `os.chmod(tmp_path, 0o600)` before the
   atomic rename, matching every other state file.
2. **`oauth_server.py`: `token_endpoint`'s `authorization_code` grant
   didn't verify `redirect_uri`.** PKCE already covers the primary
   code-interception risk, but RFC 6749/OAuth 2.1 defense-in-depth expects
   the `redirect_uri` presented at the token exchange to match the one used
   at `/authorize` for that code. **Fixed**: added that check, rejecting a
   mismatch with `invalid_grant`.
3. **TOCTOU race in tenant directory provisioning.** The file-based
   multi-tenancy design (superseded — see
   [Architecture](ARCHITECTURE.md#http-mode--multi-tenant-oauth-server-design-history))
   created a tenant's directory and then `chmod`'d it in two separate
   steps, leaving a brief window where it existed at default (umask-based)
   permissions. **Fixed at the time** via a restrictive process-wide
   `os.umask(0o077)` set at both entrypoints (`server.py`'s `main()` and
   `scripts/login.py`'s `main()`), closing that window everywhere a file or
   directory gets created, not just that one call site. **Superseded
   entirely** by the stateless redesign, which removed tenant directories
   from the codebase altogether — there's nothing left for this class of
   bug to apply to in HTTP mode. The umask hardening remains in place for
   stdio mode's `auth_state.json`/`session_state.json`.
4. **`oauth_server.py`: `_is_allowed_redirect_uri` didn't accept the IPv6
   loopback literal.** The RFC 8252 loopback-redirect allowance covered
   `localhost`/`127.0.0.1` but not `::1`, which `urlparse()` normalizes a
   bracketed `http://[::1]:port/callback` URL's `.hostname` down to (no
   brackets). Not a vulnerability by itself (a stricter allowlist than
   necessary), but a real client-compatibility gap for anyone whose loopback
   resolves to IPv6 first. **Fixed**: added `"::1"` to the allowed hostname
   set.

A later adversarial review (2026-10) confirmed and fixed:

5. **An access token was accepted as a refresh token.** Both envelopes
   carry `tenant` + `pc_refresh_token`, and Fernet's TTL check only reads
   the mint timestamp, so a leaked ~1hr access token worked as a 90-day
   refresh token. **Fixed**: the refresh grant rejects any payload carrying
   `pc_access_token`.
6. **Mid-request PC token refresh lost the rotated refresh token.** In the
   last minute of an outer token's life, a tool call refreshed PC ID's
   single-use refresh token in memory only; the outer refresh token Claude
   held then carried a consumed one, forcing a reconnect. **Fixed**: outer
   access tokens now expire with the PC token inside them (Claude refreshes
   via `/token`, which re-embeds rotation), and `EphemeralTokenManager`
   never refreshes proactively.
7. **Expired authorization codes were never purged** (each holds PC
   credentials). **Fixed**: swept on every new login.

Rejected after checking: the PC ID `state` check being skipped for a
pasted bare code is not a CSRF hole — the logged-in account must match the
claimed `client_id`, so a planted code can only provision its own owner's
account, never the victim's (and it should also fail PKCE, since it was
minted for a different verifier).

**Dependency vulnerabilities**: `pip-audit` against the full pinned
dependency set (see [Dependency policy](../README.md#dependency-policy))
found zero known vulnerabilities as of the last run recorded here. This
project pins exact versions and does not auto-update, so re-run
`pip-audit` yourself before trusting this claim if meaningful time has
passed — a clean audit today says nothing about a CVE disclosed tomorrow
against a version this project hasn't moved off of yet.

## Verified against real behavior, not just review

Consistent with how this whole project was built, the fixes above and the
stateless-credential design were checked against real, live PC Express
credentials and a real production deployment, not only unit tests:

- The full encrypt → decrypt → live `get_profile()` API call →
  force-refresh → second live API call chain was run against this
  project's own real PC ID account, confirming `EphemeralTokenManager` and
  `token_crypto.py` work end-to-end against production, including PC ID's
  single-use refresh-token rotation actually landing correctly in the
  freshly-minted outer token.
- The Docker image (`Dockerfile`, `docker-compose.yml`) was built and run
  for real (`docker build`, `docker run`, `docker compose up`) — stdio mode
  answering a real `initialize` call, HTTP mode passing its own healthcheck
  and answering `/health`/`/mcp` correctly, confirmed running as the
  unprivileged container user (`id` → `uid=1000(pcexpress)`), not just a
  `Dockerfile` that looks plausible. (It later regressed when the widget
  asset was added without a matching `COPY`; fixed and rebuilt 2026-10.)
- The redesigned code was deployed to this project's actual live VPS
  (new `PCEXPRESS_TOKEN_SECRET` generated, `pc-express-mcp.service`
  restarted) and verified over real TLS: `/health` → `200`, an
  unauthenticated `/mcp` → `401` with the correct `WWW-Authenticate`
  header, before/after the restart, with the systemd journal checked for
  errors.
