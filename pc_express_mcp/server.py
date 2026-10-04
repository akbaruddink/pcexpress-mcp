"""MCP server exposing PC Express grocery ordering as tools.

Design notes:

- `place_order` never submits payment. There is no known/published PC
  Express payment API, and even if there were, we deliberately don't
  automate spending money. It validates the cart, requires an explicit
  `confirm=True`, and hands you a checkout URL to finish on your phone app
  or in a browser -- per the user's stated preference for this build.
- Every tool catches PcidAuthError / PcxApiError and returns a structured
  `{"error": ...}` dict instead of a raw traceback, so a re-auth requirement
  reads as a clear instruction ("run scripts/login.py") rather than a stack
  trace the MCP client has to interpret.
- Stdio mode's state (active banner/store/cart) lives in session_state.json;
  tokens live separately in auth_state.json. Neither is committed to git
  (see .gitignore) and neither is ever logged. HTTP mode has no on-disk
  state at all: each tenant's PC Express credentials live only inside the
  encrypted OAuth token they're holding (see token_crypto.py/oauth_server.py),
  and their session/cart state lives in the in-memory _session_states dict
  below -- see _load_session()/_save_session().
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import ToolAnnotations

from . import config, nutrition_client, session_state
from .api_client import PCExpressAPI, PcxApiError
from .auth import EphemeralTokenManager, PcidAuthError, TokenManager

# MCPServer is the current (mcp>=2.0) home of what used to be FastMCP in
# older SDK releases -- same @mcp.tool()/.run()/.streamable_http_app()
# surface this module uses, just moved and renamed. Confirmed by actually
# installing the SDK with uv and inspecting it, not assumed from memory.

# MCP Apps (io.modelcontextprotocol/ui): interactive_product_search's
# widget. See "Interactive product search widget" in docs/RESEARCH.md for
# why the client-side JS is a separately built asset (web/product-search-
# widget/), not inline Python -- MCP App resources must be a single
# self-contained HTML document, bundled once with esbuild and committed,
# not built at server runtime (this project has no Node dependency
# otherwise and shouldn't gain one just to serve one static file).
apps = Apps()

_WIDGET_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web" / "product-search-widget" / "dist" / "widget.html"
)
_INTERACTIVE_SEARCH_RESOURCE_URI = "ui://pc-express/product-search.html"

try:
    _widget_html = _WIDGET_HTML_PATH.read_text(encoding="utf-8")
except FileNotFoundError as exc:
    raise RuntimeError(
        f"{_WIDGET_HTML_PATH} is missing -- build it first: "
        "cd web/product-search-widget && npm install && npm run build"
    ) from exc

apps.add_html_resource(
    _INTERACTIVE_SEARCH_RESOURCE_URI,
    _widget_html,
    name="pc-express-product-search",
    title="PC Express Product Search",
    # Product photos are served from digital.loblaws.ca (confirmed live
    # throughout this project's search/cart results) -- the MCP Apps
    # sandbox blocks external images by default unless declared here.
    csp=ResourceCsp(resource_domains=["https://digital.loblaws.ca"]),
)

# NOTE: these two @apps.tool()-decorated functions must be defined here,
# before MCPServer(...) is constructed below -- unlike @mcp.tool(), which
# registers incrementally on the already-existing `mcp` object wherever it
# appears in this file, Extension.tools() is only consumed once, inside
# MCPServer.__init__ (see mcp/server/mcpserver/server.py's
# _apply_extension). A first attempt defined these near search_products
# (much later in the file, after mcp = MCPServer(...)) and both tools
# silently never registered -- caught by checking mcp.list_tools() showed
# 14, not 16, not any exception. The function *bodies* below can still
# reference _load_session/_get_api/_simplify_product/_tool_error even
# though those aren't defined until later in this file -- Python resolves
# names inside a function body at call time, long after the whole module
# has finished importing, not at def time.
def _lookup_products_by_code(api: PCExpressAPI, store_id: str, product_codes: list[str]) -> tuple[list[dict], list[str]]:
    """Resolve specific product codes to their real, current data.

    No dedicated lookup-by-code endpoint exists (`api_client.get_product`
    is confirmed broken -- see its docstring); confirmed live instead that
    searching with the exact code as the search term reliably returns
    that product as an exact match (3/3 real codes tried, each returning
    exactly one result matching the requested code) -- an invalid code
    doesn't error, it just returns unrelated fuzzy-matched results, so
    each result is checked for an exact code match rather than trusting
    "first result." One search call per code. Returns (products,
    not_found_codes).

    Not itself an @apps.tool() -- a plain helper shared by
    interactive_product_search and _interactive_search_results below. It
    must stay defined *before* either of those, not just before they're
    called: a first version of this placed it between the @apps.tool(...)
    decorator and interactive_product_search's `def`, which silently
    rebound that decorator onto this helper instead (Python decorators
    bind to the very next `def`, regardless of intent) -- caught by
    MCPServer failing to even construct (a JSON-schema error on this
    function's `api: PCExpressAPI` parameter), not a subtle bug.
    """
    products: list[dict] = []
    not_found: list[str] = []
    for code in product_codes:
        raw = api.search_products(code, store_id, size=5)
        match = next((p for p in raw.get("results", []) or [] if p.get("code") == code), None)
        if match:
            products.append(_simplify_product(match))
        else:
            not_found.append(code)
    return products, not_found


@apps.tool(
    resource_uri=_INTERACTIVE_SEARCH_RESOURCE_URI,
    annotations=ToolAnnotations(
        title="Interactive Product Search", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
def interactive_product_search(
    query: Optional[str] = None,
    product_codes: Optional[list[str]] = None,
    size: int = 20,
    ctx: Optional[Context] = None,
) -> dict:
    """Search products, or show a specific hand-picked list of products, as
    an interactive widget (photos, prices, per-item Add-to-Cart buttons) on
    a client that renders MCP Apps UI -- instead of plain text.

    For more relevant results, call get_purchase_history once near the
    start of a session (not before every search) to learn what this
    household actually buys, then use that context when choosing a
    query or picking products to recommend.

    Provide exactly one of:
    - `query`: a normal search, same as search_products.
    - `product_codes`: a curated list of specific products you already
      know about (e.g. picked from earlier search_products/
      interactive_product_search results after comparing price,
      nutrition, or anything else) -- this is how you show the user
      exactly what *you're* recommending, not just "here's a search."
      This tool can't take full product details as an argument for this
      -- only codes, which it looks up itself -- because accepting full
      product data here would defeat the whole reason the widget's data
      stays out of your context on a supporting client (see below): it
      would all have to pass through your own output to get here.

    Requires an active store (see set_active_store). Confirmed live,
    including the full Add-to-Cart round trip: MCP Apps widgets render on
    Claude Desktop, claude.ai web, and the Claude mobile app -- see
    docs/RESEARCH.md "Interactive product search widget". On a client
    that doesn't support MCP Apps, this degrades automatically to the
    same full text/photo_markdown results search_products would give
    (for `product_codes`, the same shape but sourced by code lookup
    instead of a search term), so it's always safe to call regardless of
    client -- use this instead of search_products whenever the user is
    meant to browse and pick items, not just get information about them.

    On a client that DOES support MCP Apps, this returns only a small
    reference, not the actual results -- by design, to keep the widget's
    data out of your context. That small response is the *correct*
    outcome for a supporting client, not a sign the widget failed to
    render. Whether it actually rendered is not something you can see
    from here: the widget renders client-side, in the user's own app,
    and you never receive its visual output, only this JSON. Do not tell
    the user it did or didn't render -- if that's in question, ask them
    what's on their screen.
    """
    if not query and not product_codes:
        return {"error": "invalid_input", "message": "Provide either query or product_codes."}
    if query and product_codes:
        return {"error": "invalid_input", "message": "Provide only one of query or product_codes, not both."}
    session = _load_session()
    if not session.store_id:
        return {"error": "no_active_store", "message": "Call set_active_store first."}
    api = _get_api(session.banner)

    not_found: list[str] = []
    if product_codes:
        try:
            results, not_found = _lookup_products_by_code(api, session.store_id, product_codes)
        except (PcidAuthError, PcxApiError) as exc:
            return _tool_error(exc)
        label = f"{len(product_codes)} selected product{'' if len(product_codes) == 1 else 's'}"
    else:
        try:
            raw = api.search_products(query, session.store_id, cart_id=session.cart_id, size=size)
        except (PcidAuthError, PcxApiError) as exc:
            return _tool_error(exc)
        results = [_simplify_product(p) for p in raw.get("results", []) or []]
        label = f'Results for "{query}"'

    if ctx is not None and client_supports_apps(ctx):
        response: dict[str, Any] = {
            "label": label,
            "size": size,
            "store_id": session.store_id,
            "banner": session.banner,
            "count": len(results),
            "note": (
                "This client negotiated MCP Apps support, so the widget was requested for these "
                f"{len(results)} result(s) -- getting back this small reference instead of the full "
                "results list is the expected, correct behavior for a supporting client, not a sign "
                "the widget failed. Whether it actually rendered isn't visible from here: the model "
                "never sees the client's UI, only this JSON. Don't claim it did or didn't render -- "
                "if that matters, ask the user what they see."
            ),
        }
        response["query"] = query if query else None
        response["product_codes"] = product_codes if product_codes else None
        if not_found:
            response["not_found"] = not_found
        return response

    response = {"label": label, "count": len(results), "results": results}
    response["query"] = query if query else None
    if not_found:
        response["not_found"] = not_found
    return response


@apps.tool(
    resource_uri=_INTERACTIVE_SEARCH_RESOURCE_URI,
    visibility=["app"],
)
def _interactive_search_results(
    size: int,
    store_id: str,
    banner: str,
    query: Optional[str] = None,
    product_codes: Optional[list[str]] = None,
) -> dict:
    """App-only: not in the model's tool list (visibility=["app"]) -- the
    interactive_product_search widget calls this itself, over the
    postMessage bridge, to fetch the actual product list for whatever the
    launcher tool ran (a search, or a specific product_codes lookup).

    Deliberately re-runs the search/lookup live, from the exact params
    the launcher used, rather than reading cached results back out by an
    opaque reference. An earlier version cached the *results* behind a
    random result_ref -- simpler at the call site, but that cache was an
    in-memory dict with no persistence, so a server restart between the
    search and the widget re-fetching it (confirmed live: this happens
    routinely during active development, but a crash or redeploy at any
    time has the same effect) silently invalidated it, and the widget had
    no way to recover except telling the user to search again. Re-running
    is cheap, safe (read-only), and idempotent enough that there's no
    real reason to cache it at all -- see docs/RESEARCH.md "Interactive
    product search widget".
    """
    api = _get_api(banner)
    try:
        if product_codes:
            results, not_found = _lookup_products_by_code(api, store_id, product_codes)
        else:
            raw = api.search_products(query or "", store_id, size=size)
            results = [_simplify_product(p) for p in raw.get("results", []) or []]
            not_found = []
    except (PcidAuthError, PcxApiError) as exc:
        error = _tool_error(exc)
        error["results"] = []
        return error
    response = {"results": results}
    if not_found:
        response["not_found"] = not_found
    return response


mcp = MCPServer("pc-express", extensions=[apps])

# Single global stdio-mode TokenManager (file-backed, cheap to re-read, one
# process = one user, no isolation concerns). HTTP-mode tenants deliberately
# get NO equivalent cache -- see _get_token_manager() below for why a fresh
# EphemeralTokenManager is built from the *current* request's token every
# time instead.
_stdio_token_manager: Optional[TokenManager] = None

# Non-sensitive session/cart cache for HTTP-mode tenants, in-memory only
# (never written to disk -- see module docstring). Losing an entry on
# restart just costs one extra API round-trip via _rediscover_cart; nothing
# here is a security boundary the way PC credentials are.
_session_states: dict[str, session_state.SessionState] = {}


def _current_tenant() -> Optional[str]:
    """The tenant (PC Express login email) the current request belongs to,
    in HTTP mode -- resolved via the MCP SDK's *native* per-request auth
    context (AuthenticationMiddleware -> AuthContextMiddleware ->
    get_access_token(), wired in by build_http_app() passing token_verifier=
    to mcp.streamable_http_app()). Not a hand-rolled contextvar -- see
    oauth_server.py's module docstring and tests/test_http_app.py for why
    that distinction mattered (verified this actually propagates through
    the SDK's internal task-group dispatch with a real request, not assumed).

    Returns None in stdio mode (no HTTP auth context exists at all -- the
    single global Desktop/Claude Code user shares config.AUTH_STATE_PATH/
    config.SESSION_STATE_PATH directly, same as before multi-tenancy).
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    return token.client_id if token else None


def _get_token_manager():
    """Returns something satisfying TokenManager's public interface
    (get_access_token/force_refresh) -- a cached file-backed TokenManager in
    stdio mode, or a fresh EphemeralTokenManager seeded by re-decrypting the
    *current* request's bearer token in HTTP mode.

    Deliberately not cached across requests for HTTP tenants: caching would
    mean a second, independent copy of PC credentials living in process
    memory that could silently diverge from what's actually inside the
    token Claude is holding right now (e.g. after Claude picks up a refreshed
    outer token from oauth_server.token_endpoint, which embeds a newly
    rotated PC refresh token -- a stale cached manager would keep trying the
    old, by-then-consumed one). Re-decrypting is cheap (local symmetric
    decryption, no network) so there's no real cost to always using the
    token that's actually present on this request.
    """
    tenant = _current_tenant()
    if tenant is None:
        global _stdio_token_manager
        if _stdio_token_manager is None:
            _stdio_token_manager = TokenManager()
        return _stdio_token_manager

    from mcp.server.auth.middleware.auth_context import get_access_token

    from . import oauth_server

    token = get_access_token()
    payload = oauth_server.decode_access_token(token.token) if token else None
    if not payload:
        # Shouldn't happen in practice -- RequireAuthMiddleware already
        # rejected anything MultiTenantTokenVerifier couldn't decode before
        # a tool ever runs -- but fail with a clear message rather than a
        # KeyError if it somehow does (e.g. a race with token expiry).
        raise PcidAuthError("Session expired or invalid. Reconnect the connector to get a fresh one.")
    return EphemeralTokenManager(
        access_token=payload["pc_access_token"],
        refresh_token=payload.get("pc_refresh_token"),
        expires_at=payload["pc_expires_at"],
    )


def _get_api(banner: str) -> PCExpressAPI:
    return PCExpressAPI(_get_token_manager(), banner)


def _load_session() -> session_state.SessionState:
    tenant = _current_tenant()
    if tenant is None:
        return session_state.load()
    return _session_states.setdefault(tenant, session_state.SessionState())


def _save_session(session: session_state.SessionState) -> None:
    tenant = _current_tenant()
    if tenant is None:
        session_state.save(session)
        return
    _session_states[tenant] = session


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PcidAuthError):
        return {"error": "auth_required", "message": str(exc)}
    if isinstance(exc, PcxApiError):
        pcx_error = exc.pcx_error
        if pcx_error and pcx_error.get("error_code") == "SELLER_ID_MISMATCH":
            # Real PC Express platform constraint, not a bug or a
            # "corrupted" cart: within a single banner, an account has
            # exactly one active cart at a time, bound to whichever store
            # it was last used at. Confirmed live this is scoped per
            # banner, not the whole account -- a real "one cart per
            # account, period" claim shipped here initially was wrong,
            # caught after a user's gut feeling and direct verification
            # (list_carts returns genuinely different, simultaneously
            # valid cart ids per banner; see docs/RESEARCH.md "Cart is
            # bound to a single store"). Matters most for accounts shared
            # across locations that also happen to use the same banner
            # (e.g. two family members both ordering from Superstore, at
            # different Superstore locations) -- different banners
            # (Superstore vs No Frills) don't conflict at all. There IS a
            # real, confirmed-live fix for the same-banner case, though --
            # switch_cart_store re-binds the existing cart to the store
            # this call actually needs.
            details = pcx_error.get("details") or {}
            expected, provided = details.get("expected"), details.get("provided")
            return {
                "error": "cart_store_mismatch",
                "message": (
                    f"Your PC Express account's active cart for this banner is currently bound to store "
                    f"{expected}, not store {provided}. PC Express only supports one active cart per banner "
                    "at a time -- this happens when this banner's cart was last used at a different store "
                    "(e.g. a family member ordering from another location on the same banner). Call "
                    f"switch_cart_store(store_id='{provided}', postal_code=...) to re-bind the cart to store "
                    f"{provided}, then retry."
                ),
                "cart_bound_to_store": expected,
                "requested_store": provided,
            }
        return {
            "error": "api_error",
            "status_code": exc.status_code,
            "message": str(exc),
            "body": exc.body,
        }
    return {"error": "unexpected", "message": str(exc)}


def _ensure_customer_and_cart(api: PCExpressAPI, session: session_state.SessionState) -> None:
    if session.customer_id and session.cart_id:
        return
    _rediscover_cart(api, session)


def _rediscover_cart(api: PCExpressAPI, session: session_state.SessionState) -> None:
    """Discover session.customer_id/cart_id fresh, scoped to session.banner.

    Uses `list_carts`, not `profile.cartId` -- confirmed live these
    disagree: `profile.cartId` returns the *same* cart id regardless of
    which banner's API client calls it (observed always returning the
    superstore cart, even when called with nofrills headers), while
    `list_carts` correctly returns each banner's own, genuinely different
    cart. An earlier version of this function used `profile.cartId`
    (the prior-art project's own approach, and `list_carts`'s docstring
    used to say as much) -- wrong for any banner switch, caught only by
    directly testing a real cross-banner scenario: after `set_active_store`
    switched the active banner, this kept resolving to the *previous*
    banner's cart id, which would have made the next add_to_cart write to
    the wrong cart under the new banner's headers instead of erroring
    loudly. See docs/RESEARCH.md "Cart discovery must be banner-scoped".
    """
    profile = api.get_profile()
    session.customer_id = profile.get("id") or profile.get("customerId") or session.customer_id
    session.cart_id = None
    if session.customer_id:
        carts = api.list_carts(session.customer_id).get("carts") or []
        if carts:
            session.cart_id = carts[0]["id"]
    _save_session(session)
    if not session.cart_id:
        raise PcxApiError(
            f"Could not discover a cart id for banner {session.banner!r} -- "
            "add an item via the PC Express app/website (on this banner) once to create a cart, then retry."
        )


def _get_cart_healing(api: PCExpressAPI, session: session_state.SessionState) -> dict:
    """GET the cart, self-healing once if it's expired server-side.

    Loblaw expires carts; a previously-good cart_id can start 404ing. On a
    404, re-discover the cart id from the customer profile and retry once
    before giving up -- mirrors the prior-art project's proven approach.
    """
    _ensure_customer_and_cart(api, session)
    try:
        return api.get_cart(session.cart_id)
    except PcxApiError as exc:
        if exc.status_code != 404:
            raise
        stale_id = session.cart_id
        _rediscover_cart(api, session)
        if session.cart_id == stale_id:
            raise
        return api.get_cart(session.cart_id)


def _update_cart_healing(
    api: PCExpressAPI, session: session_state.SessionState, entries: dict[str, dict[str, Any]]
) -> dict:
    """POST a cart update, self-healing once on a 404 (expired cart), then return the fresh cart."""
    _ensure_customer_and_cart(api, session)
    try:
        api.update_cart_entries(session.cart_id, entries)
    except PcxApiError as exc:
        if exc.status_code != 404:
            raise
        stale_id = session.cart_id
        _rediscover_cart(api, session)
        if session.cart_id == stale_id:
            raise
        api.update_cart_entries(session.cart_id, entries)
    return api.get_cart(session.cart_id)


def _seller_id_for_removal(api: PCExpressAPI, session: session_state.SessionState) -> Optional[str]:
    """sellerId to send when zeroing out a cart entry (remove_from_cart,
    update_quantity(0)). Confirmed live: PC Express's API rejects a bare
    `{"quantity": 0}` with SELLER_ID_MISMATCH (`expected`: the cart's real
    store, `provided`: null) if sellerId is omitted entirely -- a real bug
    in this project (a fix for this exact shape was already found once
    during ad hoc cleanup while investigating switch_cart_store, but never
    wired into remove_from_cart/update_quantity themselves; see
    docs/RESEARCH.md "Cart is bound to a single store").

    Uses the cart's own real current binding (`_cart_bound_store`), not
    the locally cached `session.store_id` -- those can disagree (e.g.
    after `switch_cart_store` without also calling `set_active_store`),
    and only the cart's own live value is guaranteed to pass validation.
    Falls back to `session.store_id` if the cart lookup itself fails or
    the cart has no fulfillment set yet; returns None (omit sellerId
    entirely, the old behavior) only if neither is available -- removal
    shouldn't be blocked by this best-effort lookup failing.
    """
    try:
        cart_store = _cart_bound_store(_get_cart_healing(api, session))
    except (PcidAuthError, PcxApiError):
        cart_store = None
    return cart_store or session.store_id


def _strip_html(text: Optional[str]) -> Optional[str]:
    """Some real product descriptions carry raw HTML markup (`<p>...</p>`,
    `†`-style footnote markers), some don't -- inconsistent across
    products, confirmed live. Strips tags only; doesn't attempt to resolve
    entities beyond the couple of common ones actually seen.
    """
    if not text:
        return text
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _markdown_image(name: Optional[str], url: Optional[str]) -> Optional[str]:
    """A ready-to-paste markdown image tag for a product photo, e.g.
    `![2% Milk](https://digital.loblaws.ca/...)`.

    This is deliberately plain chat-message markdown, not MCP's own
    `ImageContent` tool-result type -- confirmed that claude.ai/Desktop
    currently render `ImageContent` from a tool result collapsed inside a
    "tool use" accordion most people never open, so returning one there
    would not actually make photos visible. A markdown image *in a chat
    message Claude itself writes* renders inline on every real Claude
    surface (including mobile), so the fix is handing back copy-paste-
    ready markdown for tools to include directly in their own reply, not
    a new content type. See docs/RESEARCH.md "Product photos in chat".
    """
    if not url:
        return None
    alt = (name or "product").replace("[", "(").replace("]", ")")
    return f"![{alt}]({url})"


def _simplify_product(p: dict) -> dict:
    """Verified live against real search results (queries against a real
    store, real inventory). Two things worth knowing before relying on this
    for automated deal-hunting:

    - **No nutrition facts or ingredients list is available from this
      endpoint.** `ingredients` was null on every real result seen (dozens
      of products checked). The only per-product-detail endpoint this
      client has (`api_client.get_product`) returned HTTP 400 against
      every URL/param combination tried -- it's unverified/likely broken,
      not wired to any tool. If nutrition data matters, pair `barcode`
      below with an external source (e.g. Open Food Facts, which indexes
      by UPC) rather than expecting this API to provide it.
    - `unit_price` (from `prices.comparisonPrices[0]`) is what actually
      enables apples-to-apples value comparison across different package
      sizes -- e.g. two milks at different `price`s but comparable
      `unit_price.value` per 100mL. Prefer it over raw `price` for
      "which is the better deal" reasoning.

    `description` is truncated to ~400 chars (full marketing copy can run
    over 1500 chars per product; untruncated, that dominates a multi-result
    response with more prose than a model needs to evaluate a product).
    `image_urls` keeps one representative size *per distinct photo*
    (`imageAssets` entries are genuinely different angles/shots -- front,
    side, angled, sometimes a lifestyle or label close-up -- real products
    carry anywhere from 3 to 9+ of these) but drops each photo's other
    4-7 same-image size variants (thumbnail/small/medium/large/extraLarge/
    retina -- all the same picture, just resized). An earlier version of
    this returned only `imageAssets[0]`, a single URL, which silently
    dropped every other angle -- caught from a direct user question about
    why only one photo was coming back when the app clearly shows several.
    """
    prices = p.get("prices") or {}
    price = prices.get("price") or {}
    was_price = prices.get("wasPrice") or {}
    comparison_prices = prices.get("comparisonPrices") or []
    comparison = comparison_prices[0] if comparison_prices else {}
    images = p.get("imageAssets") or []
    image_urls = [
        url
        for img in images
        if (url := (img.get("mediumUrl") or img.get("largeUrl") or img.get("thumbnailUrl")))
    ]
    badges = p.get("badges") or {}
    loyalty_badge = badges.get("loyaltyBadge") or {}
    deal_badge = badges.get("dealBadge") or {}
    barcodes = p.get("upcs") or []

    description = _strip_html(p.get("description"))
    if description and len(description) > 400:
        description = description[:400].rsplit(" ", 1)[0] + "…"

    unit_price = None
    if comparison.get("value") is not None:
        unit_price = {
            "value": comparison.get("value"),
            "per": f"{comparison.get('quantity')}{comparison.get('unit')}" if comparison.get("unit") else None,
        }

    return {
        "code": p.get("code"),
        "sku": p.get("articleNumber"),
        "barcode": barcodes[0] if barcodes else None,
        "name": p.get("name"),
        "brand": p.get("brand"),
        "description": description,
        "package_size": p.get("packageSize"),
        "image_urls": image_urls,
        "photo_markdown": _markdown_image(p.get("name"), image_urls[0] if image_urls else None),
        "aisle": p.get("aisle"),
        "stock_status": p.get("stockStatus"),
        "price": price.get("value"),
        "regular_price": was_price.get("value"),
        "member_price": p.get("mopDealPrice"),
        "unit_price": unit_price,
        "deal_text": deal_badge.get("text"),
        "loyalty_points": loyalty_badge.get("points"),
    }


def _simplify_cart(cart: dict) -> dict:
    """Verified live against a real, non-empty cart (3 real items): entries
    do NOT have a top-level `product`/`code`/`totalPrice` the way an
    earlier, never-live-verified version of this function assumed (that
    version silently returned all-null items -- caught from a real user
    report, not anticipated). The real shape nests the product two levels
    down (`entry.offer.product`), the line-item code is `entry.offer.id`
    (equal to `product.id`, confirmed identical on every real entry seen),
    and the actual price paid is `entry.prices.totalSalePrice` (falling
    back to `totalRegularPrice` if unset) -- `entry.prices` also carries
    `comparisonPrices`/`ehfTotal`/deposit fields not surfaced here. Order
    totals live at `order.totals.totalPrice`/`subTotal`, not on the cart
    object itself; summed across orders in the (rare) multi-order case
    rather than assuming exactly one. Each entry's `photo_markdown` is
    built from `product.primaryImage` -- a single URL, unlike search
    results' multi-angle `image_urls` (the raw cart entry shape only ever
    carries one photo per product, confirmed against a real cart).

    Entries also carry `brand`/`package_size`/`unit_price`/`regular_price`/
    `deal_text` -- real fields confirmed present on a real cart entry
    (`product.brand`, `product.sizeLabel`, `prices.comparisonPrices`,
    `prices.totalRegularPrice`, `offer.badges.dealBadge`/
    `offer.promotionLabel`) that just weren't extracted before. One real
    shape gotcha caught here: a cart entry's `comparisonPrices` items key
    the number as `"price"` (e.g. `{"price": 0.36, "quantity": 100, "unit":
    "g"}`), not `"value"` like a search result's `comparisonPrices` does
    (`_simplify_product` above) -- same-looking field, different key name,
    confirmed against a real cart fixture rather than assumed identical to
    search's shape.
    """
    orders = cart.get("orders") or []
    entries: list[dict] = []
    total_price = 0.0
    have_total = False
    for order in orders:
        totals = order.get("totals") or {}
        if totals.get("totalPrice") is not None:
            total_price += totals["totalPrice"]
            have_total = True
        for entry in order.get("entries", []) or []:
            offer = entry.get("offer") or {}
            product = offer.get("product") or {}
            prices = entry.get("prices") or {}
            total_sale_price = prices.get("totalSalePrice")
            comparison_prices = prices.get("comparisonPrices") or []
            comparison = comparison_prices[0] if comparison_prices else {}
            unit_price = None
            if comparison.get("price") is not None:
                unit_price = {
                    "value": comparison.get("price"),
                    "per": f"{comparison.get('quantity')}{comparison.get('unit')}" if comparison.get("unit") else None,
                }
            deal_badge = (offer.get("badges") or {}).get("dealBadge") or {}
            entries.append(
                {
                    "code": offer.get("id") or product.get("id"),
                    "name": product.get("name"),
                    "brand": product.get("brand"),
                    "package_size": product.get("sizeLabel"),
                    "quantity": entry.get("quantity"),
                    "total_price": total_sale_price if total_sale_price is not None else prices.get("totalRegularPrice"),
                    "regular_price": prices.get("totalRegularPrice"),
                    "unit_price": unit_price,
                    "deal_text": deal_badge.get("text") or offer.get("promotionLabel"),
                    "photo_markdown": _markdown_image(product.get("name"), product.get("primaryImage")),
                }
            )
    return {
        "cart_id": cart.get("id"),
        "status": cart.get("status"),
        "min_cart_value": cart.get("minCartValue"),
        "item_count": len(entries),
        "items": entries,
        "raw_total": total_price if have_total else None,
    }


def _simplify_slot(slot: dict) -> dict:
    """Best-effort trim of a raw time-slot entry.

    Unlike the other simplifiers here, this one's field names are NOT
    verified against a live response -- get_time_slots hits a different,
    unauthenticated endpoint from an unrelated project (see its docstring
    in api_client.py), and no one has publicly confirmed its exact shape.
    `startTime`/`available` come from that project's own field names;
    endTime/fee/slotType are speculative aliases in case they exist. If
    the real shape differs, expect nulls here rather than an error -- that
    should make a shape mismatch obvious rather than silent.
    """
    return {
        "start_time": slot.get("startTime"),
        "end_time": slot.get("endTime"),
        "available": slot.get("available"),
        "fee": slot.get("fee") or slot.get("slotFee"),
        "slot_type": slot.get("slotType") or slot.get("type"),
    }


def _simplify_order_summary(order: dict) -> dict:
    return {
        "order_id": order.get("id"),
        "placed": order.get("placed"),
        "store": order.get("store"),
        "order_type": order.get("orderType"),
        "fulfillment_type": order.get("fulfillmentType"),
        "total": order.get("total"),
        "delivery_fee": order.get("deliveryFee"),
        "service_fee": order.get("serviceFee"),
    }


def _simplify_order_detail(detail: dict) -> dict:
    """Trim a single historical-order response to what's actually useful.

    Verified live against a real order: the raw response is ~85% one thing
    -- each of the 13 line items embeds a full ~60-field product object
    (promo badges, loyalty program fields, comparison prices, image URLs)
    on top of a separately-nested booking.pickupLocation store object
    (full address, departments, geofence, hours) that duplicates store
    identity already available from bannerName/session context. None of
    that is useful for "what did I order and how much did it cost" -- this
    keeps the order-level totals/status and, per item, just the product
    identity, quantity, and price.
    """
    od = detail.get("orderDetails") or {}
    entries: list[dict] = []
    for e in od.get("entries", []) or []:
        product = e.get("product") or {}
        entries.append(
            {
                "code": product.get("articleNumber") or product.get("id"),
                "name": product.get("productName"),
                "brand": product.get("brand"),
                "quantity": e.get("quantity"),
                "unit_price": e.get("unitPrice"),
                "total_price": e.get("totalPrice"),
                "availability_status": e.get("availabilityStatus"),
            }
        )
    pickup_location = ((od.get("booking") or {}).get("pickupLocation") or {})
    return {
        "order_number": od.get("orderNumber"),
        "order_type": od.get("orderType"),
        "status": od.get("status") or detail.get("statusDisplay") or od.get("deliveryStatus"),
        "store_name": pickup_location.get("name") or od.get("bannerName"),
        "sub_total": od.get("subTotal"),
        "total_price": od.get("totalPriceWithTax") or od.get("totalPrice"),
        "total_tax": od.get("totalTax"),
        "total_discounts": od.get("totalDiscounts"),
        "total_items": od.get("totalItems"),
        "points_earned": detail.get("pointsEarned"),
        "points_redeemed": detail.get("pointsRedeemed"),
        "item_count": len(entries),
        "items": entries,
    }


def _simplify_store(loc: dict) -> dict:
    """Trim a get_pickup_location response to what's useful for picking/confirming a store.

    Verified live against a real store: the raw response is ~14KB, and
    ~91% of that is two things this drops -- `storeDetails` (full 7-day
    hours for the store/pharmacy/optical/medical clinic, department phone
    numbers, manager name -- amenity-schedule data unrelated to grocery
    ordering) and `departments` (a 39-entry department-name directory).
    `openNowResponseData` is deliberately NOT included here even though
    it's small -- it's point-in-time data that goes stale the moment this
    is cached in known_stores, so it's its own tool (get_store_hours)
    instead, fetched fresh on demand.
    """
    address = loc.get("address") or {}
    return {
        "store_id": loc.get("storeId") or loc.get("pickupLocationId"),
        "name": loc.get("name"),
        "pickup_type": loc.get("pickupType"),
        "location_type": loc.get("locationType"),
        "is_shoppable": loc.get("isShoppable"),
        "visible": loc.get("visible"),
        "min_cart_value": loc.get("minCartValue"),
        "address": address.get("formattedAddress"),
        "phone": loc.get("orderContactNumber"),
        "pickup_instructions": loc.get("pickupInstructions"),
        "timezone": loc.get("timeZone"),
    }


def _simplify_loyalty(profile: dict, promotions: dict) -> dict:
    """Verified live: `profile.pcOptimum.points` (balance/dollarsRedeemable/
    dollarsRedeemedLifetime) is real, populated account data.
    `promotions.stampCards` is a real, working endpoint too, but only
    confirmed in the *inactive* shape (`isActive: false` on this account) --
    `balance`/`rewards` field names are carried through as-is when active,
    not independently verified against a populated example.

    There is no dedicated "browse/clip available personalized offers"
    endpoint in this API -- a dozen plausible URLs were tried and all
    404'd (see docs/RESEARCH.md "Loyalty offers"). The closest real thing
    to per-item offers is what search_products already surfaces per
    product (`deal_text`/`loyalty_points` on each result) -- this tool is
    account-level status, not an offer feed.
    """
    pc_optimum = profile.get("pcOptimum") or {}
    points = pc_optimum.get("points") or {}
    stamp_cards = promotions.get("stampCards") or {}
    return {
        "in_pc_optimum": profile.get("inPCOptimum"),
        "points_balance": points.get("balance"),
        "dollars_redeemable": points.get("dollarsRedeemable"),
        "dollars_redeemed_lifetime": points.get("dollarsRedeemedLifetime"),
        "stamp_card_active": stamp_cards.get("isActive"),
        "stamp_card_balance": stamp_cards.get("balance"),
        "stamp_card_rewards": stamp_cards.get("rewards"),
    }


# --- store / banner selection -------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Known Stores", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def list_stores() -> dict:
    """List stores this session already knows about (active store + any you've validated via set_active_store).

    PC Express has no publicly documented store-search API (see README
    "Finding your store_id" for how third parties get store lists -- mostly
    by scraping the site's own JS bundle, which this project deliberately
    does not do). Find your store's 4-digit id once via the banner
    website's/app's own store locator, then call set_active_store.
    """
    session = _load_session()
    return {
        "active_banner": session.banner,
        "active_store_id": session.store_id,
        "known_stores": session.known_stores,
        "hint": (
            "No store-search API is available. Find your store_id via the "
            "banner site's store locator (e.g. "
            "https://www.realcanadiansuperstore.ca/store-locator), then call "
            "set_active_store(store_id=..., banner=...)."
        ),
    }


def _cart_bound_store(cart: dict) -> Optional[str]:
    """The store a real cart is actually bound to right now --
    `orders[0].fulfillment.courier.storeId`. This is the field that
    actually determines SELLER_ID_MISMATCH, and confirmed live to be more
    reliable than `profile.lastStoreId` for this purpose: a real account
    was observed with `lastStoreId` still showing an old store well after
    the cart's own `courier.storeId` had already moved to a new one
    (following a real switch_cart_store-equivalent call) -- the two fields
    track different things and can genuinely disagree. Returns None if the
    cart has no orders yet (a brand new, never-used cart).
    """
    orders = cart.get("orders") or []
    if not orders:
        return None
    courier = (orders[0].get("fulfillment") or {}).get("courier") or {}
    return courier.get("storeId")


def _cart_store_mismatch_note(cart_store: Optional[str], target_store_id: str) -> Optional[str]:
    """Returns a warning string if `cart_store` (the account's real,
    currently cart-bound store, from `_cart_bound_store`) differs from
    `target_store_id` (the store being activated), else None. See
    set_active_store's docstring and _tool_error's SELLER_ID_MISMATCH
    handling for the underlying platform constraint this warns about.
    """
    if cart_store and cart_store != target_store_id:
        return (
            f"Heads up: this account's active cart is currently bound to store {cart_store}, not {target_store_id}. "
            "Search and browsing will work fine, but adding items for this store will fail until the cart is "
            f"re-bound -- call switch_cart_store(store_id='{target_store_id}', postal_code=...) to fix it, or add "
            "items for this store will raise a cart_store_mismatch error naming both stores again."
        )
    return None


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Active Store",
        read_only_hint=False,  # writes local session state (not PC Express account state)
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def set_active_store(store_id: str, banner: Optional[str] = None) -> dict:
    """Set the active store (and optionally banner) used by search/cart/slot tools.

    Validates the store id against the pickup-locations endpoint so a typo
    fails immediately instead of silently breaking later tool calls.

    If this banner's cart was last used at a *different* store (common on
    an account shared across locations -- e.g. family members both
    ordering from the same banner but different stores), the response
    includes a `cart_note` warning: PC Express only supports one active
    cart per banner at a time, bound to whichever store it was last used
    at (see docs/RESEARCH.md "Cart is bound to a single store" --
    confirmed live to be scoped per banner, not the whole account:
    Superstore and No Frills carts, for example, are genuinely
    independent and don't conflict with each other at all). This is a
    heads-up, not a hard error -- search/browsing work fine regardless;
    adding/updating cart items will fail with a `cart_store_mismatch`
    error until you call switch_cart_store to re-bind the cart to this
    store.
    """
    session = _load_session()
    if banner:
        try:
            config.banner_info(banner)
        except ValueError as exc:
            return {"error": "invalid_banner", "message": str(exc)}
        if banner != session.banner:
            # A cached cart_id belongs to whichever banner discovered it --
            # confirmed live this isn't just theoretical: without this,
            # a real cross-banner switch kept the previous banner's cart_id
            # cached, and the next cart write would have silently targeted
            # the wrong banner's cart instead of erroring. Clearing it here
            # forces _rediscover_cart to look it up fresh, scoped to the
            # new banner, the next time any cart tool runs.
            session.cart_id = None
        session.banner = banner

    api = _get_api(session.banner)
    try:
        location = api.get_pickup_location(store_id)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)

    store = _simplify_store(location)
    session.store_id = store_id
    session.known_stores[store_id] = store
    _save_session(session)
    result: dict[str, Any] = {"active_banner": session.banner, "active_store_id": store_id, "store": store}

    # Best-effort, non-fatal: a failure here shouldn't block set_active_store
    # itself succeeding, since store selection and cart state are separate
    # concerns -- see _tool_error's SELLER_ID_MISMATCH handling for what
    # happens if this warning goes unheeded and an add is attempted anyway.
    # Checks the cart's own real fulfillment binding, not profile.lastStoreId
    # -- confirmed live that the two can disagree (see _cart_bound_store).
    try:
        cart_store = _cart_bound_store(_get_cart_healing(api, session))
    except (PcidAuthError, PcxApiError):
        cart_store = None
    note = _cart_store_mismatch_note(cart_store, store_id)
    if note:
        result["cart_note"] = note

    return result


