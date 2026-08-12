# Contributing

Thanks for considering it. This is a hobby-scale, unofficial project — response
times on issues/PRs are best-effort, not guaranteed. See
[docs/LEGAL.md](docs/LEGAL.md) for the project's no-affiliation/liability
posture before contributing or forking.

## Scope: what contributions should avoid

This project's legal footing rests on everything here being independently
derived from published, public reverse-engineering work and normal,
human-initiated API traffic (see [docs/RESEARCH.md](docs/RESEARCH.md) and
[docs/LEGAL.md](docs/LEGAL.md)) — not from any confidential or unauthorized
source. To keep it that way, PRs/issues should not include:

- Decompiled/disassembled Loblaw app binaries or source, or anything
  obtained by circumventing a technical protection measure.
- Any internal Loblaw document, employee communication, or non-public
  information about their systems.
- Code that automates payment/checkout submission, bypasses bot detection
  (Akamai, CAPTCHA), or scrapes rendered HTML pages instead of calling the
  same API endpoints the official app itself uses.
- Real credentials, tokens, or unredacted personal account data of any
  kind (yours or anyone else's) — see the Tests section below for the one
  narrow exception (anonymized structural snapshots) and its review
  requirement.

If you're unsure whether something you found belongs here, ask first
(open an issue) rather than opening a PR with it.

## Before you file a bug

This wraps an **undocumented, reverse-engineered API** (see
[docs/RESEARCH.md](docs/RESEARCH.md)). A lot of what looks like "this tool
is broken" is actually "Loblaw changed something upstream," which no amount
of code review here will fix — the endpoint/response shape has to be
re-derived against a live account. Before filing:

- Check [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#verification-status-precisely)
  for what's already known to be shaky (`get_available_slots`,
  `search_products`, cart add/remove/update field names).
- Include: which tool, stdio or HTTP mode, the exact error (`{"error":
  ...}` tool responses are safe to paste — they're structured, not a raw
  traceback), and your banner. **Never paste a raw access/refresh token,
  `PCEXPRESS_TOKEN_SECRET`, or `.env` contents into an issue.**
- If you can, attach a scrubbed version of the raw API response (`curl` the
  endpoint yourself with your own token, redact anything personal) — that's
  the single most useful thing for actually fixing a shape mismatch.

## Development setup

```bash
git clone <your fork>
cd pc-express-mcp
uv sync --extra dev --extra http
uv run pytest
```

See [README.md Development](README.md#development) for what each test file
covers, and [Dependency policy](README.md#dependency-policy) before adding
or bumping a dependency — this project pins exact versions deliberately.

## Making changes

- **Verify against real behavior before claiming something works.** This
  project's whole history is full of subtle bugs that only real API
  responses / a real deployment caught — a field name that's `code` here
  but `articleNumber` there, an OAuth Host-header allowlist that only
  breaks behind a real reverse proxy, a test-isolation bug that leaked into
  real production state. If you're touching `api_client.py`, `auth.py`, or
  `oauth_server.py`, prefer testing against your own real PC Express
  account/deployment over trusting that the code "looks right."
- **Minimal comments.** Code here doesn't explain *what* it does (names
  should already do that) — comments exist only for non-obvious *why*: a
  hidden API constraint, a workaround for a specific confirmed bug, a
  security tradeoff that isn't visible from the code alone. If you're
  adding a comment that just restates the next line, drop it.
- **No speculative abstraction.** Don't add config flags, hooks, or
  generalized interfaces for a use case nobody's asked for yet. Three
  similar lines beat a premature abstraction.
- **Security-sensitive paths** (`auth.py`, `oauth_server.py`,
  `token_crypto.py`, anything touching credentials or the HTTP-mode trust
  boundary) get extra scrutiny — see [docs/SECURITY.md](docs/SECURITY.md)
  for the threat model your change needs to keep intact. If you're not sure
  whether a change affects it, ask in the PR description rather than
  guessing.

## Tests

- Add tests for new tool logic, especially `_simplify_*` functions (see
  `tests/test_simplifiers.py` for the pattern — fixture data should
  reflect real, verified response shapes where possible, and say so in a
  comment if it's speculative instead).
- `tests/fixtures/raw_responses/` holds real (anonymized) API captures that
  `tests/test_snapshot_shapes.py` runs the simplifiers against as a
  regression guard — see that directory's README. If you suspect Loblaw
  changed a response shape, `python scripts/capture_snapshots.py` against
  your own account refreshes them (manual PII review required before
  committing — read the script's docstring first).
- `uv run pytest --cov=pc_express_mcp --cov-report=term-missing` before
  submitting — CI runs the same thing across Python 3.10/3.12/3.14.
- Never commit real credentials, even in a test fixture. Every existing
  test either mocks the PC ID network calls or uses obviously-fake
  values (`fake-pcid-access-token`, etc.). Snapshot fixtures are the one
  exception that legitimately holds real (anonymized) response *shapes* —
  never raw tokens, always manually reviewed before committing.

## Pull requests

- Keep them focused — one logical change per PR is easier to review and
  easier to revert if something's wrong.
- Run the full test suite locally first (`uv run pytest`).
- Describe what you verified and how (unit tests alone, or against a real
  account/deployment) — this project's README is explicit about
  verification status everywhere, and PRs should be too.
- If you're bumping a pinned dependency, say why (a specific CVE, a needed
  feature, a bug fix) — not "keeping up to date" alone, per the dependency
  policy above.

## Reporting a security issue

See [docs/SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability). Please
don't open a public issue for anything that could be actively exploited
against someone else's deployment (e.g. an auth-bypass in `oauth_server.py`)
until there's been a chance to fix it.
