"""Functional tests for the self-service, multi-tenant OAuth server
(oauth_server.py): the consolidated PC-ID-login-based /authorize flow
(claimed-client_id-vs-logged-in-account match, not a fixed allowlist or a
passphrase), the stateless encrypted-token issuance (no per-tenant files --
see token_crypto.py), the Claude-facing authorization_code + PKCE +
refresh_token flow, discovery metadata shape, and the resulting per-tenant
gate -- via httpx's ASGI transport against a real Starlette app composed
similarly to (but simpler than) server.build_http_app(): this file uses a
minimal dummy /mcp gate built directly on oauth_server.get_token_tenant()
rather than the full SDK-native middleware stack, keeping this file's scope
to oauth_server.py's own logic. The full SDK composition (get_access_token()
propagating through real request dispatch, two-tenant isolation on real
tool calls) is covered end-to-end in tests/test_http_app.py instead.

The PC ID token exchange/refresh and the profile-email lookup are mocked
(via monkeypatch on auth.raw_exchange_authorization_code /
auth.raw_refresh_token / api_client.fetch_customer_id) so these tests never
hit the real network -- those functions are exercised for real in
api_client.py/auth.py's own manual verification against the live
deployment, not here.
"""

import base64
import hashlib
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import api_client, auth, config, oauth_server, token_crypto  # noqa: E402

pytestmark = pytest.mark.anyio

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://testserver")
    monkeypatch.setattr(config, "TOKEN_SECRET", Fernet.generate_key().decode("ascii"))
    oauth_server._AUTH_CODES.clear()
    yield


async def _dummy_inner_app(scope, receive, send):
    """A minimal /mcp stand-in gated directly on oauth_server.get_token_tenant()
    -- not the full SDK middleware stack (see module docstring). Echoes the
    resolved tenant back in the body so tests can assert on it.
    """
    headers = dict(scope.get("headers") or [])
    presented = headers.get(b"authorization", b"").decode("latin-1")
    token = presented[len("Bearer ") :] if presented.startswith("Bearer ") else ""
    tenant = oauth_server.get_token_tenant(token)
    if not tenant:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": f"inner-app-reached:{tenant}".encode()})


def _build_app():
    app = Starlette(
        routes=[
            Route("/.well-known/oauth-authorization-server", oauth_server.authorization_server_metadata),
            Route("/.well-known/oauth-protected-resource", oauth_server.protected_resource_metadata),
            Route("/authorize", oauth_server.authorize_get, methods=["GET"]),
            Route("/authorize", oauth_server.authorize_post, methods=["POST"]),
            Route("/token", oauth_server.token_endpoint, methods=["POST"]),
        ]
    )
    app.router.mount("/", _dummy_inner_app)
    return app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _extract_hidden_fields(html: str) -> dict[str, str]:
    import re

    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html))


