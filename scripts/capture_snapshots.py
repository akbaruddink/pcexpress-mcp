#!/usr/bin/env python3
"""Capture anonymized raw API response snapshots into tests/fixtures/raw_responses/.

Run this against your own real, logged-in account occasionally (not on any
schedule -- there's no way to detect a shape change except by looking) to
refresh the baseline these snapshots represent:

    python scripts/capture_snapshots.py

Requires an active session (run scripts/login.py first, or pass
`--auth-state PATH`) and PCEXPRESS_STORE_ID. Everything captured is
read-only: booking a slot is never captured automatically.

Why this exists: every `_simplify_*` function in server.py was built
against an *assumption* about response shape at least once, and at least
one of those assumptions (`_simplify_cart`) turned out to be wrong the
first time it hit a real, non-empty cart -- caught from a real user report,
not from any test, because there was no real response on file to compare
against. These snapshots are that reference: not test fixtures pytest
runs against automatically (they're real API captures), but a checked
baseline a human/agent can diff a new raw response against when
investigating a "the tool used to work, now it returns nulls" report,
instead of re-deriving the whole shape from scratch.

Anonymization is defense-in-depth, not a single mechanism, because a single
mechanism already proved insufficient here: a first pass at this script
used only key-name-substring matching and still shipped a real customer's
full name, email, and internal PC Optimum member/wallet IDs -- caught by a
manual review before committing, exactly because "name"/"id"/"uid" are
used for both genuinely personal fields (customer.name, user.uid) *and*
harmless structural ones (product.name, offer.id) depending on where
they're nested, so no flat key-matcher can get both right. This version
combines three layers: (1) known-personal *sub-objects* redacted wholesale
by path (customer/user/shippingAddress blocks, not just individual
fields inside them), (2) broad key-substring matching for unambiguous
categories (email, phone, address, postal code) that are safe to blanket-
match anywhere, (3) exact-value scrubbing of this specific run's actual
discovered PII (email, name, loyalty IDs) wherever it appears in the tree,
regardless of key.

**Always manually review every output file before committing anyway.**
This project got burned by trusting automated redaction once already, and
that's the whole reason this docstring is this long.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import config, nutrition_client
from pc_express_mcp.api_client import PCExpressAPI
from pc_express_mcp.auth import PcidAuthError, TokenManager

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "raw_responses"
PLACEHOLDER = "REDACTED"

# Layer 1: sub-objects known to hold personal data, redacted wholesale
# (every field inside, not cherry-picked) wherever this exact key path
# appears, regardless of which endpoint it's found in. Path segments are
# matched by exact key name at any depth -- e.g. "customer" matches
# cart.customer AND orderDetails.customer.
# Keys are compared lowercased with underscores removed, so the checkout
# service's snake_case (phone_number, address_line_1, last_4_digits) is
# caught by the same entries as pcx-bff's camelCase (phoneNumber).
WHOLESALE_REDACT_KEYS = {
    "customer",
    "user",
    "shippingaddress",
    "deliveryaddress",
    "billingaddress",
    "addressdata",
    "shippinginfo",
    "paymentmethods",
}

# Layer 2: individual keys safe to blanket-substring-match anywhere,
# because they're distinctive enough not to collide with legitimate
# product/store/order structural fields (unlike bare "id" or "name").
SENSITIVE_KEY_SUBSTRINGS = (
    "email",
    "phone",
    "postalcode",
    "firstname",
    "lastname",
    "customerid",
    "custid",
    "cartid",
    "orderid",
    "ordernumber",
    "accountid",
    "loyaltyid",
    "pcoptimumid",
    "memberid",
    "walletid",
    "membershipid",
    "membershipcardnumber",
    "householdid",
    "bookingid",
    "pcid",
    "requestid",
    "sessionid",
    "ownername",
    "manager",
    "deliveryinstructions",
    "comment",
    "streetaddress",
    "addressline",
    "addressid",
    "paymentmethodid",
    "last4",
    "cardholder",
)

# Bare "id"/"uid" mean wildly different things depending on which object
# they're on (a cart's top-level "id" IS its cart_id; product.id is benign),
# so they're redacted only at the top level -- see TOP_LEVEL_ID_LIKE_KEYS.
EXACT_KEY_OVERRIDES = {"uid"}
TOP_LEVEL_ID_LIKE_KEYS = {"id", "cartid"}


def _norm(key: str) -> str:
    return key.lower().replace("_", "")


def _redact_value_by_key(value: Any, key: str) -> Any:
    key_lower = _norm(key)
    if key_lower in WHOLESALE_REDACT_KEYS and isinstance(value, dict):
        return {k: PLACEHOLDER for k in value}
    if isinstance(value, dict):
        return {k: _redact_value_by_key(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value_by_key(v, key) for v in value]
    if any(s in key_lower for s in SENSITIVE_KEY_SUBSTRINGS) or key_lower in EXACT_KEY_OVERRIDES:
        return PLACEHOLDER if value is not None else None
    return value


def _scrub_known_values(data: Any, known_values: list[str]) -> Any:
    """Layer 3: exact-value scrub, independent of key name entirely --
    catches a leaked value under a key name nobody thought to list.
    """
    text = json.dumps(data)
    for value in known_values:
        if value:
            text = text.replace(value, PLACEHOLDER)
    return json.loads(text)


def _redact(data: Any, known_values: list[str]) -> Any:
    redacted = _redact_value_by_key(data, "")
    if isinstance(redacted, dict):
        for key in list(redacted.keys()):
            if _norm(key) in TOP_LEVEL_ID_LIKE_KEYS:
                redacted[key] = PLACEHOLDER
    return _scrub_known_values(redacted, known_values)


def _save(name: str, data: Any, known_values: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(_redact(data, known_values), f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    print(f"wrote {path}")


def main() -> int:
    banner = config.DEFAULT_BANNER
    store_id = config.DEFAULT_STORE_ID
    if not store_id:
        print("PCEXPRESS_STORE_ID is not set -- needed to call search/cart/pickup-location.", file=sys.stderr)
        return 1

    auth_state = sys.argv[sys.argv.index("--auth-state") + 1] if "--auth-state" in sys.argv else None
    tm = TokenManager(auth_state)
    api = PCExpressAPI(tm, banner)

    try:
        profile = api.get_profile()
    except PcidAuthError as exc:
        print(f"Not logged in: {exc}", file=sys.stderr)
        return 1

    # Known-real values for this specific account, scrubbed everywhere
    # regardless of which key they turn up under (layer 3 above).
    known_values = [v for v in (profile.get("id"), profile.get("firstName"), profile.get("lastName")) if v]
    email = profile.get("id") or ""
    if email and "@" in email:
        # The email's local-part alone (e.g. "jsmith82" from
        # jsmith82@example.com) also shows up concatenated into other
        # fields in practice (confirmed: a first capture run here leaked
        # exactly this way) -- scrub it too.
        known_values.append(email.split("@")[0])

    # Real loyalty point/dollar balances are personal financial data even
    # without a name attached -- "points" itself is too generic a key to
    # blanket-redact (it also appears, harmlessly, on per-product loyalty
    # badges in search results), so this is scoped to exactly this path.
    if isinstance(profile.get("pcOptimum"), dict) and isinstance(profile["pcOptimum"].get("points"), dict):
        profile["pcOptimum"]["points"] = {k: PLACEHOLDER for k in profile["pcOptimum"]["points"]}

    _save("get_profile", profile, known_values)

    promotions = api.get_customer_promotions()
    _save("get_customer_promotions", promotions, known_values)

    # list_carts, not profile.cartId: the latter is banner-invariant.
    carts = api.list_carts(profile.get("id") or profile.get("customerId")).get("carts") or []
    cart_id = carts[0].get("id") if carts else None
    known_values += [v for v in (cart_id, profile.get("customerId")) if v]
    if cart_id:
        cart = api.get_cart(cart_id)
        _save("get_cart", cart, known_values)
        courier = ((cart.get("orders") or [{}])[0].get("fulfillment") or {}).get("courier") or {}
        location_id, postal = courier.get("fulfillmentLocationId"), (courier.get("deliveryAddress") or {}).get("postalCode")
        if location_id and postal:
            known_values.append(postal)
            slots = api.get_delivery_slots(cart_id, location_id, postal)
            # ~300 slots; a few per date is enough to pin the shape.
            for loc in slots.values():
                if isinstance(loc, dict) and isinstance(loc.get("timeslots"), list):
                    loc["timeslots"] = loc["timeslots"][:3] + [t for t in loc["timeslots"] if t.get("available")][:3]
            _save("get_delivery_slots", slots, known_values)
            _save("get_checkout", api.get_checkout(cart_id), known_values)
        else:
            print("Cart has no delivery location/address -- skipping slot and checkout captures.", file=sys.stderr)
    else:
        print("No cart on this banner -- add an item via the app once to capture get_cart.", file=sys.stderr)

    search = api.search_products("milk", store_id, cart_id=cart_id, size=5)
    _save("search_products", search, known_values)

    # Open Food Facts -- not personal data (generic product info), no
    # redaction needed. Best-effort: their real 15 req/min/IP rate limit
    # (see nutrition_client.py) means a re-run of this script soon after
    # another one could legitimately fail here; that's fine, this snapshot
    # just won't refresh that run.
    for result in search.get("results", []):
        upcs = result.get("upcs") or []
        if not upcs:
            continue
        try:
            product = nutrition_client.fetch_nutrition(upcs[0])
        except nutrition_client.OpenFoodFactsRateLimited:
            print("Open Food Facts rate-limited -- skipping that snapshot this run.", file=sys.stderr)
            break
        if product:
            _save("openfoodfacts_product", product, [])
            break
    else:
        print("No Open Food Facts hit among this search's results -- skipping that snapshot.", file=sys.stderr)

    location = api.get_pickup_location(store_id)
    _save("get_pickup_location", location, known_values)

    orders = api.get_historical_orders()
    order_history = orders.get("orderHistory") or []
    detail_target_id = order_history[0]["id"] if order_history else None
    known_values += [o["id"] for o in order_history[:3] if o.get("id")]
    if len(order_history) > 3:
        order_history = order_history[:3]
    # Each order's own "id" is a real per-order reference number tied to
    # this account -- not caught by the top-level-only id redaction since
    # it's nested one level down inside the list, not the response root.
    order_history = [{**o, "id": PLACEHOLDER} for o in order_history]
    orders = {**orders, "orderHistory": order_history}
    _save("get_historical_orders", orders, known_values)
    if detail_target_id:
        detail = api.get_historical_order(detail_target_id)
        _save("get_historical_order", detail, known_values)
    else:
        print("No historical orders -- skipping get_historical_order capture.", file=sys.stderr)

    print(
        "\nDone. Review every file in tests/fixtures/raw_responses/ BY HAND before "
        "committing -- run something like:\n"
        "  grep -rniE '[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}' tests/fixtures/raw_responses/\n"
        "and look for anything that reads like a real name, address, or phone number "
        "this redaction pass didn't anticipate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
