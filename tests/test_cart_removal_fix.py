"""Unit tests for two real bugs found live (both reported directly by a
user, both reproduced against a real account before being fixed):

1. remove_from_cart/update_quantity(0) sent a bare {"quantity": 0}, which
   PC Express's API rejects with SELLER_ID_MISMATCH ("provided": null) --
   a real bug in this project, not a platform quirk. Fixed by always
   including sellerId/fulfillmentMethod, sourced from the cart's own real
   binding rather than the possibly-stale locally cached session.store_id.
2. get_time_slots could return HTTP 200 with an HTML "Site Under
   Maintenance" page instead of JSON, crashing with a raw
   json.JSONDecodeError instead of a normal tool error.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402
from pc_express_mcp.api_client import PCExpressAPI, PcxApiError  # noqa: E402
from pc_express_mcp.auth import PcidAuthError  # noqa: E402


def test_seller_id_for_removal_prefers_cart_real_binding_over_session(monkeypatch):
    session = session_state.SessionState(store_id="2841", cart_id="cart-1")  # stale/mismatched
    cart = {"orders": [{"fulfillment": {"courier": {"storeId": "1024"}}}]}
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: cart)
    assert server._seller_id_for_removal(api=None, session=session) == "1024"


def test_seller_id_for_removal_falls_back_to_session_when_cart_lookup_fails(monkeypatch):
    session = session_state.SessionState(store_id="1024", cart_id="cart-1")

    def raise_it(api, s):
        raise PcxApiError("boom", status_code=500, body="")

    monkeypatch.setattr(server, "_get_cart_healing", raise_it)
    assert server._seller_id_for_removal(api=None, session=session) == "1024"


def test_seller_id_for_removal_falls_back_on_auth_error(monkeypatch):
    session = session_state.SessionState(store_id="1024", cart_id="cart-1")

    def raise_it(api, s):
        raise PcidAuthError("expired")

    monkeypatch.setattr(server, "_get_cart_healing", raise_it)
    assert server._seller_id_for_removal(api=None, session=session) == "1024"


def test_seller_id_for_removal_none_when_nothing_available(monkeypatch):
    session = session_state.SessionState(store_id=None, cart_id="cart-1")
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: {"orders": []})
    assert server._seller_id_for_removal(api=None, session=session) is None


def test_get_time_slots_raises_clean_error_on_non_json_response(monkeypatch):
    api = PCExpressAPI(token_manager=None, banner="superstore")

    class _HtmlResp:
        status_code = 200
        text = "<!DOCTYPE html><title>Site Under Maintenance</title>"

        def json(self):
            raise json.JSONDecodeError("Expecting value", self.text, 0)

    monkeypatch.setattr(api, "_request", lambda method, url, **kw: _HtmlResp())

    with pytest.raises(PcxApiError) as exc_info:
        api.get_time_slots("1024")
    assert exc_info.value.status_code == 200
    assert "non-JSON" in str(exc_info.value)


def test_get_time_slots_still_returns_real_json_normally(monkeypatch):
    api = PCExpressAPI(token_manager=None, banner="superstore")

    class _JsonResp:
        status_code = 200
        text = '{"timeSlots": []}'

        def json(self):
            return {"timeSlots": []}

    monkeypatch.setattr(api, "_request", lambda method, url, **kw: _JsonResp())
    assert api.get_time_slots("1024") == {"timeSlots": []}
