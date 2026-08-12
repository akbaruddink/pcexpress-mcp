# Research summary (how we know any of this)

There's no public PC Express API documentation. Everything here comes from
two already-public reverse-engineering projects, not from us intercepting
or decompiling anything ourselves:

- **[FireBall1725/pcexpress-mcp-server](https://github.com/FireBall1725/pcexpress-mcp-server)**
  (AGPL-3.0) — an existing MCP server covering search/cart/order-history.
  This project's `config.py`/`auth.py`/`api_client.py` were built from it —
  and, importantly, cross-checked against its **actual Python source**
  (`pcid_config.py`, `pcid_token.py`, `pcexpress_mcp_server.py`), not just
  its markdown docs. That distinction mattered in practice: an early pass
  here, based only on an AI-generated summary of that repo's docs, invented
  a `Site-Banner: "loblaw"` (singular) mapping for the `loblaws` banner that
  doesn't exist in the real code — it's just the raw banner key
  (`"loblaws"`) everywhere. Caught and fixed by reading the source directly.
  That repo's own docs also distinguish endpoints it actually verified
  returning HTTP 200 against a live account from ones only "seen in the
  app" and never exercised — `api_client.py`'s docstrings carry that
  distinction forward (see the table below). Notably, that project
  explicitly does not implement checkout — nobody has published a working
  payment-submission endpoint.
- **[shmick/pcexpress-pickup](https://github.com/shmick/pcexpress-pickup)**
  (archived) — a pickup time-slot checker that documents a separate,
  *unauthenticated* `{banner-domain}/api/pickup-locations/{id}/time-slots`
  endpoint, used here for `get_available_slots`. This is a different host
  and system than pcx-bff — FireBall1725's own docs list timeslot routes as
  "seen in the app, never implemented," so this is the only lead available,
  not a confirmed-working pcx-bff route.

**Endpoint confidence, low to high:** `get_available_slots` (different,
unrelated project) < `list_stores`/store search (no API found at all,
manual store_id entry required) < `type_ahead`/`list_carts` (documented as
"verified" by prior art but not exercised by its actual shipped tool code)
< everything else (cross-checked against real, working source: profile,
cart get/update, product search, pickup-location lookup, order history).

**Bot protection:** the PC ID login page (`accounts.pcid.ca`) sits behind
**Akamai**, which is why login can't be scripted headlessly — you log in
once, in a real browser, and everything after that (token refresh, all
`pcx-bff` calls) works over plain HTTPS with no bot-wall encountered by
either prior-art project. `robots.txt` on the banner websites disallows
`/cart/`, `/checkout/`, `/account/` for crawlers — one more reason this
project talks to the app's own API instead of scraping HTML pages.

**Checkout/payment:** deliberately out of scope. No prior art documents a
working order-submission endpoint, which strongly suggests that surface has
stronger protection (payment tokenization, fraud checks) than the rest of
the API. Rather than guess at something that spends real money, this
project stops at "cart is ready" and hands off to the app. See
[`place_order` never submits
payment](ARCHITECTURE.md#place_order-never-submits-payment).

## Response-size verification

Verified against a live account, before → after this project's own response
simplifiers (`server.py`'s `_simplify_*` functions): a single order detail,
36,940 → 2,435 chars (15x). Store details, 14,221 → 399 chars (35x). Both
simplifiers drop large, low-value-per-byte structures — per-order-item
~60-field product objects (promo badges, loyalty fields, comparison prices,
images), and a store's full 7-day amenity-hours schedule (`storeDetails`)
plus its 39-entry department directory — while keeping everything actually
useful (totals, tax, points, item code/name/quantity/price; store
name/address/phone/pickup type).

## Search pagination

`search_products` (`products/search`) is genuinely paginated, not a
"return everything" endpoint pretending not to be one -- confirmed live,
unlike `get_historical_orders` (see below), which returns its complete
history in one call regardless of any params sent. Verified:

- `pagination.totalResults` in the raw response is a real count of
  matching products at the store (observed as high as 340 for a broad
  query), almost always far more than any single call returns.
- The request body's `pagination.from` is a real, working item offset --
  `from=5` returns genuinely different products than `from=0`, confirmed
  by comparing result codes across calls, not just trusting the echoed
  pagination metadata.
- The response's own `pagination.pageNumber` field is misleadingly named:
  it echoes back the raw `from` offset you sent, not a page *index*
  (`from=5, size=5` comes back as `pageNumber: 5`, not `1`).
- `totalResults` is not perfectly stable: the same query against the same
  store, called seconds apart, returned 136, then 128, then 127, then 141
  results total across otherwise-identical requests. The response's own
  `searchVariation`/`modelVersion` fields suggest this is an ML-driven,
  personalized/variant search, not a static index -- treat `totalResults`
  as "roughly this many," not exact.
- The actual number of items in `results` can run slightly over the
  requested `size` (observed: asked for 5, got 7; asked for 20, got 22;
  asked for 15, got 20) -- most likely sponsored/promoted items merged
  into the array beyond the organic page size, not confirmed from
  documentation (none exists). This isn't just a curiosity: it's why
  `search_products(include_nutrition=True)` truncates the *actual results
  list* to 15 after fetching, not just the requested `size` parameter --
  capping the request alone would not have reliably kept Open Food Facts
  lookups at or under 15 per call. See "Nutrition enrichment (Open Food
  Facts)" below.

This was missing from `search_products`'s original tool implementation --
the underlying `api_client.py` method already accepted an offset parameter,
but the MCP tool never exposed it, and the response handed back to Claude
silently dropped the real `totalResults` the API was already returning.
Fixed: see `search_products`'s docstring in `server.py`.

`get_historical_orders`, by contrast, is **not** paginated in practice: an
earlier investigation tried five different offset/limit/page query-param
candidates against it and every one was silently ignored, with the
endpoint returning the complete order history (284 orders, in the account
tested) in a single response regardless. The `limit` parameter on this
project's `get_order_status` tool is a client-side truncation of that full
list, not a real API-level page size.

## Search result shape across product categories

`_simplify_product`'s field mapping was originally verified only against
milk/cheese/baby-food results. After a direct question about whether it
generalizes, it was checked against a much wider spread: produce
(`bananas`), meat (`chicken breast`, `ground beef`), deli
(`deli ham` -- see below), seafood (`salmon fillet`), alcohol-adjacent
(`wine` -- this store's results were all dealcoholized/non-alcoholic;
real alcohol may not be searchable through this API/banner at all, not
independently confirmed either way), household goods (`paper towel`),
baby items (`diapers`), and frozen (`ice cream`) -- 9 queries, 30+ real
products, zero crashes, every core field (code/name/price) populated.

One real, structural difference found: items sold **by weight** (some
deli/meat/produce) use a `_KG` product code suffix instead of `_EA`, and
`packageSize` comes back as an empty string rather than null or a fixed
size (there's no fixed package when you're charged per kg) -- everything
else in the shape (`unit_price`, `barcode`, images, badges) held up
identically to the by-each case. See
`_simplify_product`'s test for `test_simplify_product_handles_weighted_item`
in `tests/test_simplifiers.py`.

## Time slots endpoint now returns a maintenance page, not JSON

`get_time_slots` was already the least-verified call in this client (see
"Research summary" above -- it's not even a pcx-bff endpoint). Reported
live: `get_available_slots` started crashing with a raw
`json.JSONDecodeError` instead of a normal tool error. Confirmed directly
(multiple independent calls, not a one-off): the endpoint now returns
HTTP 200 with an HTML "Site Under Maintenance" page instead of JSON, for
every store tried. Whether this is temporary or the endpoint has been
retired outright isn't known -- either way, the fix here is defensive,
not a workaround for the underlying endpoint: `get_time_slots` now
catches the non-JSON response and raises a normal `PcxApiError` (which
`get_available_slots` already knew how to turn into a clean `{"error":
...}` result), so a real outage/retirement on Loblaw's side surfaces as a
clear error instead of an unhandled crash.

## Product detail endpoint (confirmed non-functional)

`api_client.get_product(product_code)` -- a single-product-detail GET,
distinct from search -- returns HTTP 400 against every URL/query-param
combination tried: the bare code, the code with its `_EA` suffix stripped,
`storeId`/`banner`/`lang`/`cartId` query params in every combination tried,
a `/details` path suffix, and a POST variant mirroring search's body shape.
Not wired to any tool. If nutrition facts or an ingredients list exist
anywhere in this API, they're most likely behind whatever this endpoint
actually is -- `ingredients` is null on every real `search_products` result
checked (dozens, across multiple queries). Finding the real shape would
need a legitimate PC Express web/app session's browser Network tab, not
blind guessing against plausible-looking URLs.

## Search filters/sort (facets are real, applying them is not confirmed)

`search_products`'s raw response carries real, populated facet data --
`filterGroups` (aisle/category, brand, deals, price ranges, sellers, "Shop
Canadian", flyers, and a dynamic "Dietary & Lifestyle" group covering
things like gluten-free/organic/dairy-free/kosher, each with a real item
`count`) and `sorts` (relevance, price asc/desc, A-Z, newest). This is
real display data for a filter sidebar -- not fabricated -- but it answers
"what filters exist," not "how do I apply one."

**Applying a filter or sort could not be confirmed working**, despite a
good-faith effort: roughly a dozen plausible request-body shapes were
tried against `products/search` for both sort (`sort: "price-asc"` (400),
`sort: {code: "price-asc"}`, `sortCode`, a full echoed-back `sorts` array
with one entry's `selected` flipped to `true`) and brand filtering
(`filters` as a list-of-objects, filters as a dict, `selectedFilters`,
`facets`, `filterGroups` echoed back with a value selected, a bare
`brand`/`productBrand` key). Every attempt either 400'd or was silently
accepted but had **zero effect on the actual results** -- verified by
comparing real result order/brands across calls with and without the
param, not just checking for a non-error response. `totalResults` itself
fluctuates a few percent run-to-run from the same unfiltered query (see
"Search pagination" above), which makes it easy to mistake for a filter
working if you only check the count and not the actual result identities.

One real lead, not followed up: each `filterGroups[].values[]` entry for
the `category` facet carries a `url` (e.g. `/food/c/27985`) that looks like
a website *browse page* path, not a search-request parameter -- suggesting
category-filtered browsing might be a separate endpoint entirely (a
category-listing page, not `products/search` with a filter applied), which
would explain why no filter parameter on the search endpoint itself did
anything. Not investigated further.

Also checked and confirmed absent: individual products in `results[]` do
not carry per-item dietary/attribute tags anywhere in their own object (no
`dietaryCallouts`-shaped field on a product, checked across 15+ real
results) -- so there's no way to expose "this specific product is
gluten-free" from data already being fetched, either. The facet's per-value
`count` is the only place that information exists in this API, and it's
aggregate-only.

**Practical implication**: filtering/sorting has to happen client-side today
-- `search_products` already returns brand, price, unit price, and deal
info per result (see `_simplify_product`) across real, working offset
pagination, which is enough to filter/sort/rank results after the fact.
Nothing here should be read as "filters don't exist" -- the facet data is
real and detailed -- only as "the request-side mechanism to use them
wasn't found," which is a different and narrower claim.

### `search.json` -- a real endpoint, but a confirmed dead end

A user-supplied DevTools capture (anonymous, not-logged-in browser session)
showed a real request to a `search.json` path with `search-bar`/`storeId`/
`cartId`/`page` query parameters -- a plausible-looking lead for the
working pagination/filter mechanism above, on a different endpoint than
the one this project uses (`products/search`, POST, on `pcx-bff`).

Two direct guesses at the host (the banner's own public domain, matching
the pattern `get_time_slots` already uses for its own public endpoint)
came back 404 and an unrelated maintenance page -- not itself conclusive,
since the real host still wasn't confirmed. What settled it: the prior-art
project's own `API_REFERENCE.md` documents having found and then abandoned
this exact path:

> "An earlier build scraped the website Next.js route
> (`/_next/data/{buildId}/en/search.json`); that broke when the site
> stopped exposing `buildId` in page HTML, so search was moved to this
> token-authenticated pcx-bff endpoint."

`search.json` is a Next.js framework-internal data route, not a designed
API -- the `{buildId}` segment is a deployment-specific build identifier
that changes every time PC Express redeploys their frontend, isn't
reliably discoverable without scraping the currently-served page HTML/JS
for it, and the site stopped even exposing it there at some point. Whether
`page` genuinely paginates *this* route is beside the point: it's exactly
the fragile path the prior-art project already built, watched break, and
deliberately moved away from -- in favor of `products/search`, the same
endpoint this project already uses. Confirms the current approach is the
right one rather than something still worth chasing; not investigated
further.

## Nutrition enrichment (Open Food Facts)

PC Express's own API has no nutrition facts or ingredients anywhere (see
"Product detail endpoint" above). [Open Food Facts](https://openfoodfacts.org)
(ODbL-licensed, community-maintained, free) is a real, separate,
barcode-indexed database that turned out to work well for this -- but
getting there required finding and working around a real bug in PC
Express's own data first.

**First attempt: 0% hit rate, looked like a coverage gap, wasn't one.** 40
real barcodes from `search_products`' `upcs` field were checked against
Open Food Facts directly, spanning both private-label items (milk, cheese)
*and* major global brands also sold at this store (Coca-Cola, Doritos,
Kraft Dinner, General Mills). Properly paced to rule out rate-limiting as
the cause: 0/40 found. A known-good sanity-check barcode (Nutella) resolved
correctly through the exact same code path, confirming the lookup
mechanism itself worked -- the gap was specific to PC Express's barcodes.

**Root cause, found by diffing a real product against Open Food Facts'
own text search.** Searching Open Food Facts for "Lucky Charms" (a product
also present in a PC Express search result) surfaced its real code:
`0065633132122` (13-digit EAN). PC Express's own `upcs` field for the same
product: `06563313212` (11 digits). Diffing character-by-character: PC
Express's value is Open Food Facts' real code with **both the leading `0`
and the trailing check digit stripped**. Checksum validation independently
supports this -- treating PC Express's raw 11-digit values as complete,
checksummed barcodes, only ~10% pass any standard UPC-A/EAN-13 checksum;
after reconstructing (prepend `0`, compute and append the correct EAN-13
check digit), a live re-test of 15 real barcodes hit **15/15 (100%)**.
Implemented in `nutrition_client.normalize_barcode`.

Also found along the way: some of PC Express's `upcs` values are 7 digits,
not 11 -- these are internal PLU-style codes for items sold by weight
(fresh meat/deli/produce, e.g. `PC Blue Menu` chicken breast), which
genuinely have no manufacturer barcode to look up. Not a bug; `normalize_barcode`
rejects these on purpose rather than guessing.

**Rate limits, per Open Food Facts' own documentation**
(https://openfoodfacts.github.io/openfoodfacts-server/api/), not just
this project's own testing: **15 requests/minute/IP** for product read
queries, **10/minute/IP** for search queries, with an explicit warning
against anything resembling search-as-you-type, and a recommendation to
download their CSV/JSONL data export and self-host for any bulk/many-
products use case rather than repeated API calls. This project's own
empirical testing (rapid-fire requests with minimal spacing) triggered
HTTP 429 within roughly 10 requests, consistent with that documented
limit. `get_nutrition_info` is deliberately single-barcode, on-demand,
no batching -- see its docstring in `server.py`.

A staging mirror exists at `https://world.openfoodfacts.net` (HTTP basic
auth `off`/`off`) for development/testing without touching the production
rate limit -- this project's own investigation above was run against
production and burned real rate-limit budget doing so; worth using
staging for any further work on this integration.

**Checked, and there is no way to get a higher rate limit** -- not by
contributing data, creating an account, authenticating requests, or any
API-key/tier system. Confirmed across their own docs, a GitHub issue where
a maintainer discussed designing the rate-limit policy (proposing a flat
~30 req/min "fair use" baseline, no tiers), and a forum thread where a
maintainer's actual advice for a caching/high-volume use case was "use our
JSONL export... so you don't have to query the API with many requests (and
get rate-limited)" -- not "contribute first" or "authenticate first." The
only documented path to any accommodation is emailing
`reuse@openfoodfacts.org` to discuss a legitimate high-volume use case, or
their [API usage form](https://docs.google.com/forms/d/e/1FAIpQLSdIE3D8qvjC_zRJw1W8OmuHhsWJ_NSckiiniAHlfaVwUZCziQ/viewform)
-- neither guaranteed, and not relevant at this project's actual usage
pattern (occasional, single-user, on-demand lookups, well under 15/min in
normal use).

**Yuka-style signals: what's real vs. what's deliberately not attempted.**
Prompted by a direct comparison against Yuka's own product screens (a
consumer app built on this same Open Food Facts data): most of what Yuka
shows has a real, structured OFF equivalent, requested and surfaced in
`_simplify_nutrition` --

- `ingredients_analysis_tags` -- real per-product tags like
  `en:non-vegan`/`en:maybe-vegetarian`/`en:palm-oil-free`, confirmed live
  against two real products showing tags from all three categories
  (vegan/vegetarian/palm oil). Mirrors Yuka's vegetarian/vegan/palm-oil-free
  preference toggles directly. **A real bug was caught here before
  shipping**: an early version derived each category's "yes" tag name from
  its key generically (assuming a `<category>-free` pattern), which broke
  specifically for palm oil -- `en:palm-oil` (contains it) shares a prefix
  with `en:palm-oil-free` and would have been silently misread as the
  free case. Fixed with an explicit tag->status table instead of a clever
  generic derivation; see `_dietary_flags`'s docstring and
  `test_dietary_flags_palm_oil_contains_is_not_confused_with_palm_oil_free`.
- `nutrient_levels` -- real, OFF-computed low/moderate/high per fat/
  saturated fat/sugars/salt (confirmed live: `{"fat": "low", "salt":
  "low", ...}` for a real product). This is the same style of qualitative
  "Low impact" labelling Yuka shows per nutrient -- not derived or
  guessed here, passed through from OFF's own computation.
- `allergens_tags`/`traces_tags` -- real, structured (vs. the free-text
  `allergens` string already used) -- used to derive a `contains`/
  `may_contain`/`not_declared` status for 4 of the 14 EU-regulated
  allergens OFF tracks (gluten, milk, soy, sulfites). "Milk" is used as
  the practical stand-in for a lactose check -- OFF tracks the milk
  allergen (any milk protein), not lactose specifically, so this is a
  reasonable but imperfect proxy (confirmed live: this account's own real
  milk product carries the milk allergen tag regardless of fat %).
- `additives_tags`/`additives_n` -- real E-number list/count (confirmed
  live against a flavoured creamer with 4 additives: `en:e306`, `en:e412`,
  `en:e418`, `en:e500`, `en:e500ii`). **No per-additive risk
  classification** is attached, unlike Yuka's "additives with limited
  risk" style labels -- checked specifically (a web search plus OFF's own
  docs) and found no evidence OFF's API exposes a risk field; that
  labelling is most likely Yuka's own separate, proprietary analysis
  layered on top of the same raw additive list this project also has
  access to.

**Deliberately not attempted**: a single 0-100 "score" like Yuka's
(`55/100 Good` in the screenshot that prompted this). That number is
Yuka's own proprietary weighting of Nutri-Score, additives, organic
status, etc. -- not something Open Food Facts' API provides, and
reverse-engineering a consumer app's scoring formula from a couple of
screenshots isn't something this project is going to guess at. The real
components that would feed a judgment like that (grades, nutrient levels,
additives, dietary flags) are all exposed individually instead, on the
view that an LLM caller reasoning over labelled, structured signals in
natural language is more transparent than trusting an opaque number
anyway. **Also not attempted**: a "pork-free" check -- pork isn't one of
the 14 EU-regulated allergens OFF's taxonomy tracks, so there's no
reliable structured signal for it the way there is for gluten/milk/soy/
sulfites; guessing from free-text ingredients would risk a false negative
on something that matters for religious/dietary reasons, not just
preference, so it's left out entirely rather than offered unreliably.

**Dependency choice**: calls the plain REST API directly via `httpx`
rather than adding the `openfoodfacts` PyPI package. That package pulls in
`requests` (redundant alongside the `httpx` this project already uses
everywhere) and `tqdm` (a progress bar, irrelevant here) for what this
project needs, which is one GET request with a `fields` filter. See
`nutrition_client.py`'s module docstring.

## Cart is bound to a single store (real platform constraint, not a bug)

Reported by a real user: `add_to_cart` started failing with a raw
`SELLER_ID_MISMATCH` error and no clear explanation, described (accurately,
as it turned out) as looking like "the same corrupted cart from earlier."
The actual cause, confirmed live against the real account: **a PC Express
account has exactly one active cart at a time, account-wide, bound to
whichever store it was last used at** -- not a bug, not corruption, and
not specific to this project. This surfaces most for accounts shared
across locations (the reporting user's case: themselves ordering from one
store, a family member from another, on the same login).

**Confirmed via the account's real state**: `get_profile()`'s `cartId` and
`lastStoreId` fields, and `get_cart()`'s `orders[].fulfillment.courier.storeId`,
all pointed at one store (2841) even though the cart itself was completely
empty (0 entries) -- the store binding is set independently of cart
contents, and persists on an otherwise-empty cart. Attempting
`update_cart_entries` with a different store's `sellerId` produced a real,
structured 400 error:

```json
{"errors":[{"message":"{\"error_response\":{\"message\":\"The seller_id provided in the
request does not match the seller_id associated with the cart's seller_cart\",
\"details\":{\"expected\":\"2841\",\"provided\":\"1024\"},\"error_code\":\"SELLER_ID_MISMATCH\"}}",
"subjectType":"PLATFORM_CART", ...}]}
```

(Note the double encoding -- the real, structured error is a JSON *string*
nested inside `errors[0].message`, not a plain nested object. Parsed by
`PcxApiError.pcx_error` in `api_client.py`, which looks like a general
pcx-bff convention worth having even though only this one error has been
confirmed to use it so far.)

**First investigation concluded no API-level fix existed for re-pinning an
existing cart to a different store -- this was wrong, corrected below.**
That first pass tried: `DELETE /carts/{id}` (405, not a supported method),
`POST /carts` with various bodies including an explicit `storeId` (200,
but idempotently returns the *same* existing cart every time --
get-or-create semantics, not create-new), including a `fulfillment`
override alongside `entries` in the same update call (still rejected with
the identical SELLER_ID_MISMATCH), `PUT /carts/{id}` (405), the cart
heartbeat endpoint (just confirms liveness, resets nothing), and a
`sellerId` query parameter on the update call (500, not a recognized
parameter). `list_carts` also confirmed there is genuinely only one cart
per account, not one per store -- switching stores isn't a matter of
picking a different existing cart. At the time, the practical guidance
was "empty the cart in the app first" -- honest about the limitation, but
wrong that no API fix existed at all; the actual working shape (below)
just hadn't been tried yet.

### The real fix: `switch_cart_store` (found from a user-supplied real capture)

A user supplied six real `curl` captures from their own logged-in browser
session (not a DevTools guess -- an actual browsing session that happened
to move their account's cart from store 2841 to store 1024), including
two calls against `/carts/{id}/dry-run` and `/carts/{id}` with a body
shape never tried in the first investigation: a *bare* `courier` key, no
`entries` at all --

```json
{"courier": {"deliveryAddress": {"postalCode": "A1A 1A1"},
 "fulfillmentLocationId": "1024PCXD"}, "fulfillmentType": "COURIER"}
```

Tested live against the real account to confirm this wasn't just
incidental to that user's own session state:

1. **`/carts/{id}/dry-run` genuinely previews without persisting.**
   Called with this body targeting store 2841 (while the real cart was
   bound to 1024) -- response showed `storeId: 2841`, but a fresh
   `GET /carts/{id}` immediately after still showed `1024`, confirming
   nothing was written.
2. **`/carts/{id}` (no `/dry-run`) with the identical body *does*
   persist.** Same call without the `/dry-run` suffix -- response and a
   follow-up `GET` both showed `storeId: 2841`. Confirmed working in both
   directions (switched live: 1024 -> 2841, then back to 1024 to restore
   the account's original state).
3. **Existing entries carry over, not dropped.** The one real item in the
   cart at the time (bananas) was present after every switch, with a
   fresh `creationTime` matching the switch's timestamp each time --
   consistent with the item being re-priced/re-validated against the new
   store's catalog, not just relabelled.
4. **A fresh `add_to_cart` after switching works with no
   SELLER_ID_MISMATCH.** Added a different real item (`sellerId` matching
   the *new* store) after switching to 2841 -- succeeded cleanly, then
   removed it. This is the part that actually matters: the switch isn't
   cosmetic, the cart is genuinely re-pinned and writable for the new
   store afterwards.

**Finding the right `fulfillmentLocationId` for an arbitrary store**:
that id (`"1024PCXD"`) is not simply `{store_id}` with a fixed suffix
across banners -- the same captures' `/v1/delivery/serviceability` call
(a *separate*, unauthenticated endpoint, confirmed live to need no
`Authorization` header at all) showed superstore/fortinos/loblaw using a
`PCXD` suffix but nofrills using `PCXPD` for the same kind of hub, for a
single postal code query. Rather than hardcode a guessed per-banner
suffix table from one data point, `switch_cart_store` calls this
serviceability endpoint with the caller's own `postal_code` and picks the
matching location id for the active banner + `store_id` out of the real
response -- see `get_delivery_serviceability`/`_find_fulfillment_location_id`.

**Also found along the way: `profile.lastStoreId` can be stale relative
to the cart's real binding.** After the live store-switch above, the
account's `get_profile()` still reported `lastStoreId: "2841"` even though
the cart's own `orders[0].fulfillment.courier.storeId` correctly showed
the new value -- the two fields track different things and aren't
guaranteed to agree. `set_active_store`'s proactive mismatch warning
(`cart_note`) originally compared against `lastStoreId`; fixed to check
the cart's own real `courier.storeId` instead (`_cart_bound_store` in
`server.py`), which is also the field SELLER_ID_MISMATCH itself is keyed
to.

**What this means practically**: `switch_cart_store(store_id, postal_code)`
re-binds the account's cart directly -- no PC Express app step needed.
`set_active_store` still proactively warns (`cart_note`) when the
newly-selected store doesn't match the cart's real current binding, and
any cart write that still hits a live mismatch returns a
`cart_store_mismatch` error that now names `switch_cart_store` as the fix
instead of pointing at the app -- see `_tool_error` and
`_cart_store_mismatch_note` in `server.py`.

### A promising-looking lead that turned out not to help: anonymous carts

A user-supplied real `curl` capture (from browsing a banner site
*without* logging in) showed that `POST /pcx-bff/api/v1/carts` works with
**no `Authorization` header at all**, given just `{"bannerId", "language",
"storeId"}`. Worth investigating directly: unlike the authenticated
version of this same call (get-or-create, always returns the *existing*
account cart regardless of `storeId` -- see above), this genuinely
**creates a fresh cart scoped to whatever store you ask for**, with
`customer: {"pcid": "", "name": "Anonymous", "email": "Anonymous"}`.

That looked like a real path to a fix: create an anonymous cart for the
*new* store, then somehow get it associated with the authenticated
account. Tested live, three things confirmed, in order:

1. **The anonymous cart is genuinely writable using the authenticated
   account's own Bearer token** -- `update_cart_entries` against the
   anonymous cart's ID succeeded, real item added, no ownership check
   rejected it. (Notable on its own: cart read/write doesn't appear to
   verify the token's customer matches the cart's `customer` -- a
   pcx-bff/PC Express platform behavior, not something this project
   causes, and not investigated further since it's outside this
   project's scope.)
2. **But the account never adopts it.** After the write, `get_profile()`'s
   `cartId` and `list_carts` still only showed the *original*,
   2841-bound cart -- the anonymous cart doesn't appear anywhere the
   account can see it.
3. **No merge/claim mechanism found.** Tried passing the anonymous cart's
   ID back into an authenticated `POST /carts` call under three plausible
   field names (`anonymousCartId`, `cartId`, `mergeCartId`) -- all three
   returned `200`, and all three silently ignored the field, returning
   the same pre-existing account cart every time.

**Conclusion: a real, interesting platform behavior, but not a usable
fix.** An anonymously-created cart can technically be written to with a
real account's token, but it stays invisible to that account everywhere
that matters (`profile.cartId`, `list_carts`, and near-certainly the
app/website's own cart view) -- items added to it almost certainly
couldn't be checked out as a real order tied to the account's payment
method or PC Optimum points. This lead didn't pan out, but a *different*
one supplied shortly after did -- see "The real fix: `switch_cart_store`"
above, which re-binds the account's *existing* cart directly instead of
trying to smuggle a second one in.

### Removing/zeroing a cart entry also requires sellerId (a real bug, not a platform quirk)

Reported live: `remove_from_cart`/`update_quantity(0)` started throwing
`cart_store_mismatch` with `requested_store: null` against a real,
correctly-configured cart. Root cause was in this project, not PC
Express: those two tools sent a bare `{"quantity": 0}`, but PC Express's
API rejects that with `SELLER_ID_MISMATCH` (`expected`: the cart's real
store, `provided`: null) unless `sellerId`/`fulfillmentMethod` are
included, confirmed live -- the exact same requirement `add_to_cart`
already handled correctly for additions. A fix for this exact shape had
already been found once, ad hoc, while cleaning up test data during the
`switch_cart_store` investigation above, but was never carried back into
the actual `remove_from_cart`/`update_quantity` tool code until this
report. Fixed by always including `sellerId`, sourced from the cart's own
real current binding (`_cart_bound_store`) rather than the locally cached
`session.store_id` -- those two can disagree (e.g. after
`switch_cart_store` without also calling `set_active_store`), and only
the cart's own live value is guaranteed to pass validation. See
`_seller_id_for_removal` in `server.py`.

## Loyalty offers (no dedicated endpoint found)

Two real, working, account-level loyalty endpoints exist and are wired to
`get_loyalty_status`:

- `ecommerce/v2/{banner}/customers` (the same profile endpoint
  `get_profile()` already used) -- `pcOptimum.points.balance`/
  `dollarsRedeemable`/`dollarsRedeemedLifetime` are real, populated,
  live-verified account data.
- `ecommerce/v2/{banner}/customers/promotions` -- returns PC Optimum
  stamp-card program status. Confirmed working, but only in the *inactive*
  shape on the account this was built against (`stampCards.isActive:
  false`) -- the `balance`/`rewards` field names for an active stamp card
  are carried through as-is, not independently verified against a
  populated example.

**What does NOT exist, as far as could be found**: a dedicated "browse or
clip available personalized offers/coupons" endpoint, the thing most
loyalty-program apps have as an "Offers" tab (load a coupon to your card
before it applies at checkout). A dozen plausible URL patterns were tried
against pcx-bff and all 404'd: `customers/offers`, `customers/rewards`,
`customers/deals`, `customers/clipped-offers`, `customers/coupons`,
`customers/personalized-offers`, `customers/optimum-offers`,
`customers/wallet`, `customers/wallet/offers`, a bannerless `/offers`,
`/loyalty/offers`, and `/loyalty/v1/offers`. If this feature exists in the
API at all, it's under a URL pattern none of those guesses landed on, or
lives on a different host than pcx-bff entirely. The closest thing this
project actually has to per-item offers is what `search_products` already
surfaces per result (`deal_text`/`loyalty_points`, sourced from each
product's own `badges`/`promotions` fields) -- real, working, but scoped to
one product at a time via search, not a browsable list of everything
currently available to the account.
