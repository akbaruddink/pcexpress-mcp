#!/usr/bin/env python3
"""One-time interactive PC ID login.

Run this yourself, on your own machine, in your own browser:

    python scripts/login.py

It opens the real PC ID login page (the same one the PC Express app uses).
You log in there normally -- this script never sees your PC Express
password, only the final OAuth redirect. After login, PC ID shows an
interstitial page (`accounts.pcid.ca/login/success?redirectURL=...`) before
your browser tries (and fails) to navigate to a `com.loblaw.pcx://...` URL
-- desktop OSes don't have the app installed to handle that custom scheme,
which is expected. Copy the address from either page (confirmed against a
real login: the interstitial's `code` is nested inside its `redirectURL`
value, not a top-level query param -- see auth.extract_pcid_redirect, which
handles both shapes) and paste it back into this script.

The resulting tokens are written to the local state file
($PCEXPRESS_STATE_DIR, default ~/.pcexpress-mcp/auth_state.json) with 0600
permissions. They are never printed in full and never sent anywhere except
directly to PC ID's own token endpoint.
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from pc_express_mcp import config
from pc_express_mcp.auth import PcidAuthError, TokenManager, build_authorize_url, extract_pcid_redirect


def _mask(token: str | None) -> str:
    if not token:
        return "<none>"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]} ({len(token)} chars)"


def main() -> int:
    import os

    os.umask(0o077)  # see server.py's main() for why this belongs at the entrypoint

    if not config.PCID_CLIENT_ID or not config.PCID_CLIENT_SECRET:
        print(
            "ERROR: PCEXPRESS_CLIENT_ID and/or PCEXPRESS_CLIENT_SECRET are not set.\n"
            "See README.md 'Authentication setup' for where to obtain the PC\n"
            "Express Android app's OAuth client id/secret (published by prior\n"
            "reverse-engineering writeups -- this repo does not ship them), then\n"
            "put both in your .env before re-running this script.",
            file=sys.stderr,
        )
        return 1

    auth_url, code_verifier, expected_state = build_authorize_url()

    print("Opening your browser to the PC ID login page...")
    print(f"If it doesn't open automatically, visit:\n\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print(
        "After you log in, PC ID shows an interstitial page at\n"
        "accounts.pcid.ca/login/success?redirectURL=... before your browser\n"
        "tries (and fails) to open a 'com.loblaw.pcx://...' URL. You can copy\n"
        "the address from EITHER page (or just the 'code' value alone) --\n"
        "paste whatever you've got below.\n"
    )
    redirected_to = input("Paste the redirect URL (or bare code) here: ").strip()

    code, state, error = extract_pcid_redirect(redirected_to)

    if error:
        print(f"PC ID returned an error: {error}", file=sys.stderr)
        return 1

    if state and state != expected_state:
        print(
            "WARNING: the 'state' value in the redirect doesn't match what we "
            "sent. This could mean the URL is stale or tampered with. Aborting.",
            file=sys.stderr,
        )
        return 1

    if not code:
        print(
            "Could not find a 'code' parameter in that. Make sure you pasted "
            "the whole redirect URL (including the query string), then try again.",
            file=sys.stderr,
        )
        return 1

    manager = TokenManager()
    try:
        with httpx.Client(timeout=30.0) as client:
            manager.exchange_code(client, code=code, code_verifier=code_verifier)
    except PcidAuthError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    state = manager._state  # noqa: SLF001 -- login script is allowed to peek for the summary
    print("\nSuccess. Tokens saved to:", config.AUTH_STATE_PATH)
    print("  access_token: ", _mask(state.access_token))
    print("  refresh_token:", _mask(state.refresh_token))
    print(
        "\nYou can now start the MCP server (see README.md 'Run the server'). "
        "It will refresh this token automatically; you shouldn't need to run "
        "this script again unless a refresh fails."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