def _find_fulfillment_location_id(serviceability: dict, banner: str, store_id: str) -> Optional[str]:
    """Pick the real courier fulfillmentLocationId for `store_id` out of a
    get_delivery_serviceability response, scoped to `banner` -- that
    response lists every banner serviceable from a postal code, not just
    the caller's own, and IDs from other banners aren't valid here even if
    a numeric store_id happens to collide.
    """
    for entry in serviceability.get("banners") or []:
        if entry.get("name") != banner:
            continue
        for loc in entry.get("pickupLocations") or []:
            location_id = loc.get("id")
            if location_id and str(location_id).startswith(store_id):
                return location_id
    return None


@mcp.tool(
    annotations=ToolAnnotations(
        title="Switch Cart to a Different Store",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def switch_cart_store(store_id: str, postal_code: str) -> dict:
    """Re-bind the account's existing cart to a different store -- the
    real, confirmed-live fix for a `cart_store_mismatch`/SELLER_ID_MISMATCH
    error, no PC Express app needed.

    PC Express only has one active cart per banner (Superstore, No
    Frills, etc. each have their own -- confirmed live, this is not
    account-wide), bound to whichever store it was last used at within
    that banner (see set_active_store's docstring). This tool re-binds
    the *active banner's* cart, the same one every other cart tool in
    this session uses. An earlier version of this project concluded
    there was no API-level way to change that -- wrong, corrected after
    a user-supplied real curl capture showed the actual working shape
    (see docs/RESEARCH.md "Cart is bound to a single store"): this
    looks up the real delivery
    fulfillment-location id for `store_id` from `postal_code` (a delivery
    address that store can actually service -- its own store address is a
    reasonable default if you don't have a specific one), then re-binds
    the cart's fulfillment to it. Existing cart items carry over,
    re-priced against the new store's catalog, not dropped.

    This changes the account's real cart, immediately -- there's no
    separate confirm step, unlike place_order. Call set_active_store for
    `store_id` too if you also want future search/add_to_cart calls to
    default to this store.
    """
    session = _load_session()
    api = _get_api(session.banner)
    try:
        # Always re-discover the cart id fresh here, never trust a cached
        # one (_ensure_customer_and_cart would skip this if session.cart_id
        # is already set, even if stale) -- confirmed live that a stale
        # cached cart_id makes set_cart_fulfillment fail with a 503/422
        # from PC Express's own backend, not a clean 404 the usual
        # self-healing (_get_cart_healing/_update_cart_healing) would
        # catch. This tool exists specifically to fix broken cart state,
        # so it should never propagate that same kind of staleness itself.
        _rediscover_cart(api, session)
        serviceability = api.get_delivery_serviceability(postal_code)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)

    location_id = _find_fulfillment_location_id(serviceability, session.banner, store_id)
    if not location_id:
        return {
            "error": "store_not_serviceable",
            "message": (
                f"No delivery fulfillment location found for store {store_id!r} on banner "
                f"{session.banner!r} from postal code {postal_code!r}. Double-check the store_id, "
                "and that this postal code is actually within that store's delivery area."
            ),
        }

    try:
        raw = api.set_cart_fulfillment(session.cart_id, location_id, postal_code)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)

    result = _simplify_cart(raw.get("cart", raw))
    result["switched_to_store"] = store_id
    if raw.get("errors"):
        result["warnings"] = raw["errors"]
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        title="Check If a Store Is Open Now",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def get_store_hours(store_id: Optional[str] = None) -> dict:
    """Check whether a store is open right now, and today's hours.

    Split out from set_active_store/list_stores on purpose: open/closed
    status is point-in-time and would go stale sitting in cached store
    info, so this fetches it fresh every call instead. Uses the active
    store if store_id is omitted.
    """
    session = _load_session()
    target_store_id = store_id or session.store_id
    if not target_store_id:
        return {"error": "no_active_store", "message": "Call set_active_store first, or pass store_id explicitly."}
    api = _get_api(session.banner)
    try:
        location = api.get_pickup_location(target_store_id)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    open_now = location.get("openNowResponseData") or {}
    return {
        "store_id": target_store_id,
        "name": location.get("name"),
        "open_now": open_now.get("openNow"),
        "hours_today": open_now.get("hours"),
        "date": open_now.get("date"),
    }