def _mock_pcid_login(monkeypatch, *, logged_in_as: str, pc_access_token: str = "fake-pcid-access-token") -> None:
    """Mock the two calls authorize_post makes out to PC ID, so tests never
    hit the real network. `logged_in_as` simulates *whose* account the
    (mocked) PC ID login belongs to -- pass a value different from the
    claimed client_id to simulate the account-mismatch rejection path.
    """
    monkeypatch.setattr(
        auth,
        "raw_exchange_authorization_code",
        lambda client, code, verifier: {
            "access_token": pc_access_token,
            "refresh_token": "fake-pcid-refresh-token",
            "id_token": "fake-id-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(api_client, "fetch_customer_id", lambda client, access_token, banner: logged_in_as)


async def _do_authorize_post(
    client: httpx.AsyncClient,
    monkeypatch,
    challenge: str,
    *,
    client_id: str = ALICE,
    state: str = "xyz",
    logged_in_as: str = ALICE,
    pc_access_token: str = "fake-pcid-access-token",
) -> httpx.Response:
    get_resp = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
    )
    assert get_resp.status_code == 200
    fields = _extract_hidden_fields(get_resp.text)

    _mock_pcid_login(monkeypatch, logged_in_as=logged_in_as, pc_access_token=pc_access_token)

    pasted = f"com.loblaw.pcx://pcx-android/login/appredirect?code=FAKECODE&state={fields['pcid_expected_state']}"
    return await client.post(
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


async def _get_code(client: httpx.AsyncClient, monkeypatch, challenge: str, client_id: str = ALICE, state: str = "xyz") -> str:
    resp = await _do_authorize_post(client, monkeypatch, challenge, client_id=client_id, state=state, logged_in_as=client_id)
    assert resp.status_code == 302, resp.text
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    return qs["code"][0]


async def test_discovery_metadata_shape(client):
    r1 = await client.get("/.well-known/oauth-authorization-server")
    assert r1.status_code == 200
    body = r1.json()
    assert body["issuer"] == "http://testserver"
    assert body["authorization_endpoint"] == "http://testserver/authorize"
    assert body["token_endpoint"] == "http://testserver/token"
    assert "registration_endpoint" not in body  # no DCR, by design
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "offline_access" in body["scopes_supported"]

    r2 = await client.get("/.well-known/oauth-protected-resource")
    assert r2.status_code == 200
    resource_body = r2.json()
    assert resource_body["resource"] == "http://testserver/mcp"
    assert resource_body["authorization_servers"] == ["http://testserver"]


async def test_mcp_endpoint_requires_token(client):
    resp = await client.get("/mcp")
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]


async def test_authorize_rejects_non_email_client_id(client):
    """No fixed allowlist -- any client_id is accepted at this stage as long
    as it's shaped like an email; garbage is rejected immediately rather
    than wasting a PC ID login attempt on something that could never match.
    """
    _, challenge = _pkce_pair()
    resp = await client.get(
        "/authorize",
        params={
            "client_id": "not-an-email",
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400


async def test_authorize_rejects_disallowed_redirect_uri(client):
    _, challenge = _pkce_pair()
    resp = await client.get(
        "/authorize",
        params={
            "client_id": ALICE,
            "response_type": "code",
            "redirect_uri": "https://evil.example.com/steal",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400


async def test_authorize_allows_ipv6_loopback_redirect_uri(client):
    _, challenge = _pkce_pair()
    resp = await client.get(
        "/authorize",
        params={
            "client_id": ALICE,
            "response_type": "code",
            "redirect_uri": "http://[::1]:54321/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 200


async def test_authorize_get_renders_pcid_login_flow(client):
    _, challenge = _pkce_pair()
    resp = await client.get(
        "/authorize",
        params={
            "client_id": ALICE,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
    )
    assert resp.status_code == 200
    assert "Log into PC Express" in resp.text
    assert ALICE in resp.text  # echoed back so the user knows which account to log into
    assert 'name="pcid_redirect"' in resp.text
    assert "passphrase" not in resp.text.lower()
    fields = _extract_hidden_fields(resp.text)
    assert fields["claude_code_challenge"] == challenge
    assert "pcid_code_verifier" in fields
    assert "pcid_expected_state" in fields


async def test_authorize_rejects_pcid_login_error(client, monkeypatch):
    _, challenge = _pkce_pair()
    get_resp = await client.get(
        "/authorize",
        params={
            "client_id": ALICE,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
    )
    fields = _extract_hidden_fields(get_resp.text)
    resp = await client.post(
        "/authorize",
        data={
            "claude_client_id": fields["claude_client_id"],
            "claude_redirect_uri": fields["claude_redirect_uri"],
            "claude_code_challenge": fields["claude_code_challenge"],
            "claude_state": fields["claude_state"],
            "pcid_code_verifier": fields["pcid_code_verifier"],
            "pcid_expected_state": fields["pcid_expected_state"],
            "pcid_redirect": "com.loblaw.pcx://pcx-android/login/appredirect?error=access_denied&error_description=cancelled",
        },
    )
    assert resp.status_code == 401


async def test_authorize_rejects_stale_pcid_state(client, monkeypatch):
    _, challenge = _pkce_pair()
    get_resp = await client.get(
        "/authorize",
        params={
            "client_id": ALICE,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
    )
    fields = _extract_hidden_fields(get_resp.text)
    _mock_pcid_login(monkeypatch, logged_in_as=ALICE)
    resp = await client.post(
        "/authorize",
        data={
            "claude_client_id": fields["claude_client_id"],
            "claude_redirect_uri": fields["claude_redirect_uri"],
            "claude_code_challenge": fields["claude_code_challenge"],
            "claude_state": fields["claude_state"],
            "pcid_code_verifier": fields["pcid_code_verifier"],
            "pcid_expected_state": fields["pcid_expected_state"],
            "pcid_redirect": "com.loblaw.pcx://pcx-android/login/appredirect?code=FAKECODE&state=some-other-state",
        },
    )
    assert resp.status_code == 401


async def test_authorize_rejects_mismatched_pcid_account(client, monkeypatch):
    """The actual security boundary: claiming client_id=alice but logging
    into a *different* PC Express account must be rejected -- this is what
    stops "any successful PC ID login" (which anyone can do with their own
    free account) from being sufficient on its own.
    """
    _, challenge = _pkce_pair()
    resp = await _do_authorize_post(client, monkeypatch, challenge, client_id=ALICE, logged_in_as="mallory@example.com")
    assert resp.status_code == 403


async def test_authorize_approves_matching_account_self_service_no_allowlist(client, monkeypatch):
    """The self-service property: BOB, who was never pre-configured
    anywhere, gets approved just by proving he can log into bob@example.com
    -- no operator action needed per new tenant.
    """
    _, challenge = _pkce_pair()
    resp = await _do_authorize_post(client, monkeypatch, challenge, client_id=BOB, logged_in_as=BOB)
    assert resp.status_code == 302
    assert "code=" in resp.headers["location"]


async def test_authorize_code_carries_pc_credentials_not_a_lookup_key(client, monkeypatch):
    """The stateless property: the auth code (and, transitively, the token
    minted from it) carries the actual PC Express credentials in-band --
    there is no server-side tenant record anywhere to look them up from.
    """
    _, challenge = _pkce_pair()
    resp = await _do_authorize_post(client, monkeypatch, challenge, client_id=ALICE, logged_in_as=ALICE)
    assert resp.status_code == 302
    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    entry = oauth_server._AUTH_CODES[code]
    assert entry["tenant"] == config.sanitize_tenant_key(ALICE)
    assert entry["pc_access_token"] == "fake-pcid-access-token"
    assert entry["pc_refresh_token"] == "fake-pcid-refresh-token"


async def test_two_tenants_get_distinct_encrypted_credentials(client, monkeypatch):
    _, challenge_a = _pkce_pair()
    _, challenge_b = _pkce_pair()

    resp_a = await _do_authorize_post(
        client, monkeypatch, challenge_a, client_id=ALICE, state="a", logged_in_as=ALICE
    )
    resp_b = await _do_authorize_post(
        client, monkeypatch, challenge_b, client_id=BOB, state="b", logged_in_as=BOB, pc_access_token="bobs-pcid-access-token"
    )
    assert resp_a.status_code == 302
    assert resp_b.status_code == 302

    code_a = parse_qs(urlparse(resp_a.headers["location"]).query)["code"][0]
    code_b = parse_qs(urlparse(resp_b.headers["location"]).query)["code"][0]
    assert oauth_server._AUTH_CODES[code_a]["tenant"] == ALICE
    assert oauth_server._AUTH_CODES[code_b]["tenant"] == BOB
    assert oauth_server._AUTH_CODES[code_a]["pc_access_token"] != oauth_server._AUTH_CODES[code_b]["pc_access_token"]


async def test_token_exchange_rejects_wrong_code_verifier(client, monkeypatch):
    verifier, challenge = _pkce_pair()
    code = await _get_code(client, monkeypatch, challenge)
    resp = await client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "code_verifier": "not-the-right-verifier"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_code_is_single_use(client, monkeypatch):
    verifier, challenge = _pkce_pair()
    code = await _get_code(client, monkeypatch, challenge)

    ok = await client.post("/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier})
    assert ok.status_code == 200

    replay = await client.post(
        "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


async def test_access_token_decrypts_to_tenant_and_pc_credentials(client, monkeypatch):
    """The actual mechanism, not just that /mcp happens to resolve the right
    tenant: decode the minted token directly and check its payload shape.
    """
    verifier, challenge = _pkce_pair()
    code = await _get_code(client, monkeypatch, challenge, client_id=ALICE)
    token_resp = await client.post(
        "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    )
    access_token = token_resp.json()["access_token"]

    payload = token_crypto.decode(access_token)
    assert payload["tenant"] == config.sanitize_tenant_key(ALICE)
    assert payload["pc_access_token"] == "fake-pcid-access-token"
    assert payload["pc_refresh_token"] == "fake-pcid-refresh-token"
    assert "pc_expires_at" in payload


async def test_tampered_token_is_rejected(client, monkeypatch):
    verifier, challenge = _pkce_pair()
    code = await _get_code(client, monkeypatch, challenge, client_id=ALICE)
    token_resp = await client.post(
        "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    )
    access_token = token_resp.json()["access_token"]
    tampered = access_token[:-1] + ("A" if access_token[-1] != "A" else "B")

    assert oauth_server.decode_access_token(tampered) is None
    resp = await client.get("/mcp", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


async def test_token_encrypted_under_a_different_secret_is_rejected(client, monkeypatch):
    """Simulates a token minted by a different deployment/secret -- proves
    decryption is actually secret-dependent, not just checking shape.
    """
    verifier, challenge = _pkce_pair()
    code = await _get_code(client, monkeypatch, challenge, client_id=ALICE)
    token_resp = await client.post(
        "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    )
    access_token = token_resp.json()["access_token"]

    other_fernet_token = Fernet(Fernet.generate_key()).encrypt(b'{"tenant":"x","pc_access_token":"y"}').decode()
    assert oauth_server.decode_access_token(other_fernet_token) is None
    # sanity: the real token still works under the real (test-fixture) secret
    assert oauth_server.decode_access_token(access_token) is not None


async def test_full_flow_then_access_protected_endpoint_then_refresh(client, monkeypatch):
    verifier, challenge = _pkce_pair()
    code = await _get_code(client, monkeypatch, challenge, client_id=ALICE)

    token_resp = await client.post(
        "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    )
    assert token_resp.status_code == 200
    tokens = token_resp.json()
    assert tokens["token_type"] == "Bearer"
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # The minted access token actually works against the protected app,
    # and resolves back to the right tenant.
    mcp_resp = await client.get("/mcp", headers={"Authorization": f"Bearer {access_token}"})
    assert mcp_resp.status_code == 200
    assert mcp_resp.text == f"inner-app-reached:{ALICE}"

    # Our outer refresh_token isn't single-use by itself (nothing server-side
    # marks it "consumed" -- there's no server-side record at all, see
    # module docstring). Single-use enforcement is inherited entirely from
    # PC ID's own refresh tokens actually rotating on use (confirmed real
    # behaviour, see auth.py's docstring) -- so the mock here must simulate
    # that rotation for the replay-is-rejected assertion below to mean
    # anything: a *real* replay of the same outer refresh token would
    # re-present the same (by-then-already-consumed) PC ID refresh token,
    # which PC ID itself would reject.
    consumed_pc_refresh_tokens: set[str] = set()

    def _mock_raw_refresh_token(client, refresh_token):
        if refresh_token in consumed_pc_refresh_tokens:
            raise auth.PcidAuthError("PC ID rejected the refresh token (HTTP 400): already used.")
        consumed_pc_refresh_tokens.add(refresh_token)
        return {"access_token": "rotated-pcid-access-token", "refresh_token": "rotated-pcid-refresh-token", "expires_in": 3600}

    monkeypatch.setattr(auth, "raw_refresh_token", _mock_raw_refresh_token)

    # Refresh rotates both tokens.
    refresh_resp = await client.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token})
    assert refresh_resp.status_code == 200
    new_tokens = refresh_resp.json()
    assert new_tokens["access_token"] != access_token
    assert new_tokens["refresh_token"] != refresh_token
    assert token_crypto.decode(new_tokens["access_token"])["pc_access_token"] == "rotated-pcid-access-token"

    replay_refresh = await client.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
    assert replay_refresh.status_code == 400
    assert replay_refresh.json()["error"] == "invalid_grant"

    # The new access token also works, and still resolves to the same tenant.
    mcp_resp_2 = await client.get("/mcp", headers={"Authorization": f"Bearer {new_tokens['access_token']}"})
    assert mcp_resp_2.status_code == 200
    assert mcp_resp_2.text == f"inner-app-reached:{ALICE}"


async def test_refresh_grant_when_pcid_rejects_it_is_invalid_grant_not_a_crash(client, monkeypatch):
    """PC ID refresh tokens are single-use -- if the presented one was
    already consumed elsewhere (or genuinely expired/revoked), PC ID's own
    refresh call fails. That must surface as a clean invalid_grant, not an
    unhandled exception.
    """
    verifier, challenge = _pkce_pair()
    code = await _get_code(client, monkeypatch, challenge, client_id=ALICE)
    token_resp = await client.post(
        "/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    )
    refresh_token = token_resp.json()["refresh_token"]

    def _raise(client, refresh_token):
        raise auth.PcidAuthError("PC ID rejected the refresh token (HTTP 400).")

    monkeypatch.setattr(auth, "raw_refresh_token", _raise)
    resp = await client.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
