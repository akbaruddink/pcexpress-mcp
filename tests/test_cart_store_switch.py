"""Unit tests for switch_cart_store -- the real, confirmed-live fix for
SELLER_ID_MISMATCH (see docs/RESEARCH.md "Cart is bound to a single
store"), found from a real user-supplied curl capture and verified live
against a real account (dry-run previews without persisting; the real
call switches the cart's bound store both ways; a fresh add_to_cart works
cleanly afterwards; the account's cart was restored to its original state
afterwards). These tests cover the pure logic and request-shape pieces
without hitting the network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import config  # noqa: E402
from pc_express_mcp.api_client import PCExpressAPI  # noqa: E402
from pc_express_mcp.server import _cart_bound_store, _find_fulfillment_location_id  # noqa: E402

# Trimmed, real shape from a user-supplied capture of
# POST https://api.pcexpress.ca/v1/delivery/serviceability (unauthenticated).
REAL_SERVICEABILITY = {
    "banners": [
        {
            "name": "superstore",
            "pickupLocations": [
                {"id": "1024PCXD", "banner": "superstore"},
                {"id": "2841PCXD", "banner": "superstore"},
            ],
        },
        {
            "name": "nofrills",
            "pickupLocations": [
                {"id": "3150PCXPD", "banner": "nofrills"},
            ],
        },
    ]
}


def test_find_fulfillment_location_id_matches_store_and_banner():
    assert _find_fulfillment_location_id(REAL_SERVICEABILITY, "superstore", "1024") == "1024PCXD"
    assert _find_fulfillment_location_id(REAL_SERVICEABILITY, "superstore", "2841") == "2841PCXD"


def test_find_fulfillment_location_id_does_not_cross_banners():
    # "3150" only exists under nofrills -- must not match if the active banner is superstore.
    assert _find_fulfillment_location_id(REAL_SERVICEABILITY, "superstore", "3150") is None


def test_find_fulfillment_location_id_none_when_store_not_serviceable():
    assert _find_fulfillment_location_id(REAL_SERVICEABILITY, "superstore", "9999") is None


def test_find_fulfillment_location_id_handles_empty_response():
    assert _find_fulfillment_location_id({}, "superstore", "1024") is None


def test_cart_bound_store_reads_real_courier_shape():
    # Real shape confirmed live: orders[0].fulfillment.courier.storeId.
    cart = {"orders": [{"fulfillment": {"courier": {"storeId": "1024"}}}]}
    assert _cart_bound_store(cart) == "1024"


def test_cart_bound_store_none_when_no_orders():
    assert _cart_bound_store({"orders": []}) is None
    assert _cart_bound_store({}) is None


def test_cart_bound_store_none_when_no_courier_fulfillment_yet():
    cart = {"orders": [{"fulfillment": {}}]}
    assert _cart_bound_store(cart) is None


def _api_with_captured_request(monkeypatch):
    api = PCExpressAPI(token_manager=None, banner="superstore")
    captured = {}

    class _FakeResp:
        def json(self):
            return {"ok": True}

    def fake_request(method, url, *, json_body=None, params=None, retried=False):
        captured["method"] = method
        captured["url"] = url
        captured["json_body"] = json_body
        captured["params"] = params
        return _FakeResp()

    monkeypatch.setattr(api, "_request", fake_request)
    return api, captured


def test_get_delivery_serviceability_sends_real_confirmed_shape(monkeypatch):
    api, captured = _api_with_captured_request(monkeypatch)
    api.get_delivery_serviceability("A1A 1A1")
    assert captured["method"] == "POST"
    assert captured["url"] == config.PCX_DELIVERY_SERVICEABILITY_URL
    assert captured["json_body"] == {"deliveryAddress": {"postalCode": "A1A 1A1"}, "isB2b": False}


def test_set_cart_fulfillment_sends_real_confirmed_shape(monkeypatch):
    api, captured = _api_with_captured_request(monkeypatch)
    api.set_cart_fulfillment("cart-123", "1024PCXD", "A1A 1A1")
    assert captured["method"] == "POST"
    assert captured["url"] == f"{config.PCX_BFF_BASE}/carts/cart-123"
    assert captured["json_body"] == {
        "courier": {"deliveryAddress": {"postalCode": "A1A 1A1"}, "fulfillmentLocationId": "1024PCXD"},
        "fulfillmentType": "COURIER",
    }


def test_set_cart_fulfillment_dry_run_hits_dry_run_suffix(monkeypatch):
    api, captured = _api_with_captured_request(monkeypatch)
    api.set_cart_fulfillment("cart-123", "1024PCXD", "A1A 1A1", dry_run=True)
    assert captured["url"] == f"{config.PCX_BFF_BASE}/carts/cart-123/dry-run"
