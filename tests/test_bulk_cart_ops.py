"""Unit tests for bulk add_to_cart/remove_from_cart/update_quantity --
converted from single-product-code args to lists after a real user report
that Claude was making one tool call per item, which is slow and wasteful
when the underlying PC Express API already accepts multiple product codes
per call (update_cart_entries's `entries` is a dict keyed by product
code -- this was always supported one level down, just never exposed at
the tool layer).

These mock session/API state rather than hitting the network -- see
test_cart_store_switch.py/test_cart_removal_fix.py for the established
pattern of monkeypatching server-module helpers instead of building a
full network mock for tools this deep in the call chain.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402


class _FakeApi:
    """Stand-in for PCExpressAPI -- server.add_to_cart/remove_from_cart/
    update_quantity only ever pass this through to _update_cart_healing
    and _seller_id_for_removal, both of which are monkeypatched below, so
    the fake itself never needs to do anything.
    """


def _session(store_id="1024"):
    return session_state.SessionState(store_id=store_id, cart_id="cart-1")


def _patch_get_api(monkeypatch, session):
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: _FakeApi())


def test_add_to_cart_sends_all_items_in_one_update_call(monkeypatch):
    session = _session()
    _patch_get_api(monkeypatch, session)
    captured = {}

    def fake_update_cart_healing(api, s, entries):
        captured["entries"] = entries
        return {"orders": []}

    monkeypatch.setattr(server, "_update_cart_healing", fake_update_cart_healing)

    result = server.add_to_cart(
        items=[
            {"product_code": "AAA", "quantity": 2, "fulfillment_method": "delivery"},
            {"product_code": "BBB"},  # defaults: quantity=1, fulfillment_method=pickup
        ]
    )
    assert captured["entries"] == {
        "AAA": {"quantity": 2, "fulfillmentMethod": "delivery", "sellerId": "1024"},
        "BBB": {"quantity": 1, "fulfillmentMethod": "pickup", "sellerId": "1024"},
    }
    assert result["items_requested"] == 2


def test_add_to_cart_rejects_empty_list():
    result = server.add_to_cart(items=[])
    assert result["error"] == "invalid_items"


def test_add_to_cart_rejects_missing_product_code(monkeypatch):
    _patch_get_api(monkeypatch, _session())
    result = server.add_to_cart(items=[{"quantity": 1}])
    assert result["error"] == "invalid_items"


def test_add_to_cart_rejects_invalid_quantity_before_any_api_call(monkeypatch):
    session = _session()
    _patch_get_api(monkeypatch, session)

    def fail_if_called(*a, **kw):
        raise AssertionError("should not reach the API for an invalid batch")

    monkeypatch.setattr(server, "_update_cart_healing", fail_if_called)
    result = server.add_to_cart(items=[{"product_code": "AAA", "quantity": 1}, {"product_code": "BBB", "quantity": 0}])
    assert result["error"] == "invalid_quantity"
    assert "BBB" in result["message"]


def test_add_to_cart_requires_active_store(monkeypatch):
    session = _session(store_id=None)
    monkeypatch.setattr(server, "_load_session", lambda: session)
    result = server.add_to_cart(items=[{"product_code": "AAA"}])
    assert result["error"] == "no_active_store"


def test_remove_from_cart_sends_all_codes_with_one_seller_id_lookup(monkeypatch):
    session = _session()
    _patch_get_api(monkeypatch, session)
    lookups = []

    def fake_seller_id_for_removal(api, s):
        lookups.append(1)
        return "1024"

    captured = {}

    def fake_update_cart_healing(api, s, entries):
        captured["entries"] = entries
        return {"orders": []}

    monkeypatch.setattr(server, "_seller_id_for_removal", fake_seller_id_for_removal)
    monkeypatch.setattr(server, "_update_cart_healing", fake_update_cart_healing)

    result = server.remove_from_cart(product_codes=["AAA", "BBB", "CCC"])
    assert len(lookups) == 1  # not once per product code
    assert captured["entries"] == {
        "AAA": {"quantity": 0, "fulfillmentMethod": "pickup", "sellerId": "1024"},
        "BBB": {"quantity": 0, "fulfillmentMethod": "pickup", "sellerId": "1024"},
        "CCC": {"quantity": 0, "fulfillmentMethod": "pickup", "sellerId": "1024"},
    }
    assert result["items_requested"] == 3


def test_remove_from_cart_rejects_empty_list():
    result = server.remove_from_cart(product_codes=[])
    assert result["error"] == "invalid_items"


def test_update_quantity_mixes_adds_and_removals_in_one_call(monkeypatch):
    session = _session()
    _patch_get_api(monkeypatch, session)
    lookups = []
    monkeypatch.setattr(server, "_seller_id_for_removal", lambda api, s: lookups.append(1) or "1024")
    captured = {}
    monkeypatch.setattr(
        server, "_update_cart_healing", lambda api, s, entries: captured.setdefault("entries", entries) or {"orders": []}
    )

    result = server.update_quantity(
        items=[
            {"product_code": "AAA", "quantity": 3},
            {"product_code": "BBB", "quantity": 0},
        ]
    )
    assert captured["entries"] == {
        "AAA": {"quantity": 3, "fulfillmentMethod": "pickup", "sellerId": "1024"},
        "BBB": {"quantity": 0, "fulfillmentMethod": "pickup", "sellerId": "1024"},
    }
    assert len(lookups) == 1
    assert result["items_requested"] == 2


def test_update_quantity_all_removals_does_not_require_active_store(monkeypatch):
    session = _session(store_id=None)
    _patch_get_api(monkeypatch, session)
    monkeypatch.setattr(server, "_seller_id_for_removal", lambda api, s: None)
    monkeypatch.setattr(server, "_update_cart_healing", lambda api, s, entries: {"orders": []})
    result = server.update_quantity(items=[{"product_code": "AAA", "quantity": 0}])
    assert result.get("error") is None


def test_update_quantity_rejects_negative_quantity():
    result = server.update_quantity(items=[{"product_code": "AAA", "quantity": -1}])
    assert result["error"] == "invalid_quantity"


def test_update_quantity_rejects_empty_list():
    result = server.update_quantity(items=[])
    assert result["error"] == "invalid_items"


def test_cart_item_schema_types_quantity_as_integer():
    """Typed items let the SDK coerce/reject a non-numeric quantity before
    the tool runs, instead of a raw TypeError on `quantity <= 0`."""
    from pc_express_mcp import server

    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in ("add_to_cart", "update_quantity"):
        schema = tools[name].parameters
        item = schema["$defs"][schema["properties"]["items"]["items"]["$ref"].split("/")[-1]]
        assert item["properties"]["quantity"]["type"] == "integer"
        assert "product_code" in item["required"]
