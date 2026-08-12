"""Unit tests for the response-simplifier functions in server.py.

Fixture shapes below are reconstructed from the real (scrubbed) response
shapes confirmed live during development -- see the docstrings on each
_simplify_* function in server.py for what was actually verified vs.
speculative (e.g. _simplify_slot's field names are unconfirmed since that
endpoint's real shape has never been publicly documented).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp.server import (  # noqa: E402
    _allergen_status,
    _dietary_flags,
    _markdown_image,
    _simplify_cart,
    _simplify_loyalty,
    _simplify_nutrition,
    _simplify_order_detail,
    _simplify_product,
    _simplify_slot,
    _simplify_store,
    _strip_html,
)


def test_markdown_image_builds_ready_to_paste_tag():
    assert _markdown_image("2% Milk", "https://x/milk.png") == "![2% Milk](https://x/milk.png)"


def test_markdown_image_none_when_no_url():
    assert _markdown_image("2% Milk", None) is None


def test_markdown_image_escapes_brackets_in_name_to_avoid_breaking_markdown():
    assert _markdown_image("Weird [Name]", "https://x/a.png") == "![Weird (Name)](https://x/a.png)"


def test_markdown_image_falls_back_to_generic_alt_text_when_name_missing():
    assert _markdown_image(None, "https://x/a.png") == "![product](https://x/a.png)"


def test_simplify_product_full_shape_matches_real_search_result():
    # Real shape, confirmed live against real search results (a previous
    # version of this fixture assumed a much flatter, unverified shape --
    # see _simplify_product's docstring for the enrichment/pagination pass
    # that replaced it after a real user asked for richer data).
    product = {
        "code": "20708942_EA",
        "articleNumber": "20708942",
        "upcs": ["06354951085", "00000000000"],
        "name": "Light Cocktail Bocconcini Cheese",
        "brand": "Saputo",
        "description": "<p>A lighter take on a favourite.</p> Fresh and milky.",
        "packageSize": "200 g",
        # Real products carry multiple distinct photos (front/angle/side/etc,
        # confirmed live: 3-9+ per product) -- this fixture has two to prove
        # both are kept, not just imageAssets[0].
        "imageAssets": [
            {"thumbnailUrl": "https://x/front-thumb.png", "mediumUrl": "https://x/front-medium.png", "largeUrl": "https://x/front-large.png"},
            {"thumbnailUrl": "https://x/side-thumb.png", "mediumUrl": "https://x/side-medium.png", "largeUrl": "https://x/side-large.png"},
        ],
        "aisle": "12A",
        "stockStatus": "OK",
        "ingredients": None,
        "prices": {
            "price": {"value": 5.5},
            "wasPrice": {"value": 6.0},
            "comparisonPrices": [{"value": 2.75, "unit": "g", "quantity": 100}],
        },
        "mopDealPrice": 5.25,
        "badges": {
            "dealBadge": {"text": "SAVE $0.50"},
            "loyaltyBadge": {"points": 500},
        },
    }
    result = _simplify_product(product)
    assert result == {
        "code": "20708942_EA",
        "sku": "20708942",
        "barcode": "06354951085",  # first of possibly several -- not all of them
        "name": "Light Cocktail Bocconcini Cheese",
        "brand": "Saputo",
        "description": "A lighter take on a favourite. Fresh and milky.",  # HTML stripped
        "package_size": "200 g",
        # One representative size *per distinct photo*, not just the first
        # angle -- each photo's other 4-7 same-image size variants are
        # dropped, but every genuinely different angle is kept.
        "image_urls": ["https://x/front-medium.png", "https://x/side-medium.png"],
        "photo_markdown": "![Light Cocktail Bocconcini Cheese](https://x/front-medium.png)",
        "aisle": "12A",
        "stock_status": "OK",
        "price": 5.5,
        "regular_price": 6.0,
        "member_price": 5.25,
        "unit_price": {"value": 2.75, "per": "100g"},
        "deal_text": "SAVE $0.50",
        "loyalty_points": 500,
    }


def test_simplify_product_handles_weighted_item():
    # Real shape, confirmed live: items sold by weight (deli, some meat/
    # produce) use a "_KG" code suffix instead of "_EA", and packageSize
    # comes back as an empty string rather than null or a fixed size --
    # there's no fixed package when you're charged per kg. Checked across
    # produce/meat/seafood/deli/household/baby/frozen categories (9+
    # queries, 30+ real products) after a direct question about whether
    # this generalizes -- everything else in the shape held up the same
    # as the "_EA" case, only these two fields differ.
    product = {
        "code": "20323392_KG",
        "articleNumber": "20323392",
        "name": "Black Forest Ham, Extra Lean",
        "brand": "Ziggy's",
        "packageSize": "",
        "stockStatus": "OK",
        "prices": {
            "price": {"value": 3.2},
            "comparisonPrices": [{"value": 32.0, "unit": "kg", "quantity": 1}],
        },
    }
    result = _simplify_product(product)
    assert result["code"] == "20323392_KG"
    assert result["package_size"] == ""
    assert result["price"] == 3.2
    assert result["unit_price"] == {"value": 32.0, "per": "1kg"}


def test_simplify_product_handles_low_stock_status():
    result = _simplify_product({"code": "1", "name": "Snacks", "stockStatus": "LOW"})
    assert result["stock_status"] == "LOW"


def test_simplify_product_truncates_long_description():
    long_description = "word " * 200  # far over the 400-char cutoff
    result = _simplify_product({"code": "1", "name": "Bread", "description": long_description})
    assert len(result["description"]) <= 401  # 400 chars + the ellipsis char
    assert result["description"].endswith("…")


def test_simplify_product_handles_missing_prices_and_images():
    result = _simplify_product({"code": "1", "name": "Bread"})
    assert result["price"] is None
    assert result["regular_price"] is None
    assert result["unit_price"] is None
    assert result["image_urls"] == []
    assert result["barcode"] is None
    assert result["photo_markdown"] is None


def test_strip_html_removes_tags_and_collapses_whitespace():
    assert _strip_html("<p>Hello</p>  <b>world</b>") == "Hello world"
    assert _strip_html("A&nbsp;B&amp;C") == "A B&C"
    assert _strip_html(None) is None
    assert _strip_html("") == ""


def test_simplify_cart_flattens_orders_and_entries():
    # Real shape, confirmed live against a real 3-item cart -- an earlier
    # version of this fixture assumed entry.product/entry.code/
    # entry.totalPrice directly, which silently produced an all-null items
    # list against the real API (caught from a real user report). The
    # actual nesting is entry.offer.product / entry.offer.id /
    # entry.prices.totalSalePrice, and order totals live on
    # order.totals.totalPrice, not the cart object itself.
    cart = {
        "id": "cart-abc",
        "status": "OPEN",
        "minCartValue": 30.0,
        "orders": [
            {
                "totals": {"subTotal": 17.69, "totalPrice": 18.19, "totalTax": 0.50},
                "entries": [
                    {
                        "quantity": 2.0,
                        "offer": {
                            "id": "111_EA",
                            "product": {
                                "id": "111_EA",
                                "name": "Eggs",
                                "brand": "No Name",
                                "primaryImage": "https://x/eggs.png",
                            },
                        },
                        "prices": {"totalSalePrice": 3.98, "totalRegularPrice": 4.5},
                    },
                    {
                        "quantity": 1.0,
                        "offer": {"id": "222_EA", "product": {"id": "222_EA", "name": "Cheese"}},
                        "prices": {"totalRegularPrice": 5.99},
                    },
                ],
            }
        ],
    }
    result = _simplify_cart(cart)
    assert result["cart_id"] == "cart-abc"
    assert result["status"] == "OPEN"
    assert result["min_cart_value"] == 30.0
    assert result["item_count"] == 2
    assert result["raw_total"] == 18.19
    assert result["items"] == [
        {
            "code": "111_EA",
            "name": "Eggs",
            "quantity": 2.0,
            "total_price": 3.98,
            "photo_markdown": "![Eggs](https://x/eggs.png)",
        },
        # totalSalePrice absent -> falls back to totalRegularPrice; no
        # primaryImage on this one -> photo_markdown is None, not omitted.
        {"code": "222_EA", "name": "Cheese", "quantity": 1.0, "total_price": 5.99, "photo_markdown": None},
    ]


def test_simplify_cart_handles_no_orders():
    result = _simplify_cart({"id": "cart-empty"})
    assert result["item_count"] == 0
    assert result["items"] == []
    assert result["raw_total"] is None


def test_simplify_cart_handles_multiple_orders():
    cart = {
        "id": "cart-multi",
        "orders": [
            {
                "totals": {"totalPrice": 10.0},
                "entries": [{"quantity": 1, "offer": {"id": "a"}, "prices": {"totalSalePrice": 10.0}}],
            },
            {
                "totals": {"totalPrice": 5.0},
                "entries": [{"quantity": 1, "offer": {"id": "b"}, "prices": {"totalSalePrice": 5.0}}],
            },
        ],
    }
    result = _simplify_cart(cart)
    assert result["item_count"] == 2
    assert result["raw_total"] == 15.0


def test_simplify_slot_prefers_primary_field_names():
    slot = {"startTime": "2026-08-10T09:00:00", "available": True, "fee": {"value": 5.0}}
    result = _simplify_slot(slot)
    assert result["start_time"] == "2026-08-10T09:00:00"
    assert result["available"] is True
    assert result["fee"] == {"value": 5.0}


def test_simplify_slot_falls_back_to_alias_field_names():
    slot = {"slotFee": {"value": 3.5}, "type": "PICKUP"}
    result = _simplify_slot(slot)
    assert result["fee"] == {"value": 3.5}
    assert result["slot_type"] == "PICKUP"
    assert result["start_time"] is None


def test_simplify_order_detail_trims_line_items_and_totals():
    detail = {
        "orderDetails": {
            "orderNumber": "CA123456789",
            "orderType": "PICKUP",
            "status": "COMPLETED",
            "bannerName": "Real Canadian Superstore",
            "subTotal": {"value": 50.0},
            "totalPriceWithTax": {"value": 55.0},
            "totalTax": {"value": 5.0},
            "totalDiscounts": {"value": 2.0},
            "totalItems": 2,
            "booking": {
                "pickupLocation": {
                    "name": "Superstore Oakville South",
                }
            },
            "entries": [
                {
                    "product": {
                        "articleNumber": "111",
                        "productName": "Bananas",
                        "brand": "No Name",
                        "irrelevantField": "should be dropped",
                    },
                    "quantity": 3,
                    "unitPrice": {"value": 0.69},
                    "totalPrice": {"value": 2.07},
                    "availabilityStatus": "FULLY_AVAILABLE",
                },
                {
                    "product": {"id": "222", "productName": "Milk"},
                    "quantity": 1,
                    "unitPrice": {"value": 4.49},
                    "totalPrice": {"value": 4.49},
                    "availabilityStatus": "FULLY_AVAILABLE",
                },
            ],
        },
        "pointsEarned": 55,
        "pointsRedeemed": 0,
    }
    result = _simplify_order_detail(detail)
    assert result["order_number"] == "CA123456789"
    assert result["status"] == "COMPLETED"
    assert result["store_name"] == "Superstore Oakville South"
    assert result["sub_total"] == {"value": 50.0}
    assert result["total_price"] == {"value": 55.0}
    assert result["total_items"] == 2
    assert result["points_earned"] == 55
    assert result["item_count"] == 2
    assert result["items"][0] == {
        "code": "111",
        "name": "Bananas",
        "brand": "No Name",
        "quantity": 3,
        "unit_price": {"value": 0.69},
        "total_price": {"value": 2.07},
        "availability_status": "FULLY_AVAILABLE",
    }
    assert result["items"][1]["code"] == "222"


def test_simplify_order_detail_falls_back_when_status_missing():
    detail = {
        "orderDetails": {"orderNumber": "CA1", "entries": []},
        "statusDisplay": "Delivered",
    }
    result = _simplify_order_detail(detail)
    assert result["status"] == "Delivered"
    assert result["items"] == []
    assert result["item_count"] == 0


def test_simplify_order_detail_falls_back_to_delivery_status():
    detail = {"orderDetails": {"deliveryStatus": "IN_TRANSIT", "entries": []}}
    result = _simplify_order_detail(detail)
    assert result["status"] == "IN_TRANSIT"


def test_simplify_order_detail_handles_missing_order_details():
    result = _simplify_order_detail({})
    assert result["order_number"] is None
    assert result["items"] == []


def test_simplify_store_trims_address_and_drops_bulk_fields():
    loc = {
        "storeId": "1234",
        "name": "Superstore Oakville South",
        "pickupType": "PICKUP",
        "locationType": "STORE",
        "isShoppable": True,
        "visible": True,
        "minCartValue": 35.0,
        "address": {"formattedAddress": "123 Main St, Oakville, ON"},
        "orderContactNumber": "905-555-0100",
        "pickupInstructions": "Park in designated PC Express spots.",
        "timeZone": "America/Toronto",
        "storeDetails": {"hours": "huge nested blob"},
        "departments": [{"name": "Dairy"}] * 39,
        "openNowResponseData": {"isOpen": True},
    }
    result = _simplify_store(loc)
    assert result == {
        "store_id": "1234",
        "name": "Superstore Oakville South",
        "pickup_type": "PICKUP",
        "location_type": "STORE",
        "is_shoppable": True,
        "visible": True,
        "min_cart_value": 35.0,
        "address": "123 Main St, Oakville, ON",
        "phone": "905-555-0100",
        "pickup_instructions": "Park in designated PC Express spots.",
        "timezone": "America/Toronto",
    }


def test_simplify_store_falls_back_to_pickup_location_id():
    result = _simplify_store({"pickupLocationId": "5678", "name": "No Frills"})
    assert result["store_id"] == "5678"


def test_simplify_store_handles_missing_address():
    result = _simplify_store({"storeId": "1"})
    assert result["address"] is None


def test_simplify_loyalty_real_shape():
    # Real shape, confirmed live: profile.pcOptimum.points is real,
    # populated account data; promotions.stampCards was only confirmed in
    # the inactive shape (this account's stamp card program isn't
    # currently running) -- see _simplify_loyalty's docstring.
    profile = {
        "inPCOptimum": True,
        "pcOptimum": {
            "points": {"balance": 203086, "dollarsRedeemable": 200, "dollarsRedeemedLifetime": 72000},
        },
    }
    promotions = {
        "stampCards": {"isActive": False, "balance": None, "rewards": None},
    }
    result = _simplify_loyalty(profile, promotions)
    assert result == {
        "in_pc_optimum": True,
        "points_balance": 203086,
        "dollars_redeemable": 200,
        "dollars_redeemed_lifetime": 72000,
        "stamp_card_active": False,
        "stamp_card_balance": None,
        "stamp_card_rewards": None,
    }


def test_simplify_loyalty_handles_missing_data():
    result = _simplify_loyalty({}, {})
    assert result["points_balance"] is None
    assert result["stamp_card_active"] is None


def test_simplify_nutrition_real_shape():
    # Real shape, confirmed live: Open Food Facts' real response for a
    # product actually sold at this store (found via barcode
    # reconstruction -- see nutrition_client.py). Trimmed to the fields
    # this project actually uses; the real response carries dozens more
    # (contributor tags, per-language variants, etc.) that are dropped.
    product = {
        "product_name": "2% M.F. Fresh Partly Skimmed Milk",
        "brands": "Neilson, Saputo",
        "quantity": "4 L",
        "nutriscore_grade": "b",
        "nova_group": 1,
        "ecoscore_grade": "c",
        "ingredients_text": "PARTLY SKIMMED MILK, VITAMIN A PALMITATE, VITAMIN D3.",
        "allergens": "milk",
        "allergens_tags": ["en:milk"],
        "traces_tags": [],
        "ingredients_analysis_tags": ["en:palm-oil-free", "en:non-vegan", "en:maybe-vegetarian"],
        "nutrient_levels": {"fat": "low", "salt": "low", "saturated-fat": "low", "sugars": "low"},
        "additives_n": 0,
        "additives_tags": [],
        "nutriments": {
            "energy-kcal_100g": 52,
            "proteins_100g": 3.6,
            "fat_100g": 2,
            "saturated-fat_100g": 1.2,
            "carbohydrates_100g": 4.8,
            "sugars_100g": 4.8,
            "fiber_100g": 0,
            "salt_100g": 0.12,
        },
    }
    result = _simplify_nutrition(product)
    assert result == {
        "product_name": "2% M.F. Fresh Partly Skimmed Milk",
        "brands": "Neilson, Saputo",
        "quantity": "4 L",
        "nutriscore_grade": "b",
        "nova_group": 1,
        "ecoscore_grade": "c",
        "ingredients_text": "PARTLY SKIMMED MILK, VITAMIN A PALMITATE, VITAMIN D3.",
        "allergens": "milk",
        "allergen_status": {"gluten": "not_declared", "milk": "contains", "soy": "not_declared", "sulfites": "not_declared"},
        "dietary_flags": {"vegan": "no", "vegetarian": "maybe", "palm_oil_free": "yes"},
        "nutrient_levels": {"fat": "low", "salt": "low", "saturated_fat": "low", "sugars": "low"},
        "additives": {"count": 0, "codes": []},
        "per_100g": {
            "energy_kcal": 52,
            "protein_g": 3.6,
            "fat_g": 2,
            "saturated_fat_g": 1.2,
            "carbohydrates_g": 4.8,
            "sugars_g": 4.8,
            "fiber_g": 0,
            "salt_g": 0.12,
        },
    }


def test_simplify_nutrition_handles_missing_data():
    result = _simplify_nutrition({})
    assert result["product_name"] is None
    assert result["per_100g"]["energy_kcal"] is None
    assert result["dietary_flags"] == {"vegan": "unknown", "vegetarian": "unknown", "palm_oil_free": "unknown"}
    assert result["allergen_status"] == {
        "gluten": "not_declared",
        "milk": "not_declared",
        "soy": "not_declared",
        "sulfites": "not_declared",
    }
    assert result["additives"] == {"count": None, "codes": []}


def test_dietary_flags_palm_oil_contains_is_not_confused_with_palm_oil_free():
    # The exact real bug caught before shipping: a generic tag->key
    # pattern-match once mapped "en:palm-oil" (contains palm oil) to
    # "yes, palm-oil-free" because it shared a prefix with "palm-oil-free".
    assert _dietary_flags(["en:palm-oil"]) == {"vegan": "unknown", "vegetarian": "unknown", "palm_oil_free": "no"}
    assert _dietary_flags(["en:palm-oil-free"]) == {"vegan": "unknown", "vegetarian": "unknown", "palm_oil_free": "yes"}
    assert _dietary_flags(["en:may-contain-palm-oil"]) == {"vegan": "unknown", "vegetarian": "unknown", "palm_oil_free": "maybe"}


def test_dietary_flags_vegan_vegetarian_variants():
    assert _dietary_flags(["en:vegan", "en:vegetarian"])["vegan"] == "yes"
    assert _dietary_flags(["en:non-vegan"])["vegan"] == "no"
    assert _dietary_flags(["en:maybe-vegan"])["vegan"] == "maybe"
    assert _dietary_flags(["en:vegan-status-unknown"])["vegan"] == "unknown"


def test_allergen_status_distinguishes_contains_from_traces():
    result = _allergen_status(["en:gluten"], ["en:soybeans"])
    assert result["gluten"] == "contains"
    assert result["soy"] == "may_contain"
    assert result["milk"] == "not_declared"
