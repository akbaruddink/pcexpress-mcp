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
    assert (result["subtotal"], result["tax"], result["total"]) == (60.75, 7.67, 68.42)
    assert result["fees"] == {"service_fee": 0.0, "delivery_fee": 5.99}
    assert "slot" not in result  # UTC there; cart_summary.slot has it in store time
    assert "Example" not in str(result) and "@" not in str(result)


# --- cases reproduced in review, and real-fixture checks ------------------

import json  # noqa: E402

import pytest  # noqa: E402

from pc_express_mcp.api_client import PcxApiError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "raw_responses"


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_real_cart_yields_its_delivery_target():
    assert server._cart_delivery_target(_load("get_cart")) == ("1024PCXD", "REDACTED")


def test_pickup_cart_with_a_stale_courier_block_is_not_a_delivery_target():
    cart = _load("get_cart")
    cart["orders"][0]["fulfillment"]["type"] = "pickupBooking"
    assert server._cart_delivery_target(cart) == (None, None)


def test_real_slot_list_parses(monkeypatch):
    api = _FakeApi()
    api.get_delivery_slots = lambda *a: _load("get_delivery_slots")
    _patch(monkeypatch, api)
    monkeypatch.setattr(server, "_get_cart_healing", lambda a, s: _load("get_cart"))
    result = server.get_available_slots(days=1)
    assert result["location_id"] == "1024PCXD"
    first_date = result["dates_available"][0]
    assert {"start", "end", "fee", "type"} <= set(result["slots"][first_date][0])


def test_expired_hold_is_flagged_on_the_real_cart():
    """The real cart still lists yesterday's hold after it expired."""
    slot = server._simplify_cart(_load("get_cart"))["slot"]
    assert slot["start"] and slot["hold_active"] is False


def test_real_booking_response_parses(monkeypatch):
    api = _FakeApi()
    api.book_delivery_slot = lambda *a: _load("book_delivery_slot")
    _patch(monkeypatch, api)
    result = server.book_delivery_slot("2026-10-08", "08:30")
    assert result["booked"]["hold_expires_at"] == "2026-10-07T21:32:38.047Z"
    assert result["warnings"][0]["code"] == "LOW_STOCK"


def test_booking_with_no_hold_in_the_response_is_not_reported_as_booked(monkeypatch):
    api = _FakeApi()
    api.book_delivery_slot = lambda *a: {"errors": [{"code": "UPSTREAM_ERROR", "error_detail": {"code": "SLOT_FULL"}}]}
    _patch(monkeypatch, api)
    result = server.book_delivery_slot("2026-10-08", "08:30")
    assert result["error"] == "not_booked"
    assert result["warnings"][0]["code"] == "SLOT_FULL"


def test_an_unavailable_slot_does_not_shadow_an_available_one_at_the_same_time(monkeypatch):
    api = _FakeApi()
    api.get_delivery_slots = lambda *a: {
        "1024PCXD": {"timeslots": [_slot("2026-10-09", "08:30", "09:00", False, 3, "DELIVERY_IMMEDIATE"), _slot("2026-10-09", "08:30", "09:30", True)]}
    }
    _patch(monkeypatch, api)
    assert "booked" in server.book_delivery_slot("2026-10-09", "08:30")
    assert api.booked == [("2026-10-09", "08:30", "09:30")]


def test_days_below_one_still_returns_one_day(monkeypatch):
    _patch(monkeypatch, _FakeApi())
    assert len(server.get_available_slots(days=-1)["slots"]) == 1


def test_real_checkout_summary_parses_without_personal_data():
    raw = _load("get_checkout")
    result = server._simplify_checkout(raw)
    charges = raw["checkout"]["checkout_data"]["charges"]
    assert result["total"] == charges["total"] / 100
    assert result["fulfillment_type"] == "DELIVERY"
    assert round(result["subtotal"] + sum(result["fees"].values()) + result["tip"] + result["tax"] - result["discount"], 2) == result["total"]


def test_checkout_service_non_json_and_network_errors_become_api_errors(monkeypatch):
    import httpx

    api = PCExpressAPI(token_manager=type("T", (), {"get_access_token": lambda self, c: "tok"})(), banner="superstore")
    html = httpx.Response(200, text="<!DOCTYPE html><title>Site Under Maintenance</title>")
    monkeypatch.setattr(api._client, "request", lambda *a, **kw: html)
    with pytest.raises(PcxApiError, match="non-JSON"):
        api.book_delivery_slot("cart-1", "2026-10-08", "08:30", "09:30", "1024PCXD", "L6H 0A0")

    def boom(*a, **kw):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(api._client, "request", boom)
    with pytest.raises(PcxApiError, match="ConnectError"):
        api.get_checkout("cart-1")


def test_checkout_service_401_does_not_burn_the_refresh_token(monkeypatch):
    import httpx

    class _Tokens:
        refreshed = 0

        def get_access_token(self, client):
            return "tok"

        def force_refresh(self, client):
            self.refreshed += 1

    tokens = _Tokens()
    api = PCExpressAPI(token_manager=tokens, banner="nofrills")
    monkeypatch.setattr(api._client, "request", lambda *a, **kw: httpx.Response(401, json={}))
    with pytest.raises(PcxApiError):
        api.get_checkout("cart-1")
    assert tokens.refreshed == 0


def test_banner_without_a_checkout_service_degrades_cleanly():
    api = PCExpressAPI(token_manager=None, banner="tandt")
    assert api.checkout_page_url() == "https://www.tntsupermarket.com/"
    with pytest.raises(PcxApiError, match="No checkout service"):
        api.get_checkout("cart-1")
