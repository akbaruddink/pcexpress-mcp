"""Unit tests for get_purchase_history -- added after a real incident: an
agent filled a cart with items (ground chicken, chips) nobody wanted, and
a family member ordered them assuming they were deliberate picks. This
tool lets an agent cross-check candidate items against what the
household actually buys, scoped to the active store (resolved from each
order's real store id, not the store name alone -- confirmed live that a
real account accumulates legacy name variants for the same store).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402
from pc_express_mcp.api_client import PcxApiError  # noqa: E402


class _FakeApi:
    def __init__(self, summaries, details):
        self._summaries = summaries
        self._details = details
        self.detail_calls = []

    def get_historical_orders(self):
        return {"orderHistory": self._summaries}

    def get_historical_order(self, order_id):
        self.detail_calls.append(order_id)
        return self._details[order_id]


def _order(order_id, placed, store_id, store_name, entries):
    return (
        {"id": order_id, "placed": placed, "store": store_name},
        {
            "orderDetails": {
                "booking": {"pickupLocation": {"storeId": store_id, "name": store_name}},
                "entries": entries,
            }
        },
    )


def _entry(code, name, brand, quantity):
    return {"product": {"articleNumber": code, "productName": name, "brand": brand}, "quantity": quantity}


def _patch(monkeypatch, api, store_id="1024"):
    session = session_state.SessionState(store_id=store_id, banner="superstore")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: api)


def test_aggregates_items_scoped_to_active_store_only(monkeypatch):
    s1, d1 = _order("A", "2026-08-01", "1024", "My Store", [_entry("MILK", "Milk", "Neilson", 1)])
    s2, d2 = _order("B", "2026-08-05", "2841", "Other Store", [_entry("CHIPS", "Chips", "Lays", 2)])
    s3, d3 = _order("C", "2026-08-10", "1024", "My Store", [_entry("MILK", "Milk", "Neilson", 1)])
    api = _FakeApi([s2, s3, s1], {"A": d1, "B": d2, "C": d3})  # out-of-order on purpose
    _patch(monkeypatch, api)

    result = server.get_purchase_history()
    assert result["orders_matched"] == 2  # A and C, not B (different store)
    assert result["distinct_items"] == 1
    item = result["items"][0]
    assert item["code"] == "MILK"
    assert item["times_purchased"] == 2
    assert item["total_quantity"] == 2
    # Newest-first scan means C (2026-08-10) is seen before A -> last_purchased is C's date.
    assert item["last_purchased"] == "2026-08-10"


def test_most_frequently_bought_sorts_first(monkeypatch):
    s1, d1 = _order("A", "2026-08-01", "1024", "My Store", [_entry("MILK", "Milk", "Neilson", 1), _entry("EGGS", "Eggs", "No Name", 1)])
    s2, d2 = _order("B", "2026-08-05", "1024", "My Store", [_entry("MILK", "Milk", "Neilson", 1)])
    api = _FakeApi([s2, s1], {"A": d1, "B": d2})
    _patch(monkeypatch, api)

    result = server.get_purchase_history()
    assert [i["code"] for i in result["items"]] == ["MILK", "EGGS"]


def test_stops_once_limit_matching_orders_found(monkeypatch):
    orders = [_order(f"O{i}", f"2026-08-{i:02d}", "1024", "My Store", [_entry(f"ITEM{i}", f"Item {i}", "Brand", 1)]) for i in range(1, 6)]
    summaries = [o[0] for o in orders][::-1]  # newest first
    details = {o[0]["id"]: o[1] for o in orders}
    api = _FakeApi(summaries, details)
    _patch(monkeypatch, api)

    result = server.get_purchase_history(limit=2)
    assert result["orders_matched"] == 2
    assert len(api.detail_calls) == 2  # didn't fetch detail for the other 3


def test_stops_at_max_orders_scanned_even_if_limit_not_reached(monkeypatch):
    # All orders are from a different store -- matched_orders never reaches
    # limit, so max_orders_scanned is what actually bounds the work.
    orders = [_order(f"O{i}", f"2026-08-{i:02d}", "2841", "Other Store", [_entry(f"ITEM{i}", f"Item {i}", "Brand", 1)]) for i in range(1, 6)]
    summaries = [o[0] for o in orders][::-1]
    details = {o[0]["id"]: o[1] for o in orders}
    api = _FakeApi(summaries, details)
    _patch(monkeypatch, api)

    result = server.get_purchase_history(limit=10, max_orders_scanned=3)
    assert result["orders_matched"] == 0
    assert result["orders_scanned"] == 3
    assert len(api.detail_calls) == 3


def test_requires_active_store(monkeypatch):
    _patch(monkeypatch, _FakeApi([], {}), store_id=None)
    result = server.get_purchase_history()
    assert result["error"] == "no_active_store"


def test_skips_orders_whose_detail_fetch_fails(monkeypatch):
    s1, d1 = _order("A", "2026-08-01", "1024", "My Store", [_entry("MILK", "Milk", "Neilson", 1)])

    class _FlakyApi(_FakeApi):
        def get_historical_order(self, order_id):
            if order_id == "BAD":
                raise PcxApiError("boom", status_code=500, body="")
            return super().get_historical_order(order_id)

    bad_summary = {"id": "BAD", "placed": "2026-08-10", "store": "My Store"}
    api = _FlakyApi([bad_summary, s1], {"A": d1})
    _patch(monkeypatch, api)

    result = server.get_purchase_history()
    assert result["orders_matched"] == 1
    assert result["items"][0]["code"] == "MILK"
