# Raw response snapshots

Real (anonymized) captures of this project's own account's API responses,
one file per endpoint (`get_cart`, `get_profile`, `get_customer_promotions`,
`search_products`, `get_pickup_location`, `get_historical_orders`,
`get_historical_order`, `get_delivery_slots`, `get_checkout`,
`openfoodfacts_product`, `book_delivery_slot`). `book_delivery_slot` is the
exception to "captured by the script": booking changes the real cart, so
it's hand-trimmed from a user-supplied web checkout capture instead. Regenerate with
`python scripts/capture_snapshots.py` against your own logged-in account --
**read that script's docstring and manually review every file before
committing a refresh**; a first pass at automated redaction here genuinely
shipped a real name and email before a manual review caught it.
`openfoodfacts_product` is the one exception with no redaction step --
it's generic product/nutrition data from a wholly separate, non-personal
public database (Open Food Facts), not account data.

Used by `tests/test_snapshot_shapes.py` as a regression guard: these are
real structures the `_simplify_*` functions in `server.py` have to keep
handling correctly, not hand-written assumptions about what the shape
*should* be (see that test file's docstring for why that distinction
mattered here -- `_simplify_cart` shipped broken against exactly this kind
of gap once already).

If a snapshot goes stale (Loblaw changes a response shape) and a
`_simplify_*` function starts silently returning nulls again, that's
exactly the failure mode these tests exist to catch early instead of via a
user bug report.
