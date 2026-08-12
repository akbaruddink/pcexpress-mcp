"""Regression guard against real (anonymized) captured API responses.

tests/fixtures/raw_responses/*.json are real responses from this project's
own account, captured via scripts/capture_snapshots.py and manually
reviewed for leftover PII before being committed (see that script's
docstring -- a first redaction pass genuinely missed a customer's name and
email, caught only by manual review, which is why "manually reviewed" is
not a formality here).

This exists specifically because `_simplify_cart` shipped for a long time
against an *assumed* shape that was never checked against a real,
non-empty cart, and broke silently (every item field came back null, no
exception) the first time a real user's cart had real items in it. A
unit test with a hand-written fixture can only catch "does this match what
I assumed the shape to be" -- it can't catch "the assumption itself was
wrong," because the fixture and the code were written by the same
assumption. These tests run the simplifiers against real captured
responses instead, so a *structural* regression (a field silently starting
to always come back null against real data) has a chance of being caught
here rather than by the next real user filing a bug.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp.server import (  # noqa: E402
    _simplify_cart,
    _simplify_loyalty,
    _simplify_nutrition,
    _simplify_order_detail,
    _simplify_order_summary,
    _simplify_product,
    _simplify_store,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "raw_responses"


def _load(name: str) -> dict:
    with open(FIXTURES / f"{name}.json") as f:
        return json.load(f)


def test_snapshot_files_present():
    # If this fails, the snapshots directory is missing or was cleared --
    # re-run scripts/capture_snapshots.py (against your own account) and
    # manually review the output before committing.
    for name in (
        "get_cart",
        "get_pickup_location",
        "get_historical_orders",
        "get_historical_order",
        "search_products",
        "get_profile",
        "get_customer_promotions",
        "openfoodfacts_product",
    ):
        assert (FIXTURES / f"{name}.json").exists(), f"missing snapshot: {name}.json"


def test_simplify_cart_against_real_snapshot():
    cart = _load("get_cart")
    result = _simplify_cart(cart)
    assert result["item_count"] > 0
    assert len(result["items"]) == result["item_count"]
    for item in result["items"]:
        # This exact assertion -- code/name/total_price non-null -- is what
        # would have caught the real all-nulls bug this file exists to
        # prevent from recurring silently.
        assert item["code"] is not None
        assert item["name"] is not None
        assert item["total_price"] is not None
        assert item["quantity"] is not None


def test_simplify_product_against_real_snapshot():
    search = _load("search_products")
    raw_results = search.get("results") or []
    assert raw_results, "snapshot has no results -- recapture with a query known to return hits"
    for raw in raw_results:
        product = _simplify_product(raw)
        assert product["code"] is not None
        assert product["name"] is not None
        # price is allowed to be legitimately absent for an out-of-stock
        # item, but every in-stock result in the real snapshot had one --
        # if this starts failing, either the snapshot changed or the price
        # field name did.
        if raw.get("stockStatus") == "OK":
            assert product["price"] is not None
        # Every real result in the snapshot carries multiple distinct
        # photos (front/angle/side/etc) -- this guards against the exact
        # regression that shipped once already (only imageAssets[0] kept,
        # silently dropping every other angle, caught from a direct user
        # question about why only one photo was coming back).
        raw_image_count = len(raw.get("imageAssets") or [])
        if raw_image_count > 1:
            assert len(product["image_urls"]) > 1


def test_simplify_store_against_real_snapshot():
    location = _load("get_pickup_location")
    store = _simplify_store(location)
    assert store["store_id"] is not None
    assert store["name"] is not None
    assert store["address"] is not None


def test_simplify_order_detail_against_real_snapshot():
    detail = _load("get_historical_order")
    result = _simplify_order_detail(detail)
    assert result["order_number"] is not None or result["status"] is not None
    assert result["item_count"] > 0
    for item in result["items"]:
        assert item["name"] is not None


def test_simplify_order_summary_against_real_snapshot():
    orders = _load("get_historical_orders")
    order_history = orders.get("orderHistory") or []
    assert order_history, "snapshot has no orderHistory entries"
    for raw in order_history:
        summary = _simplify_order_summary(raw)
        assert summary["store"] is not None
        assert summary["order_type"] is not None


def test_simplify_loyalty_against_real_snapshot():
    profile = _load("get_profile")
    promotions = _load("get_customer_promotions")
    result = _simplify_loyalty(profile, promotions)
    assert result["in_pc_optimum"] is True
    # Points balance is redacted to "REDACTED" (a string) in the committed
    # snapshot -- this only asserts the field is *present and non-null*,
    # not the real numeric value (which never gets committed).
    assert result["points_balance"] is not None
    assert result["stamp_card_active"] is False


def test_simplify_nutrition_against_real_snapshot():
    # Real Open Food Facts response for a real product actually sold at
    # this store, found via barcode reconstruction -- see
    # nutrition_client.py's docstring for the bug this works around
    # (PC Express's own barcode field is truncated) and docs/RESEARCH.md
    # for the investigation. Not personal data -- generic product info.
    product = _load("openfoodfacts_product")
    result = _simplify_nutrition(product)
    assert result["product_name"] is not None
    assert result["nutriscore_grade"] is not None
    assert result["nova_group"] is not None
    assert result["per_100g"]["energy_kcal"] is not None
    assert result["dietary_flags"]["palm_oil_free"] == "yes"
    assert result["allergen_status"]["milk"] == "contains"
    assert result["nutrient_levels"]["saturated_fat"] == "low"  # hyphen normalized to underscore
    assert result["ingredients_text"] is not None
