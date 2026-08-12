## What this changes and why

<!-- One or two sentences. Link an issue if there is one. -->

## How you verified it

<!--
Unit tests alone, or also against a real PC Express account / a real
deployment? This project is explicit about verification status everywhere
(see docs/DEPLOYMENT.md) -- PRs should be too. If you touched
auth.py/oauth_server.py/token_crypto.py/api_client.py, prefer testing
against something real over "the code looks right."
-->

## Checklist

- [ ] `uv run pytest` passes locally
- [ ] Added/updated tests for the change (see `tests/test_simplifiers.py` for
      the fixture-data pattern if this touches a `_simplify_*` function)
- [ ] No real credentials, tokens, or `.env` contents anywhere in this diff
- [ ] If a dependency was added/bumped: pinned exact in `pyproject.toml`,
      `uv lock` run, and the reason is stated above (not just "keeping up
      to date" -- see [Dependency policy](../README.md#dependency-policy))
- [ ] If this touches the HTTP-mode trust boundary (`oauth_server.py`,
      `auth.py`, `token_crypto.py`, `server.py`'s tenant resolution): I've
      read [docs/SECURITY.md](../docs/SECURITY.md) and this doesn't weaken
      the threat model described there without calling it out explicitly
      above