# --- loyalty ---------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get PC Optimum Points Balance & Loyalty Status",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def get_loyalty_status() -> dict:
    """PC Optimum points balance and loyalty program status for this account.

    Returns real, live points balance and redeemable dollar value, plus
    stamp-card program status if this banner runs one. **This is account
    status, not a feed of available offers/coupons to browse or clip** --
    no such endpoint could be found in this API despite trying a dozen
    plausible URLs (see docs/RESEARCH.md "Loyalty offers"). For
    product-specific deals, check the `deal_text`/`loyalty_points` fields
    search_products already returns per result -- that's the real "offers"
    data this API actually exposes.
    """
    api = _get_api(_load_session().banner)
    try:
        profile = api.get_profile()
        promotions = api.get_customer_promotions()
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    return _simplify_loyalty(profile, promotions)


# --- products ------------------------------------------------------------------


MAX_SIZE_WITH_NUTRITION = 15


def _cap_size_for_nutrition(size: int, include_nutrition: bool) -> tuple[int, bool]:
    """Returns (effective_size, was_capped). See search_products' docstring
    for why: Open Food Facts' documented 15 req/min/IP limit means a
    single enriched search can't safely look up more than 15 products.
    """
    if include_nutrition and size > MAX_SIZE_WITH_NUTRITION:
        return MAX_SIZE_WITH_NUTRITION, True
    return size, False


