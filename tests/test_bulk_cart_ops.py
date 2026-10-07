"""Unit tests for add_to_cart/remove_from_cart/update_quantity, against a
fake cart that behaves like PC Express does live: one update call takes
many codes, and codes it can't use (bare, wrong suffix, unknown) are
silently ignored while a normal cart comes back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402


class _FakeCart:
    def __init__(self, quantities=None, store="1024", mode="courier", catalog=("AAA_EA", "BBB_EA", "LEMON_EA", "TOM_KG")):
        self.quantities = dict(quantities or {})
        self.store = store
        self.mode = mode
        self.catalog = set(catalog)
        self.sent = []

    def raw(self):
        entries = [{"quantity": q, "offer": {"id": c, "product": {"name": c}}, "prices": {}} for c, q in self.quantities.items()]
        return {
            "id": "cart-1",
            "status": "OPEN",
            "orders": [{"fulfillment": {"type": self.mode, self.mode: {"storeId": self.store}}, "totals": {}, "entries": entries}],
        }

    def update(self, entries):
        self.sent.append(entries)
        for code, e in entries.items():
            if code not in self.catalog:
                continue
            if e["quantity"] == 0:
                self.quantities.pop(code, None)
            else:
                self.quantities[code] = e["quantity"]
        return self.raw()

    def lookup(self, api, store_id, codes):
        found = [{"code": c} for c in sorted(self.catalog) if c.split("_")[0] in codes]
        return found, [c for c in codes if not any(f["code"].split("_")[0] == c for f in found)]


def _patch(monkeypatch, cart, store_id="1024"):
    session = session_state.SessionState(store_id=store_id, cart_id="cart-1")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_save_session", lambda s: None)
    monkeypatch.setattr(server, "_get_api", lambda banner: object())
    monkeypatch.setattr(server, "_get_cart_healing", lambda api, s: cart.raw())
    monkeypatch.setattr(server, "_update_cart_healing", lambda api, s, entries: cart.update(entries))
    monkeypatch.setattr(server, "_lookup_products_by_code", cart.lookup)
    return session


def test_add_sends_all_items_in_one_call_defaulting_to_the_carts_mode(monkeypatch):
    cart = _FakeCart()
    _patch(monkeypatch, cart)
    result = server.add_to_cart(
        items=[{"product_code": "AAA_EA", "quantity": 2}, {"product_code": "BBB_EA", "fulfillment_method": "pickup"}]
    )
    assert cart.sent == [
        {
            "AAA_EA": {"quantity": 2, "fulfillmentMethod": "delivery", "sellerId": "1024"},
            "BBB_EA": {"quantity": 1, "fulfillmentMethod": "pickup", "sellerId": "1024"},
        }
    ]
    assert result["applied"] == ["AAA_EA", "BBB_EA"]
    assert result["rejected"] == []
    assert result["fulfillment_method"] == "delivery"
    assert result["store_id"] == "1024"


def test_add_increments_existing_quantity(monkeypatch):
    cart = _FakeCart({"LEMON_EA": 1.0})
    _patch(monkeypatch, cart)
    server.add_to_cart(items=[{"product_code": "LEMON_EA"}])
    assert cart.quantities["LEMON_EA"] == 2


def test_add_resolves_bare_codes_from_history(monkeypatch):
    cart = _FakeCart()
    _patch(monkeypatch, cart)
    result = server.add_to_cart(items=[{"product_code": "LEMON"}, {"product_code": "TOM"}])
    assert result["applied"] == ["LEMON_EA", "TOM_KG"]


def test_add_reports_ignored_codes_and_errors_when_nothing_applied(monkeypatch):
    cart = _FakeCart()
    _patch(monkeypatch, cart)
    result = server.add_to_cart(items=[{"product_code": "TOM_EA"}, {"product_code": "NOPE"}])
    assert result["error"] == "nothing_applied"
    assert {r["code"]: r["reason"] for r in result["rejected"]} == {"NOPE": "unknown_code", "TOM_EA": "not_applied"}
    assert next(r for r in result["rejected"] if r["code"] == "TOM_EA")["suggested_code"] == "TOM_KG"


def test_add_partial_success_is_not_an_error(monkeypatch):
    cart = _FakeCart()
    _patch(monkeypatch, cart)
    result = server.add_to_cart(items=[{"product_code": "AAA_EA"}, {"product_code": "NOPE_EA"}])
    assert "error" not in result
    assert result["applied"] == ["AAA_EA"]
    assert result["rejected"][0]["code"] == "NOPE_EA"


def test_add_rejects_bad_input_before_any_api_call(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("should not reach the API")

    monkeypatch.setattr(server, "_load_session", fail)
    assert server.add_to_cart(items=[])["error"] == "invalid_items"
    assert server.add_to_cart(items=[{"quantity": 1}])["error"] == "invalid_items"
    result = server.add_to_cart(items=[{"product_code": "AAA_EA"}, {"product_code": "BBB_EA", "quantity": 0}])
    assert result["error"] == "invalid_quantity"
    assert "BBB_EA" in result["message"]


def test_add_without_active_store_uses_the_carts_store(monkeypatch):
    """HTTP mode loses the active store on restart; the cart's binding survives."""
    cart = _FakeCart(store="2841")
    session = _patch(monkeypatch, cart, store_id=None)
    result = server.add_to_cart(items=[{"product_code": "AAA_EA"}])
    assert result["applied"] == ["AAA_EA"]
    assert cart.sent[0]["AAA_EA"]["sellerId"] == "2841"
    assert session.store_id == "2841"


