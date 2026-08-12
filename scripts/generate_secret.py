#!/usr/bin/env python3
"""Print a fresh PCEXPRESS_TOKEN_SECRET for HTTP mode.

Run once per deployment:

    python scripts/generate_secret.py

...and put the output in your .env as PCEXPRESS_TOKEN_SECRET=<value>. This
single value is what encrypts/decrypts every tenant's PC Express credentials
inside the OAuth tokens this server issues to Claude -- see
pc_express_mcp/token_crypto.py and oauth_server.py's module docstring for
the design.

Anyone with this value can decrypt those tokens, so treat it like any other
server secret: never commit it, never share it, and generating a new one
invalidates every currently-issued token at once (every connected user has
to reconnect).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode("ascii"))
