"""Delivery slots, booking and the checkout summary, against trimmed shapes
from a real web checkout session (one-checkout.<banner domain>)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402
from pc_express_mcp.api_client import PCExpressAPI  # noqa: E402

CART = {
    "id": "cart-1",
    "orders": [
        {
            "fulfillment": {
                "type": "courier",
                "courier": {"storeId": "1024", "fulfillmentLocationId": "1024PCXD", "deliveryAddress": {"postalCode": "L6H 0A0"}},
            },
            "entries": [],
        }
    ],
}


def _slot(date, start, end, available, charge=0, slot_type="DELIVERY_NEXT_DAY"):
    return {"date": date, "startTime": start, "endTime": end, "available": available, "charge": charge, "slotType": slot_type}


SLOTS = {
    "1024PCXD": {
        "timeslots": [
            _slot("2026-10-07", "19:30", "20:30", False, 3, "DELIVERY_IMMEDIATE"),
            _slot("2026-10-08", "08:30", "09:30", True),
            _slot("2026-10-08", "09:00", "10:00", True),
            _slot("2026-10-09", "08:30", "09:30", True),
            _slot("2026-10-10", "08:30", "09:30", True),
        ]
    }
}

BOOKED = {
    "cart": {
        "cart_data": {
            "store_carts": [
                {
                    "fulfillment": {
                        "delivery": {
                            "time_slot": {"start_time": "2026-10-08T12:30:00Z", "end_time": "2026-10-08T13:30:00Z", "expiry_time": "2026-10-07T21:32:38.047Z"}
                        }
                    }
                }
            ]
        }
    },
    "errors": [{"code": "UPSTREAM_ERROR", "error_detail": {"code": "LOW_STOCK", "message": "Product in cart is low stock."}}],
}


class _FakeApi:
    banner_info = {"domain": "www.realcanadiansuperstore.ca", "checkout_lob": "PCXSUPER"}

    def __init__(self):
        self.booked = []

    def get_delivery_slots(self, cart_id, location_id, postal_code):
        assert (location_id, postal_code) == ("1024PCXD", "L6H 0A0")
        return SLOTS

    def book_delivery_slot(self, cart_id, date, start, end, location_id, postal_code):
        self.booked.append((date, start, end))
        return BOOKED


def _patch(monkeypatch, api):
    session = session_state.SessionState(store_id="1024", cart_id="cart-1")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: api)
    monkeypatch.setattr(server, "_get_cart_healing", lambda a, s: CART)


def test_lists_only_available_slots_for_the_first_days(monkeypatch):
    _patch(monkeypatch, _FakeApi())
    result = server.get_available_slots(days=2)
    assert result["dates_available"] == ["2026-10-08", "2026-10-09", "2026-10-10"]
    assert list(result["slots"]) == ["2026-10-08", "2026-10-09"]
    assert result["slots"]["2026-10-08"][0] == {"start": "08:30", "end": "09:30", "fee": 0, "type": "NEXT_DAY"}


def test_lists_a_specific_date(monkeypatch):
    _patch(monkeypatch, _FakeApi())
    assert list(server.get_available_slots(date="2026-10-10")["slots"]) == ["2026-10-10"]


def test_books_an_available_slot_and_reports_the_hold(monkeypatch):
    api = _FakeApi()
    _patch(monkeypatch, api)
    result = server.book_delivery_slot("2026-10-08", "08:30")
    assert api.booked == [("2026-10-08", "08:30", "09:30")]
    assert result["booked"]["hold_expires_at"] == "2026-10-07T21:32:38.047Z"
    assert result["warnings"] == [{"code": "LOW_STOCK", "message": "Product in cart is low stock."}]


def test_refuses_unavailable_or_unknown_slots(monkeypatch):
    api = _FakeApi()
    _patch(monkeypatch, api)
    assert server.book_delivery_slot("2026-10-07", "19:30")["error"] == "slot_unavailable"
    assert server.book_delivery_slot("2026-10-08", "03:00")["error"] == "slot_not_found"
    assert api.booked == []


def test_booking_needs_a_verified_banner(monkeypatch):
    api = _FakeApi()
    api.banner_info = {"domain": "www.nofrills.ca"}
    _patch(monkeypatch, api)
    assert server.book_delivery_slot("2026-10-08", "08:30")["error"] == "booking_unsupported"


def test_cart_without_delivery_target_reports_slots_unavailable(monkeypatch):
    _patch(monkeypatch, _FakeApi())
    monkeypatch.setattr(server, "_get_cart_healing", lambda a, s: {"orders": [{"fulfillment": {"type": "pickupBooking"}}]})
    assert server.get_available_slots()["error"] == "slots_unavailable"


def test_booking_sends_local_wall_time_with_a_literal_z(monkeypatch):
    """The web client sends 08:30 local as "...T08:30:00.000Z" and the
    service books 08:30 local (confirmed live) -- don't convert to UTC."""
    api = PCExpressAPI(token_manager=None, banner="superstore")
    sent = {}

    class _Resp:
        def json(self):
            return {}

    def fake_request(method, url, **kw):
        sent.update(method=method, url=url, **kw)
        return _Resp()

    monkeypatch.setattr(api, "_request", fake_request)
    api.book_delivery_slot("cart-1", "2026-10-08", "08:30", "09:30", "1024PCXD", "L6H 0A0")
    assert sent["method"] == "PATCH"
    assert sent["url"] == "https://one-checkout.realcanadiansuperstore.ca/api/carts/cart-1/fulfillment"
    assert sent["json_body"]["deliveryTimeslot"]["startTime"] == "2026-10-08T08:30:00.000Z"
    assert sent["checkout"] is True


def test_checkout_auth_is_a_cookie_with_the_banner_lob():
    class _Tokens:
        def get_access_token(self, client):
            return "tok"

    headers = PCExpressAPI(token_manager=_Tokens(), banner="superstore")._checkout_auth_headers()
    assert headers["Cookie"] == "authToken=tok; lob=PCXSUPER"
    assert "Authorization" not in headers


def test_checkout_summary_is_in_dollars_without_personal_details():
    raw = {
        "checkout": {
            "checkout_data": {
                "charges": {
                    "subtotal": 6075,
                    "total_tax": 767,
                    "tip": 0,
                    "total": 6842,
                    "total_discount": 0,
                    "max_redeemable_points": 60000,
                    "fulfillment_fee_components": [
                        {"fee_component_type": "SERVICE_FEE", "calculated_amount_in_cents": 0},
                        {"fee_component_type": "DELIVERY_FEE", "calculated_amount_in_cents": 599},
                    ],
                },
                "fulfillment": {
                    "fulfillment_type": "DELIVERY",
                    "delivery_details": {"shipping_info": {"address_data": {"address_line_1": "1 Example St"}}},
                    "time_slot": {"start_time": "2026-10-08T12:30:00Z", "end_time": "2026-10-08T13:30:00Z", "expiry_time": "x"},
                },
                "customer_email": "someone@example.com",
            }
        }
    }
    result = server._simplify_checkout(raw)
    assert (result["subtotal"], result["tax"], result["delivery_fee"], result["total"]) == (60.75, 7.67, 5.99, 68.42)
    assert result["slot"]["start"] == "2026-10-08T12:30:00Z"
    assert "Example" not in str(result) and "@" not in str(result)
