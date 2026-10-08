"""Unit test for place_order's tightened checkout-handoff message -- after
a user reported having to leave Claude entirely to review their cart in
the PC Express app and then come back to keep discussing, place_order now
explicitly instructs showing a full visual receipt (photos, quantities,
prices, total) before the checkout link, so only the actual payment step
requires switching apps. See docs/RESEARCH.md "Product photos in chat".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402


class _FakeApi:
    def checkout_page_url(self):
        return "https://one-checkout.example/en/pre-checkout"

    def get_checkout(self, cart_id):
        return {"checkout": {"checkout_data": {"charges": {"subtotal": 1250, "total_tax": 0, "total": 1250}}}}


def test_place_order_message_instructs_full_visual_receipt_before_checkout_link(monkeypatch):
    session = session_state.SessionState(store_id="1024", cart_id="cart-1")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: _FakeApi())
    cart = {
        "orders": [
            {
                "totals": {"totalPrice": 12.5},
                "entries": [
                    {
                        "quantity": 1,
                        "offer": {"id": "AAA", "product": {"id": "AAA", "name": "Bread", "primaryImage": "https://x/bread.png"}},
                        "prices": {"totalSalePrice": 12.5},
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: cart)

    result = server.place_order(confirm=True)
    assert result["status"] == "ready_for_manual_checkout"
    assert "photo_markdown" in result["message"] or "visual receipt" in result["message"]
    assert result["cart_summary"]["items"][0]["photo_markdown"] == "![Bread](https://x/bread.png)"
    assert result["checkout_url"] == "https://one-checkout.example/en/pre-checkout"
    assert result["checkout"]["total"] == 12.5


def test_place_order_still_requires_confirm():
    result = server.place_order(confirm=False)
    assert result["error"] == "confirmation_required"


def test_place_order_still_rejects_empty_cart(monkeypatch):
    session = session_state.SessionState(store_id="1024", cart_id="cart-1")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: object())
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: {"orders": []})
    result = server.place_order(confirm=True)
    assert result["error"] == "empty_cart"


def test_place_order_still_hands_off_when_the_checkout_service_fails(monkeypatch):
    from pc_express_mcp.api_client import PcxApiError

    class _DownApi(_FakeApi):
        def get_checkout(self, cart_id):
            raise PcxApiError("GET ... -> ConnectError")

    session = session_state.SessionState(store_id="1024", cart_id="cart-1")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: _DownApi())
    cart = {"orders": [{"entries": [{"quantity": 1, "offer": {"id": "AAA_EA", "product": {"name": "Bread"}}, "prices": {"totalSalePrice": 3.0}}]}]}
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: cart)
    result = server.place_order(confirm=True)
    assert result["checkout_url"]
    assert result["cart_summary"]["item_count"] == 1
    assert result["checkout"]["error"] == "checkout_summary_unavailable"