def test_no_cart_is_a_state_for_get_cart_and_an_error_for_writes(monkeypatch):
    session = session_state.SessionState(store_id="1024")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: object())

    def no_cart(api, s):
        raise server.NoCartError("No open cart")

    monkeypatch.setattr(server, "_get_cart_healing", no_cart)
    assert server.get_cart()["status"] == "NO_CART"
    assert server.add_to_cart(items=[{"product_code": "AAA_EA"}])["error"] == "no_cart"


def test_remove_uses_the_carts_real_store_as_seller_id(monkeypatch):
    """Confirmed live: a removal without the cart's real binding as sellerId
    fails with SELLER_ID_MISMATCH, even when the session's store is stale."""
    cart = _FakeCart({"AAA_EA": 1, "BBB_EA": 2}, store="1024")
    _patch(monkeypatch, cart, store_id="2841")
    result = server.remove_from_cart(product_codes=["AAA_EA", "BBB"])
    assert cart.sent == [
        {
            "AAA_EA": {"quantity": 0, "fulfillmentMethod": "delivery", "sellerId": "1024"},
            "BBB_EA": {"quantity": 0, "fulfillmentMethod": "delivery", "sellerId": "1024"},
        }
    ]
    assert result["applied"] == ["AAA_EA", "BBB_EA"]
    assert result["item_count"] == 0


def test_remove_reports_codes_not_in_cart_without_writing(monkeypatch):
    cart = _FakeCart({"AAA_EA": 1})
    _patch(monkeypatch, cart)
    result = server.remove_from_cart(product_codes=["99999999_EA"])
    assert result["error"] == "nothing_applied"
    assert result["rejected"] == [{"code": "99999999_EA", "reason": "not_in_cart"}]
    assert cart.sent == []


def test_remove_rejects_empty_list():
    assert server.remove_from_cart(product_codes=[])["error"] == "invalid_items"


def test_update_quantity_sets_exact_quantities_and_removes_in_one_call(monkeypatch):
    cart = _FakeCart({"AAA_EA": 5, "BBB_EA": 1})
    _patch(monkeypatch, cart)
    result = server.update_quantity(items=[{"product_code": "AAA_EA", "quantity": 3}, {"product_code": "BBB_EA", "quantity": 0}])
    assert len(cart.sent) == 1
    assert cart.quantities == {"AAA_EA": 3}
    assert result["applied"] == ["AAA_EA", "BBB_EA"]


def test_update_quantity_all_removals_does_not_need_an_active_store(monkeypatch):
    cart = _FakeCart({"AAA_EA": 1})
    _patch(monkeypatch, cart, store_id=None)
    result = server.update_quantity(items=[{"product_code": "AAA_EA", "quantity": 0}])
    assert result.get("error") is None


def test_update_quantity_rejects_bad_input():
    assert server.update_quantity(items=[])["error"] == "invalid_items"
    assert server.update_quantity(items=[{"product_code": "AAA_EA", "quantity": -1}])["error"] == "invalid_quantity"


def test_cart_item_schema_types_quantity_as_integer():
    """Typed items let the SDK coerce/reject a non-numeric quantity before
    the tool runs, instead of a raw TypeError on `quantity <= 0`."""
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in ("add_to_cart", "update_quantity"):
        schema = tools[name].parameters
        item = schema["$defs"][schema["properties"]["items"]["items"]["$ref"].split("/")[-1]]
        assert item["properties"]["quantity"]["type"] == "integer"
        assert "product_code" in item["required"]
