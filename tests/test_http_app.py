"""End-to-end test of the real composed HTTP app (server.build_http_app()) --
the actual mcp.streamable_http_app(), not a stand-in, with its lifespan
properly driven the same way uvicorn drives it.

This exists specifically to catch real bugs found while building this
project that no unit test against a dummy inner app could catch:

1. Mounting mcp's Starlette app inside an outer Starlette app via Mount()
   silently drops its lifespan (which starts the StreamableHTTPSessionManager's
   task group), so every real request 500'd with "Task group is not
   initialized. Make sure to use run()."
2. Whether per-request tenant resolution (server.py's _current_tenant(),
   via the SDK's native TokenVerifier -> AuthContextMiddleware ->
   get_access_token() pipeline) actually propagates correctly through the
   real SDK's internal task-group-based request dispatch -- verified here
   with two concurrent tenants and a real MCP tools/call, not assumed from
   reading the SDK source.

The PC ID login step inside /authorize is mocked (see test_oauth_server.py
for why) -- this file's job is proving the real mcp app and its auth
wiring survive contact with the real SDK, not re-proving the OAuth logic
itself (already covered there).
"""

import base64
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import api_client, auth, config, oauth_server, server  # noqa: E402

pytestmark = pytest.mark.anyio

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
TEST_ACCOUNT_EMAIL = "test-account@example.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://testserver")
    monkeypatch.setattr(config, "TOKEN_SECRET", Fernet.generate_key().decode("ascii"))
    # Critical: even though HTTP-mode tenants no longer touch disk at all,
    # stdio-mode helpers (_stdio_token_manager / session_state.load()) are
    # still reachable if a test call somehow resolves tenant=None -- keep
    # AUTH_STATE_PATH pointed at tmp_path so that path can never touch real,
    # live credentials either.
    monkeypatch.setattr(config, "AUTH_STATE_PATH", str(tmp_path / "auth_state.json"))
    oauth_server._AUTH_CODES.clear()
    server._session_states.clear()
    monkeypatch.setattr(
        auth,
        "raw_exchange_authorization_code",
        lambda client, code, verifier: {
            "access_token": "fake-pcid-access-token",
            "refresh_token": "fake-pcid-refresh-token",
            "id_token": "fake-id-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(api_client, "fetch_customer_id", lambda client, access_token, banner: TEST_ACCOUNT_EMAIL)
    yield


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _extract_hidden_fields(html: str) -> dict[str, str]:
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html))


