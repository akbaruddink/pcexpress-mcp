"""Unit tests for banner-scoped cart discovery -- a real bug found live
while fulfilling a direct user request to add items to two different
banners' carts in the same session. `_rediscover_cart` used to read
`profile.cartId`, which turned out to return the *same* cart id
regardless of which banner's headers requested it (always the superstore
cart, confirmed live even under nofrills headers). `list_carts` correctly
returns each banner's own cart. Switching banners via `set_active_store`
also has to invalidate the cached cart_id, or the stale one survives the
switch and the next cart write silently targets the wrong banner's cart.

See docs/RESEARCH.md "Cart discovery must be banner-scoped".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402
from pc_express_mcp.api_client import PcxApiError  # noqa: E402


class _FakeApi:
    """profile.cartId deliberately returns the WRONG (superstore) cart id
    regardless of banner, matching the real bug -- list_carts is the one
    that must be trusted.
    """

    def __init__(self, banner_carts: dict[str, list[str]]):
        self._banner_carts = banner_carts
        self.banner = None

    def get_profile(self):
        return {"id": "customer-1", "customerId": "customer-1", "cartId": "WRONG-cart-from-profile"}

    def list_carts(self, customer_id):
        assert customer_id == "customer-1"
        ids = self._banner_carts.get(self.banner, [])
        return {"carts": [{"id": i} for i in ids]}


def _api_for_banner(banner_carts):
    api = _FakeApi(banner_carts)

    def get_for_banner(banner):
        api.banner = banner
        return api

    return get_for_banner


def test_rediscover_cart_uses_list_carts_not_profile_cartid():
    session = session_state.SessionState(banner="nofrills")
    get_api = _api_for_banner({"nofrills": ["nofrills-cart-real"], "superstore": ["superstore-cart-real"]})
    api = get_api("nofrills")
    server._rediscover_cart(api, session)
    assert session.cart_id == "nofrills-cart-real"
    assert session.cart_id != "WRONG-cart-from-profile"


def test_rediscover_cart_raises_clearly_when_banner_has_no_cart_yet():
    session = session_state.SessionState(banner="zehrs")
    get_api = _api_for_banner({})  # zehrs never used -- empty carts list
    api = get_api("zehrs")
    try:
        server._rediscover_cart(api, session)
        assert False, "expected PcxApiError"
    except PcxApiError as exc:
        assert "zehrs" in str(exc)


def test_set_active_store_clears_cart_id_when_banner_actually_changes(monkeypatch):
    session = session_state.SessionState(banner="superstore", cart_id="stale-superstore-cart")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_save_session", lambda s: None)
    monkeypatch.setattr(server, "_get_api", lambda banner: object())
    monkeypatch.setattr(server, "_simplify_store", lambda loc: {})

    class _StoreApi:
        def get_pickup_location(self, store_id):
            return {}

    monkeypatch.setattr(server, "_get_api", lambda banner: _StoreApi())
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: {"orders": []})

    server.set_active_store(store_id="1356", banner="nofrills")
    assert session.cart_id is None  # forced to re-discover fresh for the new banner


def test_set_active_store_keeps_cart_id_when_banner_is_unchanged(monkeypatch):
    session = session_state.SessionState(banner="superstore", cart_id="still-good-cart")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_save_session", lambda s: None)
    monkeypatch.setattr(server, "_simplify_store", lambda loc: {})

    class _StoreApi:
        def get_pickup_location(self, store_id):
            return {}

    monkeypatch.setattr(server, "_get_api", lambda banner: _StoreApi())
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: {"orders": []})

    server.set_active_store(store_id="1024", banner="superstore")
    assert session.cart_id == "still-good-cart"


def test_set_active_store_keeps_cart_id_when_banner_omitted(monkeypatch):
    session = session_state.SessionState(banner="superstore", cart_id="still-good-cart")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_save_session", lambda s: None)
    monkeypatch.setattr(server, "_simplify_store", lambda loc: {})

    class _StoreApi:
        def get_pickup_location(self, store_id):
            return {}

    monkeypatch.setattr(server, "_get_api", lambda banner: _StoreApi())
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: {"orders": []})

    server.set_active_store(store_id="1024")  # no banner= at all
    assert session.cart_id == "still-good-cart"