def _cap_results_for_nutrition(results: list, include_nutrition: bool) -> tuple[list, bool]:
    """Returns (capped_results, was_capped). Separate from
    _cap_size_for_nutrition because capping the *requested* size isn't
    sufficient on its own -- PC Express can itself return more results
    than requested (confirmed live: asked for 5, got 7), so the actual
    results list needs its own truncation to reliably keep nutrition
    lookups at or under 15 per call.
    """
    if include_nutrition and len(results) > MAX_SIZE_WITH_NUTRITION:
        return results[:MAX_SIZE_WITH_NUTRITION], True
    return results, False


def _enrich_with_nutrition(results: list[dict], client: httpx.Client) -> dict[str, Any]:
    """Attach Open Food Facts nutrition data (see `_simplify_nutrition`) to
    each result that has a `barcode`, in place. Paced ~1 second apart
    between actual lookups (not counting skipped no-barcode results) to
    stay well clear of Open Food Facts' documented 15 req/min/IP limit --
    see search_products' docstring. Stops early, without raising, if the
    limit is hit anyway; already-enriched results are left as they are.
    """
    enriched_count = 0
    rate_limited = False
    made_a_request = False
    for product in results:
        barcode = product.get("barcode")
        if not barcode:
            continue
        if made_a_request:
            time.sleep(1)
        made_a_request = True
        try:
            off_product = nutrition_client.fetch_nutrition(barcode, client=client)
        except nutrition_client.OpenFoodFactsRateLimited:
            rate_limited = True
            break
        if off_product is not None:
            product["nutrition"] = _simplify_nutrition(off_product)
            enriched_count += 1
        else:
            product["nutrition"] = {"found": False}
    return {"enriched_count": enriched_count, "rate_limited": rate_limited}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Products", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )
)
def search_products(query: str, size: int = 20, offset: int = 0, include_nutrition: bool = False) -> dict:
    """Search the product catalog at the active store.

    For more relevant results, call get_purchase_history once near the
    start of a session (not before every search) to learn what this
    household actually buys, then use that context when choosing a
    query or ranking/recommending results.

    Requires an active store (see set_active_store). Each result carries
    everything this API actually exposes about a product -- name, brand,
    truncated description, every distinct product photo (`image_urls`,
    one URL per angle), package size, barcode, aisle, current/regular/
    member price, per-unit price (for value comparisons across package
    sizes), and any active deal/loyalty offer. **No nutrition facts or
    ingredients are available from PC Express's own API** -- set
    `include_nutrition=True` to attach Open Food Facts data per result
    (see below), or call `get_nutrition_info(barcode)` separately for one
    product at a time. `code` is the product identifier needed by
    add_to_cart/remove_from_cart; `sku` (bare article number) and
    `barcode` (UPC) are what you'd use to cross-reference this product
    elsewhere.

    Each result also carries `photo_markdown` -- a ready-to-paste
    `![name](url)` tag for its main photo. Include it directly in your own
    reply text (not just the raw `image_urls`) when showing products to
    the user, so the photo actually renders inline in the chat instead of
    staying invisible in tool output.

    This endpoint is genuinely paginated -- `offset` is a real, working
    item offset (verified live: `offset=5` returns different products than
    `offset=0`, not a repeat). `total_results` in the response is this
    account's real count of matching products, not just how many came back
    in `results` -- if it's larger than `len(results)`, call again with
    `offset` advanced by however many you just got to see more. Treat
    `total_results` as an estimate, not exact -- PC Express's search
    appears to be personalized/model-driven and the count can shift a few
    percent between otherwise-identical calls.

    **No filter or sort parameters (brand, price range, dietary/lifestyle,
    category, price/name sort) are exposed here, on purpose**: the raw
    response advertises real filter/sort facets, but applying one could
    not be made to work against the real API after trying roughly a dozen
    plausible request shapes for both sort and brand filtering -- every
    attempt was either rejected outright or silently had zero effect on
    the actual results (see docs/RESEARCH.md "Search filters/sort"). Do
    your own filtering/sorting over the returned results (or across
    multiple paginated calls) using the fields above -- don't try passing
    an undocumented filter/sort kwarg here, it doesn't exist and won't do
    anything.

    **`include_nutrition=True` limits (read before using):** Open Food
    Facts documents a hard limit of 15 requests/minute/IP for product
    lookups (https://openfoodfacts.github.io/openfoodfacts-server/api/),
    and this option makes one such request per result, sequentially, with
    a ~1 second pace between them to stay well clear of that limit within
    a single call. Consequences:
    - **At most 15 results total whenever this is enabled** -- a larger
      `size` is silently reduced, not rejected, and (since PC Express can
      itself return more results than requested -- confirmed live: asked
      for 5, got 7) the results list is *also* truncated to 15 after the
      fact if PC Express handed back more than that. Either way,
      `nutrition_enrichment.size_capped` in the response tells you if
      truncation happened, and `returned` always reflects the real,
      final count.
    - The call takes noticeably longer (up to ~15 seconds for 15 results
      with barcodes) -- this is expected, not a hang.
    - If the rate limit is hit partway through anyway,
      `nutrition_enrichment.rate_limited` will be true and remaining
      results simply won't have a `nutrition` field -- not an error, and
      the results themselves are still complete and valid.
    - Per-item "not found" (`result["nutrition"] = {"found": false, ...}`)
      is common and expected -- coverage varies by product, and
      weight-priced items (deli/meat/produce) never have a real barcode.
    Only turn this on when you actually need nutrition data to answer the
    request -- for a plain product search, leave it off (the default) and
    it costs nothing extra.
    """
    session = _load_session()
    if not session.store_id:
        return {"error": "no_active_store", "message": "Call set_active_store first."}

    size, _ = _cap_size_for_nutrition(size, include_nutrition)

    api = _get_api(session.banner)
    try:
        raw = api.search_products(query, session.store_id, cart_id=session.cart_id, size=size, from_=offset)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    results = [_simplify_product(p) for p in raw.get("results", [])]
    pagination = raw.get("pagination") or {}
    total_results = pagination.get("totalResults")

    # PC Express can return MORE results than the requested `size` -- see
    # search_products' docstring/docs/RESEARCH.md ("asked for 5, got 7").
    # Requesting size<=15 above is not sufficient on its own to guarantee
    # at most 15 nutrition lookups; the actual results list has to be
    # truncated too. Confirmed live: a size=3 request once came back with
    # 11 results, which would have made 11 sequential Open Food Facts
    # calls if this truncation weren't here.
    results, results_capped = _cap_results_for_nutrition(results, include_nutrition)

    response: dict[str, Any] = {
        "query": query,
        "store_id": session.store_id,
        "offset": offset,
        "returned": len(results),
        "total_results": total_results,
        "has_more": total_results is not None and (offset + len(results)) < total_results,
        "results": results,
    }

    if include_nutrition:
        with httpx.Client(timeout=10.0) as off_client:
            summary = _enrich_with_nutrition(results, off_client)
        response["nutrition_enrichment"] = {
            "enabled": True,
            "size_capped": results_capped,
            "enriched_count": summary["enriched_count"],
            "rate_limited": summary["rate_limited"],
            "data_source": "Open Food Facts (openfoodfacts.org, ODbL-licensed) -- not affiliated with PC Express or Loblaw",
        }

    return response


