"""Open Food Facts client -- nutrition/ingredient enrichment via barcode lookup.

Why this exists: PC Express's own API never returns nutrition facts or an
ingredients list from any endpoint this project could find (see
docs/RESEARCH.md "Product detail endpoint"). Open Food Facts
(https://world.openfoodfacts.org, ODbL-licensed, community-maintained) is
a free, barcode-indexed nutrition database that turned out to have
excellent real coverage of this store's catalog (100% hit rate across a
15-item live sample) -- but only once a real bug in PC Express's own
barcode data was worked around, see `normalize_barcode` below. A naive
first attempt using PC Express's `upcs` values as-is had a 0% hit rate
across 40 real products including major global brands; that turned out to
be the barcode bug, not a real Open Food Facts coverage gap. See
docs/RESEARCH.md "Nutrition enrichment (Open Food Facts)" for the full
investigation.

Deliberately calls the plain REST API directly via `httpx` rather than
adding the `openfoodfacts` PyPI package as a dependency: that package
pulls in `requests` (a second, redundant HTTP client alongside the httpx
this project already uses everywhere) and `tqdm` (a progress bar,
irrelevant to a single lookup) for what this project actually needs, which
is one GET request with a fields filter. Not worth the added dependency
surface -- see README "Dependency policy".

No PC Express account, session, or auth is needed for anything in this
module -- it's a wholly separate, unauthenticated public API.

Rate limit, per Open Food Facts' own API docs
(https://openfoodfacts.github.io/openfoodfacts-server/api/), not just
this project's own empirical observation: **15 requests/minute/IP** for
product read queries (`GET /api/v*/product`), separate from and stricter
than most people would assume. Their docs explicitly recommend against
anything resembling search-as-you-type, and for bulk needs (many products
at once) recommend downloading their CSV/JSONL data export and
self-hosting rather than repeated API calls -- reinforces why this module
is deliberately single-lookup, no batching, no retry loop. A staging
mirror exists at https://world.openfoodfacts.net (HTTP basic auth
`off`/`off`) for development/testing against without touching the
production rate limit -- worth using if iterating on this module further.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from . import config


class OpenFoodFactsRateLimited(RuntimeError):
    """Raised on HTTP 429 specifically, distinct from a plain not-found
    (which fetch_nutrition returns as None) -- callers should tell the
    user "try again shortly," not "no data available for this product."
    """


def _ean13_check_digit(twelve_digits: str) -> str:
    nums = [int(d) for d in twelve_digits]
    odd_sum = sum(nums[0:12:2])  # positions 1,3,5,...,11 (0-indexed 0,2,4,...,10)
    even_sum = sum(nums[1:12:2])  # positions 2,4,6,...,12 (0-indexed 1,3,5,...,11)
    total = odd_sum + even_sum * 3
    return str((10 - (total % 10)) % 10)


def normalize_barcode(raw: str) -> Optional[str]:
    """Reconstruct a real, lookupable EAN-13 from whatever barcode-ish
    string is passed in.

    The 11-digit case is the important one: PC Express's `upcs` field (see
    `_simplify_product` in server.py) is NOT a complete, standard barcode
    -- confirmed live by finding the same product in Open Food Facts under
    its real 13-digit code and diffing the two. PC Express's version has
    both the EAN-13's leading `0` AND its own trailing check digit
    stripped (e.g. real code `0065633132122` for a real product shows up
    from PC Express as `06563313212` -- missing the leading `0` and the
    final `2`). Reconstructing means prepending the `0` back and
    *computing* (not guessing) the correct check digit -- confirmed to
    produce real, correct, resolvable barcodes across every sample checked
    (15/15 live).

    7-digit codes (also seen in PC Express's real data) are intentionally
    rejected (returns None): those are internal PLU-style codes for
    items sold by weight (fresh meat/deli/produce), which genuinely have
    no manufacturer barcode to look up -- not a bug, a real absence.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 11:
        prefix = "0" + digits
        return prefix + _ean13_check_digit(prefix)
    if len(digits) == 12:
        return "0" + digits
    if len(digits) in (8, 13):  # EAN-8 or already a full EAN-13
        return digits
    return None


# Kept short and specific on purpose -- see server.py's get_nutrition_info
# docstring for why (avoid pulling back OFF's much larger raw payload,
# which carries dozens of internal/contributor-tracking fields unrelated
# to "is this food any good").
_FIELDS = (
    "product_name,brands,quantity,nutriscore_grade,nova_group,ecoscore_grade,nutriments,"
    "ingredients_text,ingredients_text_en,ingredients_lc,allergens,allergens_tags,traces_tags,"
    "ingredients_analysis_tags,nutrient_levels,additives_tags"
)


def fetch_nutrition(barcode: str, client: Optional[httpx.Client] = None) -> Optional[dict[str, Any]]:
    """Look up one product by barcode. Returns None if not found, malformed,
    or the request otherwise fails -- callers treat "not found" as a
    normal, expected outcome (real coverage varies by product), not an
    error. Raises OpenFoodFactsRateLimited specifically on HTTP 429 (see
    that class's docstring) so callers can distinguish "no data" from "hit
    the rate limit, ask again later" rather than conflating the two.

    Deliberately a single request, no retry/backoff: see this module's
    docstring for Open Food Facts' own documented 15 req/min/IP limit on
    read queries -- this is meant to be called for one product a person
    actually cares about, not looped over a whole search-results page.
    See get_nutrition_info's docstring.

    `client` is injectable (defaults to a fresh one-off httpx.Client) so
    tests can pass an httpx.Client(transport=httpx.MockTransport(...))
    instead of hitting the real network -- same pattern as auth.py's
    raw_exchange_authorization_code.
    """
    normalized = normalize_barcode(barcode)
    if not normalized:
        return None
    url = f"{config.OPENFOODFACTS_BASE}/product/{normalized}.json"
    headers = {"User-Agent": config.OPENFOODFACTS_USER_AGENT}
    try:
        if client is not None:
            resp = client.get(url, params={"fields": _FIELDS}, headers=headers)
        else:
            with httpx.Client(timeout=10.0) as owned_client:
                resp = owned_client.get(url, params={"fields": _FIELDS}, headers=headers)
    except httpx.HTTPError:
        return None
    if resp.status_code == 429:
        raise OpenFoodFactsRateLimited("Open Food Facts rate limit hit (15 req/min/IP for product lookups). Try again shortly.")
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if data.get("status") != 1:
        return None
    product = data.get("product")
    return product if isinstance(product, dict) else None