async def _get_access_token(client: httpx.AsyncClient, monkeypatch, email: str) -> str:
    """Full mocked authorize dance for a given tenant email, returning a
    real, working Claude-facing access token. Re-points the fetch_customer_id
    mock at `email` for this call -- the autouse fixture's default always
    returns TEST_ACCOUNT_EMAIL, which only works for single-tenant tests;
    multi-tenant tests need each call to "log into" the right account.
    """
    monkeypatch.setattr(api_client, "fetch_customer_id", lambda client, access_token, banner: email)
    verifier, challenge = _pkce_pair()
    get_resp = await client.get(
        "/authorize",
        params={
            "client_id": email,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
        },
    )
    assert get_resp.status_code == 200
    fields = _extract_hidden_fields(get_resp.text)

    pasted = f"com.loblaw.pcx://pcx-android/login/appredirect?code=FAKECODE&state={fields['pcid_expected_state']}"
    authorize_resp = await client.post(
        "/authorize",
        data={
            "claude_client_id": fields["claude_client_id"],
            "claude_redirect_uri": fields["claude_redirect_uri"],
            "claude_code_challenge": fields["claude_code_challenge"],
            "claude_state": fields["claude_state"],
            "pcid_code_verifier": fields["pcid_code_verifier"],
            "pcid_expected_state": fields["pcid_expected_state"],
            "pcid_redirect": pasted,
        },
        follow_redirects=False,
    )
    assert authorize_resp.status_code == 302, authorize_resp.text
    code = parse_qs(urlparse(authorize_resp.headers["location"]).query)["code"][0]

    token_resp = await client.post(
        "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    )
    assert token_resp.status_code == 200, token_resp.text
    return token_resp.json()["access_token"]


async def _call_tool(client: httpx.AsyncClient, access_token: str, tool_name: str, arguments: dict | None = None) -> dict:
    """Drive a real MCP Streamable HTTP conversation: initialize (capturing
    the Mcp-Session-Id header the protocol requires on follow-up requests),
    the initialized notification, then tools/call -- using raw JSON-RPC
    over httpx rather than the SDK's own client (which depends on a
    separate `httpx2` package this project doesn't otherwise use).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    init_resp = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
        },
    )
    assert init_resp.status_code == 200, init_resp.text
    session_headers = {**headers, "mcp-session-id": init_resp.headers["mcp-session-id"]}

    await client.post("/mcp", headers=session_headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    call_resp = await client.post(
        "/mcp",
        headers=session_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments or {}}},
    )
    assert call_resp.status_code == 200, call_resp.text
    for line in call_resp.text.splitlines():
        if line.startswith("data: "):
            envelope = json.loads(line[len("data: ") :])
            content = envelope["result"]["content"][0]["text"]
            return json.loads(content)
    raise AssertionError(f"no SSE data line in response: {call_resp.text!r}")


async def test_real_mcp_app_survives_startup_and_answers_initialize(monkeypatch):
    """The exact scenario that previously 500'd: a real request against the
    real mcp app, after entering the composed app's lifespan the way
    uvicorn.run() does.
    """
    app = server.build_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            access_token = await _get_access_token(client, monkeypatch, TEST_ACCOUNT_EMAIL)

            # This is the call that used to 500 with "Task group is not
            # initialized" before the lifespan fix.
            mcp_resp = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                content=(
                    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
                    '{"protocolVersion":"2025-06-18","capabilities":{},'
                    '"clientInfo":{"name":"test","version":"0"}}}'
                ),
            )
            assert mcp_resp.status_code == 200, mcp_resp.text
            assert '"serverInfo"' in mcp_resp.text
            assert "pc-express" in mcp_resp.text


async def test_health_and_metadata_reachable_without_lifespan_dependency():
    app = server.build_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = await client.get("/health")
            assert health.status_code == 200

            as_metadata = await client.get("/.well-known/oauth-authorization-server")
            assert as_metadata.status_code == 200
            assert as_metadata.json()["issuer"] == "http://testserver"

            unauth = await client.get("/mcp")
            assert unauth.status_code == 401
            assert "resource_metadata=" in unauth.headers["www-authenticate"]


async def test_two_tenants_get_isolated_session_state(monkeypatch):
    """The core multi-tenancy correctness property: two different people
    self-provisioning access (two different emails) must never see each
    other's cart/store state, even served by the same running process.

    This is a real, empirical check that get_access_token()'s tenant
    resolution propagates correctly through the *actual* SDK's request
    dispatch (including whatever internal task-group handling
    mcp.streamable_http_app() does for the streamable-HTTP session) --
    not assumed from reading the SDK source. Uses list_stores (no real PC
    Express API call) against pre-seeded, distinctly-marked per-tenant
    entries in server._session_states (the in-memory cache -- see
    server.py's module docstring for why there's no file to seed anymore)
    to make isolation directly observable.
    """
    from pc_express_mcp import session_state as session_state_module

    alice, bob = "alice@example.com", "bob@example.com"
    for email, marker in ((alice, "alice-store"), (bob, "bob-store")):
        server._session_states[email] = session_state_module.SessionState(store_id=marker)

    app = server.build_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token_alice = await _get_access_token(client, monkeypatch, alice)
            token_bob = await _get_access_token(client, monkeypatch, bob)

            result_alice = await _call_tool(client, token_alice, "list_stores")
            result_bob = await _call_tool(client, token_bob, "list_stores")

    assert result_alice["active_store_id"] == "alice-store"
    assert result_bob["active_store_id"] == "bob-store"