# OFF's own allergen taxonomy codes for 4 of the 14 EU-regulated allergens
# -- stable, documented tag IDs, not something that varies per product.
# "milk" is used as the closest available proxy for a lactose-free check:
# OFF tracks the *milk* allergen (any milk protein), not lactose content
# specifically, so a product tagged allergen-free-of-milk is a reasonable
# but imperfect stand-in (e.g. lactose-free milk itself still carries the
# milk allergen tag -- confirmed live, see this account's own real milk
# product snapshot).
_ALLERGEN_TAGS = {
    "gluten": "en:gluten",
    "milk": "en:milk",
    "soy": "en:soybeans",
    "sulfites": "en:sulphur-dioxide-and-sulphites",
}


def _allergen_status(allergens_tags: list, traces_tags: list) -> dict:
    """"contains" / "may_contain" (cross-contamination trace) / "not_declared"
    per allergen, derived from OFF's own manufacturer-declared tags -- not
    an independent safety guarantee. Deliberately worded as a factual
    declaration status, not "safe"/"unsafe", for exactly that reason: this
    reflects what a label says, not a certification. There's no "pork"
    entry here -- pork isn't one of the 14 EU-regulated allergens OFF
    tracks, so there's no reliable structured signal for it (unlike
    Yuka's "Pork-free" toggle, which likely comes from ingredient-text
    parsing this project deliberately doesn't attempt -- too easy to get
    a false negative on something that matters for religious/dietary
    reasons, not just preference).
    """
    result = {}
    for name, tag in _ALLERGEN_TAGS.items():
        if tag in allergens_tags:
            result[name] = "contains"
        elif tag in traces_tags:
            result[name] = "may_contain"
        else:
            result[name] = "not_declared"
    return result


def _dietary_flags(ingredients_analysis_tags: list) -> dict:
    """vegan/vegetarian/palm-oil status -- OFF encodes these as tags like
    `en:non-vegan` / `en:maybe-vegetarian` / `en:palm-oil-content-unknown`
    rather than separate boolean fields (verified live against two real
    products showing tags from all three categories). Collapsed to
    yes/no/maybe/unknown per category.

    Deliberately an explicit tag->status table, not a generic pattern
    match: an earlier draft tried to derive the "yes" tag name from the
    key generically (e.g. `palm_oil_free` -> `palm-oil-free`), which
    silently broke for palm oil specifically -- `en:palm-oil` (the
    *contains* case) and `en:palm-oil-free` differ by more than the
    "-free" suffix pattern the vegan/vegetarian tags follow, so the
    generic version would have mapped "contains palm oil" to "yes,
    palm-oil-free". Caught before shipping, not after.
    """
    flags = {"vegan": "unknown", "vegetarian": "unknown", "palm_oil_free": "unknown"}
    tag_map = {
        "vegan": ("vegan", "yes"),
        "non-vegan": ("vegan", "no"),
        "maybe-vegan": ("vegan", "maybe"),
        "vegetarian": ("vegetarian", "yes"),
        "non-vegetarian": ("vegetarian", "no"),
        "maybe-vegetarian": ("vegetarian", "maybe"),
        "palm-oil-free": ("palm_oil_free", "yes"),
        "palm-oil": ("palm_oil_free", "no"),
        "may-contain-palm-oil": ("palm_oil_free", "maybe"),
    }
    for raw_tag in ingredients_analysis_tags:
        tag = raw_tag.removeprefix("en:")
        if tag in tag_map:
            key, status = tag_map[tag]
            flags[key] = status
        # else: the "-status-unknown"/"-content-unknown" tags, or anything
        # unrecognized -- leave that category at "unknown" rather than guess.
    return flags


def _simplify_nutrition(product: dict) -> dict:
    """Trim an Open Food Facts product to what's useful for a food-quality
    decision -- verified live against real products from this store's own
    catalog (see `nutrition_client.py`). Kept intentionally narrow: OFF's
    raw response carries dozens of contributor/internal-tracking fields
    (keyword lists, correction history, per-language variants) that don't
    help "should I buy this."

    `nutriscore_grade` (a-e) and `nova_group` (1-4, processing level) are
    the two headline signals -- closest thing this has to a Yuka-style
    single-glance grade. **There is deliberately no fabricated single
    0-100 "score" here** the way Yuka shows one -- that number is Yuka's
    own proprietary weighting of Nutri-Score/additives/organic-status/etc,
    not something Open Food Facts' API provides, and guessing at their
    formula isn't something this project is going to do. Everything real
    that goes into a judgment like that is exposed individually below
    instead (grades, nutrient_levels, additives, dietary flags) --
    reasoning over structured signals in natural language is something an
    LLM caller can do directly, and honestly, better than trusting an
    opaque single number.

    `nutrient_levels` (low/moderate/high per fat/saturated fat/sugars/salt)
    is OFF's own real qualitative traffic-light rating, not derived here.
    `additives` is a real E-number list/count with no risk classification
    attached -- OFF's API doesn't provide per-additive risk levels (Yuka's
    "limited risk" style labels are Yuka's own separate analysis, not
    available from this data source). `dietary_flags` and `allergen_status`
    are explained in `_dietary_flags`/`_allergen_status` above.
    """
    nutriments = product.get("nutriments") or {}
    return {
        "product_name": product.get("product_name"),
        "brands": product.get("brands"),
        "quantity": product.get("quantity"),
        "nutriscore_grade": product.get("nutriscore_grade"),
        "nova_group": product.get("nova_group"),
        "ecoscore_grade": product.get("ecoscore_grade"),
        "ingredients_text": product.get("ingredients_text"),
        "allergens": product.get("allergens"),
        "allergen_status": _allergen_status(product.get("allergens_tags") or [], product.get("traces_tags") or []),
        "dietary_flags": _dietary_flags(product.get("ingredients_analysis_tags") or []),
        "nutrient_levels": {k.replace("-", "_"): v for k, v in (product.get("nutrient_levels") or {}).items()},
        "additives": {
            "count": product.get("additives_n"),
            "codes": product.get("additives_tags") or [],
        },
        "per_100g": {
            "energy_kcal": nutriments.get("energy-kcal_100g"),
            "protein_g": nutriments.get("proteins_100g"),
            "fat_g": nutriments.get("fat_100g"),
            "saturated_fat_g": nutriments.get("saturated-fat_100g"),
            "carbohydrates_g": nutriments.get("carbohydrates_100g"),
            "sugars_g": nutriments.get("sugars_100g"),
            "fiber_g": nutriments.get("fiber_100g"),
            "salt_g": nutriments.get("salt_100g"),
        },
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Nutrition Info (Open Food Facts)",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
def get_nutrition_info(barcode: str) -> dict:
    """Nutrition facts, ingredients, allergens, and Nutri-Score/NOVA/Eco-Score
    grade for a product, looked up by barcode against Open Food Facts -- a
    free, community-maintained database, wholly separate from PC Express/
    Loblaw. No PC Express account or active store needed; this doesn't
    touch PC Express's API at all.

    Also returns `dietary_flags` (vegan/vegetarian/palm-oil-free, each
    yes/no/maybe/unknown), `allergen_status` (gluten/milk/soy/sulfites,
    each contains/may_contain/not_declared -- a factual label-declaration
    status, not an independent safety guarantee), `nutrient_levels` (OFF's
    own low/moderate/high traffic-light rating per fat/saturated
    fat/sugars/salt), and `additives` (E-number count + codes, no risk
    classification -- OFF's API doesn't provide per-additive risk levels).
    **Deliberately no single 0-100 "score"** the way some consumer apps
    show -- that's their own proprietary weighting of these same signals,
    not something this data source provides; reason over the real
    components above directly rather than expecting one fabricated here.
    No "pork-free" check either -- pork isn't one of the allergens OFF
    tracks, so there's no reliable structured signal for it. See
    `_simplify_nutrition`'s docstring in server.py for the full reasoning.

    Pass the `barcode` field a `search_products` result already gave you.
    Real, verified coverage of this store's catalog is good (100% in a
    15-item live sample) -- but **"not found" is a normal, expected result
    for plenty of real products**, not a bug: items sold by weight
    (fresh meat/deli/produce) never have a manufacturer barcode to begin
    with, and any database's coverage of any given item isn't guaranteed.
    Treat a `found: false` response as "no data available," not "this tool
    is broken."

    **Call this selectively, one product at a time, for items you actually
    need nutrition info on** -- not automatically for every search result.
    Open Food Facts documents a hard limit of 15 requests/minute/IP for
    product lookups (https://openfoodfacts.github.io/openfoodfacts-server/api/)
    and explicitly discourages anything resembling bulk/search-as-you-type
    use; most search results won't need this level of detail anyway.
    """
    try:
        product = nutrition_client.fetch_nutrition(barcode)
    except nutrition_client.OpenFoodFactsRateLimited:
        return {
            "found": False,
            "barcode": barcode,
            "message": (
                "Open Food Facts rate limit hit (15 requests/minute/IP). This is not "
                "a 'no data' result -- wait a bit before looking up another product."
            ),
        }
    if product is None:
        return {
            "found": False,
            "barcode": barcode,
            "message": (
                "Not found in Open Food Facts. This is common and not an error -- "
                "coverage varies, and items sold by weight never have a real barcode."
            ),
        }
    result = _simplify_nutrition(product)
    result["found"] = True
    result["barcode"] = barcode
    result["data_source"] = "Open Food Facts (openfoodfacts.org, ODbL-licensed) -- not affiliated with PC Express or Loblaw"
    return result


# --- cart ------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="View Cart", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def get_cart() -> dict:
    """View the current cart contents and total.

    Each item carries `photo_markdown` -- paste it directly into your
    reply (not just the item name) so the cart actually looks like a cart.
    """
    session = _load_session()
    api = _get_api(session.banner)
    try:
        raw = _get_cart_healing(api, session)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    return _simplify_cart(raw)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add to Cart",
        read_only_hint=False,
        destructive_hint=False,
        # The underlying API sets an entry's quantity rather than
        # incrementing it, so calling this again with the same args is a
        # no-op against an unchanged cart.
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def add_to_cart(items: list[dict[str, Any]]) -> dict:
    """Add one or more products to the cart, or increase their quantity, in a single call.

    When choosing or confirming what to add, especially on someone
    else's behalf, it helps to already have context from
    get_purchase_history (call it once near the start of a session, not
    before every add) on what this household actually buys.

    items: a list of {"product_code": str, "quantity": int (default 1),
    "fulfillment_method": "pickup"|"delivery" (default "pickup")}. All items
    are sent as one cart update -- the underlying API already accepts
    multiple product codes per call (it's a dict keyed by product code),
    this tool just didn't expose that until a real user report about
    excessive round-trips when adding several items at once. If the same
    product_code appears more than once, the last entry for it wins.
    Requires an active store.

    The returned cart's items carry `photo_markdown` -- include it in your
    reply when confirming what was added.
    """
    if not items:
        return {"error": "invalid_items", "message": "items must be a non-empty list."}
    session = _load_session()
    if not session.store_id:
        return {"error": "no_active_store", "message": "Call set_active_store first."}
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        product_code = item.get("product_code")
        if not product_code:
            return {"error": "invalid_items", "message": "Each item needs a product_code."}
        quantity = item.get("quantity", 1)
        if quantity <= 0:
            return {
                "error": "invalid_quantity",
                "message": f"quantity for {product_code!r} must be >= 1 (use remove_from_cart to remove).",
            }
        fulfillment_method = item.get("fulfillment_method", "pickup")
        entries[product_code] = {
            "quantity": quantity,
            "fulfillmentMethod": fulfillment_method,
            "sellerId": session.store_id,
        }
    api = _get_api(session.banner)
    try:
        raw = _update_cart_healing(api, session, entries)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    result = _simplify_cart(raw)
    result["items_requested"] = len(entries)
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        title="Remove from Cart", read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    )
)
def remove_from_cart(product_codes: list[str]) -> dict:
    """Remove one or more products from the cart entirely, in a single call.

    The returned cart's remaining items carry `photo_markdown` -- include
    it when confirming the cart's new contents.
    """
    if not product_codes:
        return {"error": "invalid_items", "message": "product_codes must be a non-empty list."}
    session = _load_session()
    api = _get_api(session.banner)
    # One lookup, reused for every item -- not per product code.
    seller_id = _seller_id_for_removal(api, session)
    entries: dict[str, dict[str, Any]] = {}
    for product_code in product_codes:
        entry: dict[str, Any] = {"quantity": 0}
        if seller_id:
            entry.update({"fulfillmentMethod": "pickup", "sellerId": seller_id})
        entries[product_code] = entry
    try:
        raw = _update_cart_healing(api, session, entries)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    result = _simplify_cart(raw)
    result["items_requested"] = len(entries)
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Cart Quantity",
        read_only_hint=False,
        destructive_hint=True,  # can reduce/zero out quantity, i.e. lose cart data
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def update_quantity(items: list[dict[str, Any]]) -> dict:
    """Set cart quantities for one or more products directly, in a single
    call (quantity=0 removes that item).

    items: a list of {"product_code": str, "quantity": int}.

    The returned cart's items carry `photo_markdown` -- include it when
    confirming the cart's new contents.
    """
    if not items:
        return {"error": "invalid_items", "message": "items must be a non-empty list."}
    for item in items:
        if not item.get("product_code") or item.get("quantity") is None:
            return {"error": "invalid_items", "message": "Each item needs product_code and quantity."}
        if item["quantity"] < 0:
            return {
                "error": "invalid_quantity",
                "message": f"quantity for {item['product_code']!r} must be >= 0.",
            }
    session = _load_session()
    needs_active_store = any(item["quantity"] > 0 for item in items)
    if needs_active_store and not session.store_id:
        return {"error": "no_active_store", "message": "Call set_active_store first."}
    api = _get_api(session.banner)
    # Lazy, computed at most once, only if some item is actually a removal.
    removal_seller_id: Optional[str] = None
    removal_seller_id_computed = False
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        product_code = item["product_code"]
        quantity = item["quantity"]
        entry: dict[str, Any] = {"quantity": quantity}
        if quantity > 0:
            entry.update({"fulfillmentMethod": "pickup", "sellerId": session.store_id})
        else:
            if not removal_seller_id_computed:
                removal_seller_id = _seller_id_for_removal(api, session)
                removal_seller_id_computed = True
            if removal_seller_id:
                entry.update({"fulfillmentMethod": "pickup", "sellerId": removal_seller_id})
        entries[product_code] = entry
    try:
        raw = _update_cart_healing(api, session, entries)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    result = _simplify_cart(raw)
    result["items_requested"] = len(entries)
    return result


# --- fulfillment slots -----------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Available Pickup/Delivery Slots",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def get_available_slots() -> dict:
    """List pickup/delivery time slots for the active store, if any are available.

    The least-verified tool in this server: it calls a different,
    unauthenticated public endpoint from an unrelated (archived) project,
    not pcx-bff -- no pcx-bff timeslot endpoint has been confirmed by
    anyone publicly. Treat results as advisory and double-check in the app
    before relying on a specific slot. See api_client.get_time_slots.
    """
    session = _load_session()
    if not session.store_id:
        return {"error": "no_active_store", "message": "Call set_active_store first."}
    api = _get_api(session.banner)
    try:
        raw = api.get_time_slots(session.store_id)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    slots_raw = raw.get("timeSlots", raw if isinstance(raw, list) else [])
    slots = [_simplify_slot(s) for s in slots_raw if isinstance(s, dict)]
    return {"store_id": session.store_id, "slot_count": len(slots), "slots": slots}


# --- checkout handoff / orders -----------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Checkout Handoff (Confirm Required)",
        read_only_hint=False,
        # Flagged destructive/non-idempotent-adjacent on purpose: this is
        # the tool closest to "spends real money" in this server (even
        # though it never actually submits payment itself -- see the
        # confirm=True gate below), and a client should treat it with the
        # same caution it would a genuinely destructive action.
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def place_order(confirm: bool = False) -> dict:
    """Validate the cart and hand off to the real checkout -- does NOT submit payment.

    No PC Express payment/checkout-submission API is known or implemented
    here on purpose. This tool checks the cart is non-empty, then returns a
    checkout URL/summary for you to finish yourself in the PC Express app
    or a browser (where you're already logged in). Requires confirm=True so
    it can never fire as a side effect of a model just "trying things."

    Before presenting `checkout_url`, show a full visual receipt in your
    reply: every item's `photo_markdown` from `cart_summary.items`,
    quantity, and price, plus `cart_summary.raw_total`. The goal is that
    the only reason left to open the PC Express app is the actual payment
    tap -- everything worth reviewing (what's in the cart, what it costs)
    should already be visible right here, not require switching apps to
    go check.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": "Call again with confirm=True to proceed. This will NOT place the order for you -- "
            "it validates the cart and gives you a link to finish checkout yourself.",
        }
    session = _load_session()
    api = _get_api(session.banner)
    try:
        raw = _get_cart_healing(api, session)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)

    cart = _simplify_cart(raw)
    if cart["item_count"] == 0:
        return {"error": "empty_cart", "message": "Cart is empty -- add items before checking out."}

    domain = config.banner_info(session.banner)["domain"]
    return {
        "status": "ready_for_manual_checkout",
        "message": (
            "Cart is ready. This tool does not submit payment -- show the user a full visual receipt "
            "(each item's photo_markdown, quantity, price, and the total) right here before mentioning "
            "the checkout link, so the only thing left to do in the PC Express app is pick a slot, "
            "confirm substitutions, and pay."
        ),
        "checkout_url": f"https://{domain}/checkout",
        "cart_summary": cart,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Order Status", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def get_order_status(order_id: Optional[str] = None, limit: int = 10) -> dict:
    """Get a specific past order's details, or list recent order history if order_id is omitted.

    `limit` caps how many orders are returned when listing (most recent
    first; this account had 284 total in testing) -- ignored when order_id
    is given. Both the list and single-order responses are trimmed of
    per-item product bloat (promo/loyalty/image metadata) and duplicated
    store metadata; see _simplify_order_summary/_simplify_order_detail.
    """
    session = _load_session()
    api = _get_api(session.banner)
    try:
        if order_id:
            raw = api.get_historical_order(order_id)
            return _simplify_order_detail(raw)
        raw = api.get_historical_orders()
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)

    all_orders = raw.get("orderHistory", []) or []
    orders = [_simplify_order_summary(o) for o in all_orders[:limit]]
    return {
        "total_online_orders": raw.get("onlineOrdersCount"),
        "total_offline_orders": raw.get("offlineOrdersCount"),
        "returned": len(orders),
        "orders": orders,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Purchase History", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def get_purchase_history(limit: int = 20, max_orders_scanned: int = 60) -> dict:
    """Real items this account has actually bought before, at the active
    store -- aggregated per product, most-frequently-bought first.

    Call this once, early in a session, before searching or adding
    items -- not before every subsequent tool call. One call is enough
    to prime your context with what this household actually buys; reuse
    that context for every search/recommendation/cart action in the rest
    of the session rather than re-fetching it. An item this household
    buys repeatedly is a safer, better-catered default than a
    similar-looking one it's never bought. This only shows what was
    *bought*, not what was liked or disliked -- still ask when unsure,
    especially for anything non-staple or when buying on someone else's
    behalf.

    Scoped to the currently active store (see set_active_store); orders
    from other stores/banners on this account are excluded -- resolved
    from each order's *real* store id (fetched per order), not the
    human-readable store name alone, which has accumulated multiple
    legacy variants over time on a real account (e.g. "1024-Oakville"
    and "North Oakville" both turned out to be the same physical store
    under old naming -- confirmed live, not assumed).

    Real order history can run into the hundreds of orders, and most of
    that volume is in fetching each order's full detail (the only place
    line items and the real store id live) -- too slow to do for the
    entire history on one tool call. This scans at most
    `max_orders_scanned` recent orders (newest first) and stops once
    `limit` *matching* (right-store) orders have been found; raise
    `max_orders_scanned` if your store's orders are a small fraction of
    total history and matches are coming up short.
    """
    session = _load_session()
    if not session.store_id:
        return {"error": "no_active_store", "message": "Call set_active_store first."}
    api = _get_api(session.banner)
    try:
        raw = api.get_historical_orders()
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)

    order_summaries = sorted(raw.get("orderHistory") or [], key=lambda o: o.get("placed") or "", reverse=True)

    items_by_code: dict[str, dict] = {}
    matched_orders = 0
    scanned = 0
    for summary in order_summaries:
        if matched_orders >= limit or scanned >= max_orders_scanned:
            break
        scanned += 1
        order_id = summary.get("id")
        if not order_id:
            continue
        try:
            detail = api.get_historical_order(order_id)
        except (PcidAuthError, PcxApiError):
            continue
        od = detail.get("orderDetails") or {}
        pickup = (od.get("booking") or {}).get("pickupLocation") or {}
        if (pickup.get("storeId") or pickup.get("id")) != session.store_id:
            continue
        matched_orders += 1
        for e in od.get("entries", []) or []:
            product = e.get("product") or {}
            code = product.get("articleNumber") or product.get("id")
            if not code:
                continue
            # order_summaries is newest-first, so each code's first
            # appearance here is already its most recent purchase.
            entry = items_by_code.setdefault(
                code,
                {
                    "code": code,
                    "name": product.get("productName"),
                    "brand": product.get("brand"),
                    "times_purchased": 0,
                    "total_quantity": 0.0,
                    "last_purchased": summary.get("placed"),
                },
            )
            entry["times_purchased"] += 1
            entry["total_quantity"] += e.get("quantity") or 0

    items = sorted(items_by_code.values(), key=lambda i: i["times_purchased"], reverse=True)
    return {
        "store_id": session.store_id,
        "orders_scanned": scanned,
        "orders_matched": matched_orders,
        "distinct_items": len(items),
        "items": items,
    }


async def _health(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


def main_http() -> None:
    """Serve over Streamable HTTP, gated by this project's own minimal OAuth server.

    This is what makes the server reachable from a *remote* MCP client
    (Claude iOS/Android, which only support remote MCP, not local stdio --
    see README "Remote/mobile access"). This is a genuinely different risk
    posture than the default stdio mode in main(): your PC Express tokens
    both live on whatever host runs this process, and that host must be
    reachable over HTTPS for Claude to connect to it.

    Auth here is a small OAuth 2.1 + PKCE authorization server this project
    runs itself (oauth_server.py) -- not a bare static bearer header, and
    not gated by a fixed passphrase or a fixed allowlisted account either.
    That's because Claude's remote-connector flow tries OAuth Dynamic
    Client Registration by default, which fails outright against a server
    with no /register endpoint; a bare bearer header only works on
    accounts where Claude's separate `static_headers` beta feature happens
    to be exposed in the UI, which is not guaranteed (confirmed by hitting
    exactly that failure against a real account). Instead, /authorize
    requires a fresh PC ID login and only approves the connector if the
    account that logs in matches whatever client_id Claude claimed --
    open self-service, any PC Express account. Each tenant's PC Express
    credentials then live only inside the encrypted OAuth token they hold
    (PCEXPRESS_TOKEN_SECRET), never on this server's disk -- see
    oauth_server.py's module docstring for the full security model,
    including why "any successful PC ID login" alone would NOT be a safe
    gate, and for the deliberate scope tradeoffs of open self-service
    (README "Remote/mobile access" covers the infrastructure/abuse side).

    This function does NOT provide TLS on its own -- by default it only
    binds to 127.0.0.1 (localhost), which is deliberately useless for real
    remote access until you put a reverse proxy (Caddy, nginx, Cloudflare
    Tunnel, etc.) in front of it and set PCEXPRESS_HTTP_HOST=0.0.0.0
    yourself (only if that proxy runs on a *different* host -- same-host
    proxies like the Caddy setup in the README should leave this alone).
    """
    import os

    try:
        import uvicorn
    except ImportError:
        raise SystemExit("HTTP mode needs uvicorn. Install with: pip install -e '.[http]'")

    if not hasattr(mcp, "streamable_http_app"):
        raise SystemExit(
            "This version of the installed mcp SDK doesn't expose "
            "streamable_http_app(). Upgrade with: pip install -U 'mcp[cli]'"
        )

    if not config.PUBLIC_URL:
        raise SystemExit(
            "PCEXPRESS_PUBLIC_URL is not set. HTTP mode needs your server's public "
            "https:// origin (e.g. https://pcexpress.example.com, no trailing slash) "
            "to build correct OAuth discovery metadata."
        )

    if not config.TOKEN_SECRET:
        raise SystemExit(
            "PCEXPRESS_TOKEN_SECRET is not set. Generate one with "
            "`python scripts/generate_secret.py` and put it in your .env -- "
            "this is what encrypts every tenant's PC Express credentials "
            "into the tokens this server issues to Claude."
        )

    app = build_http_app()
    host = os.environ.get("PCEXPRESS_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("PCEXPRESS_HTTP_PORT", "8090"))
    uvicorn.run(app, host=host, port=port)


def build_http_app():
    """Compose the Streamable HTTP + OAuth Starlette app (no server started).

    Split out from main_http() so tests can exercise the *real* composed
    app -- including the actual mcp.streamable_http_app(), not a stand-in --
    with its lifespan properly driven via
    `async with app.router.lifespan_context(app): ...`, the same mechanism
    uvicorn uses. This is deliberately not folded back into main_http():
    that function also validates env vars and calls uvicorn.run(), neither
    of which a test should do.
    """
    from contextlib import asynccontextmanager

    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
    from starlette.applications import Starlette
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.routing import Route

    from . import oauth_server
    from mcp.server.transport_security import TransportSecuritySettings

    # See main_http's docstring and README "Hosting on a VPS with a public
    # IP" for why this is disabled: it's a separate check from our own auth
    # that otherwise rejects every request arriving through a reverse proxy
    # with a real hostname, since streamable_http_app() auto-enables a
    # 127.0.0.1/localhost-only Host-header allowlist whenever its `host`
    # param is left at default. Confirmed against a real deployment, not
    # assumed.
    #
    # No auth params passed here -- despite MCPServer.__init__ accepting
    # auth=/token_verifier=/auth_server_provider=, streamable_http_app()
    # itself does NOT (confirmed against the real signature after first
    # assuming otherwise and hitting a TypeError). Since `mcp` is a
    # module-level singleton holding every already-registered @mcp.tool(),
    # reconstructing it with those constructor params isn't an option
    # either -- a second MCPServer instance would have zero tools. Instead,
    # the same three SDK classes MCPServer would have wired in internally
    # are composed by hand below, wrapping this plain (unauthenticated)
    # inner_mcp_app -- functionally the same outcome, just assembled
    # ourselves instead of through MCPServer's constructor-time convenience
    # path, which isn't reachable here.
    inner_mcp_app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )

    # TokenVerifier -> BearerAuthBackend -> AuthenticationMiddleware sets
    # scope["user"]; AuthContextMiddleware copies that into a contextvar
    # get_access_token() reads from anywhere in the request's call stack
    # (including inside a tool function body -- see _current_tenant()
    # above); RequireAuthMiddleware actually rejects unauthenticated
    # requests with 401 + WWW-Authenticate. Order matters: authenticate,
    # then make it contextually available, then enforce it -- innermost
    # (closest to inner_mcp_app) to outermost matches that sequence read
    # backwards, since each layer wraps the previous one.
    protected_mcp_app = RequireAuthMiddleware(
        inner_mcp_app, required_scopes=[], resource_metadata_url=f"{config.PUBLIC_URL}/.well-known/oauth-protected-resource"
    )
    protected_mcp_app = AuthContextMiddleware(protected_mcp_app)
    protected_mcp_app = AuthenticationMiddleware(
        protected_mcp_app, backend=BearerAuthBackend(oauth_server.MultiTenantTokenVerifier())
    )

    # inner_mcp_app's own lifespan is what starts its StreamableHTTPSessionManager's
    # task group (mcp/server/lowlevel/server.py wires
    # lifespan=lambda app: session_manager.run() into that Starlette instance
    # specifically). Starlette does NOT propagate ASGI "lifespan" scope
    # events into apps reached only via routing/Mount -- only the outermost
    # app's own lifespan ever runs. Since inner_mcp_app is mounted (not the
    # top-level app uvicorn.run() is given), its lifespan would silently
    # never fire, and every real request would 500 with "Task group is not
    # initialized" the first time it touched the session manager. Confirmed
    # by hitting exactly that against the real deployment (see
    # tests/test_http_app.py for the regression test). Fix: explicitly enter
    # inner_mcp_app's own lifespan from our outer app's lifespan.
    @asynccontextmanager
    async def _combined_lifespan(_app):
        async with inner_mcp_app.router.lifespan_context(inner_mcp_app):
            yield

    app = Starlette(
        routes=[
            Route("/health", _health),
            Route("/.well-known/oauth-authorization-server", oauth_server.authorization_server_metadata),
            Route("/.well-known/oauth-protected-resource", oauth_server.protected_resource_metadata),
            Route("/authorize", oauth_server.authorize_get, methods=["GET"]),
            Route("/authorize", oauth_server.authorize_post, methods=["POST"]),
            Route("/token", oauth_server.token_endpoint, methods=["POST"]),
        ],
        lifespan=_combined_lifespan,
    )
    # Mount the manually auth-wrapped MCP app at "/" last, as a catch-all --
    # Starlette matches routes in order, so the explicit routes above always
    # win for their exact paths.
    app.router.mount("/", protected_mcp_app)
    return app


def main() -> None:
    import os
    import sys

    # Belt-and-suspenders: every state file this process writes is already
    # explicitly chmod'd to 0600/0700 after creation, but os.makedirs/open
    # briefly create things at default (umask-based) permissions first. A
    # restrictive process-wide umask closes that window everywhere at once,
    # for this process and anything it forks, rather than relying solely on
    # each individual call site's explicit chmod. Set here (the actual
    # entrypoint), not at import time, since mutating process-global umask
    # as an import side effect would be surprising for anything embedding
    # this package as a library. scripts/login.py -- a separate entrypoint
    # that also writes auth_state.json -- sets this too.
    os.umask(0o077)

    if "--http" in sys.argv or os.environ.get("PCEXPRESS_HTTP") == "1":
        main_http()
    else:
        mcp.run()


if __name__ == "__main__":
    main()
