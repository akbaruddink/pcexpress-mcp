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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import ToolAnnotations
from typing_extensions import NotRequired, TypedDict

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
def _base_code(code: Optional[str]) -> Optional[str]:
    """`20028593001_EA` -> `20028593001` (PC's articleNumber)."""
    return code.split("_")[0] if code else code


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

    A bare code (no `_EA`/`_KG`/`_C04` suffix, e.g. a raw articleNumber)
    matches the product whose suffixed code starts with it -- the suffix
    can't be derived (tomatoes need `_KG`, lemons `_EA`), only looked up.
    """
    products: list[dict] = []
    not_found: list[str] = []
    for code in product_codes:
        raw = api.search_products(code.split("_")[0], store_id, size=5)
        match = next(
            (p for p in raw.get("results", []) or [] if p.get("code") == code or ("_" not in code and _base_code(p.get("code")) == code)),
            None,
        )
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

    If you haven't already called get_purchase_history this session,
    call it first -- once is enough (don't call it before every search),
    but if you're ever unsure whether you already have, call it again
    rather than skip it; it's cheap, and assuming you already know what
    this household buys when you don't is the worse mistake. Use that
    context when choosing a query or picking products to recommend.

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
    api = _get_api(session.banner)
    if not _ensure_active_store(api, session):
        return _no_active_store()

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
        results = [_simplify_product(p) for p in (raw.get("results") or [])[:size]]
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
            results = [_simplify_product(p) for p in (raw.get("results") or [])[:size]]
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


class NoCartError(PcxApiError):
    """The active banner has no open cart (e.g. just after checkout)."""


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PcidAuthError):
        return {"error": "auth_required", "message": str(exc)}
    if isinstance(exc, NoCartError):
        return {"error": "no_cart", "message": str(exc)}
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
            session.cart_id = carts[0].get("id")
    _save_session(session)
    if not session.cart_id:
        raise NoCartError(
            f"No open cart on banner {session.banner!r} -- normal right after checkout. PC Express creates a "
            "new one when the app or website is next opened; no API to create one is known, so ask the user to "
            "open the PC Express app once, then retry."
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


def _ensure_active_store(
    api: PCExpressAPI, session: session_state.SessionState, cart_store_id: Optional[str] = None
) -> Optional[str]:
    """The active store, falling back to the store this banner's cart is
    bound to (`cart_store_id` if the caller already has it). HTTP mode keeps
    the active store in memory only, so a server restart drops it
    mid-session (seen in a real session: gone hours later, while get_cart
    still worked) -- the cart's binding is the right default and survives
    restarts because it lives in PC Express itself.
    """
    if not session.store_id:
        try:
            session.store_id = cart_store_id or _cart_bound_store(_get_cart_healing(api, session))
        except (PcidAuthError, PcxApiError):
            return None
        if session.store_id:
            _save_session(session)
    return session.store_id


def _no_active_store() -> dict:
    return {"error": "no_active_store", "message": "No active store and no cart to infer it from -- call set_active_store."}


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


def _unit_price(prices: dict, value_key: str) -> Optional[dict]:
    """`{"value", "per"}` from `prices.comparisonPrices[0]`. Search results
    key the number as "value", cart entries as "price" (confirmed live)."""
    comparison = (prices.get("comparisonPrices") or [{}])[0]
    if comparison.get(value_key) is None:
        return None
    unit = comparison.get("unit")
    return {"value": comparison[value_key], "per": f"{comparison.get('quantity')}{unit}" if unit else None}


def _simplify_product(p: dict) -> dict:
    """A search result, trimmed. Verified live: no nutrition/ingredients
    exist here (`ingredients` always null; `api_client.get_product` is
    broken) -- pair `barcode` with Open Food Facts instead. `unit_price`
    is the field for value comparisons across package sizes. `description`
    is cut to ~400 chars. `image_urls` keeps one size per distinct photo
    (products carry 3-9+ angles). `aisle` and `mopDealPrice` were dropped:
    null on every real result seen (91 of 91 in one session).
    """
    prices = p.get("prices") or {}
    price = prices.get("price") or {}
    was_price = prices.get("wasPrice") or {}
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
        "stock_status": p.get("stockStatus"),
        "price": price.get("value"),
        "regular_price": was_price.get("value"),
        "unit_price": _unit_price(prices, "value"),
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
    `offer.promotionLabel`) that just weren't extracted before (see
    `_unit_price` for the "price"-vs-"value" key gotcha).

    Cart-level `store_id` (the binding), `fulfillment_method`, booked
    `slot` and the `totals` breakdown all come from the cart's own
    `orders[].fulfillment`/`totals` (confirmed live). `regular_price` is
    only reported alongside a `deal_text`: without a deal it was seen
    disagreeing with the charged price, in both directions, for unknown
    reasons. `_KG` lines are `estimated` -- the charged price is set when
    the item is weighed at picking.
    """
    orders = cart.get("orders") or []
    entries: list[dict] = []
    totals: dict[str, Optional[float]] = {k: None for k in _CART_TOTAL_FIELDS}
    for order in orders:
        for key, raw_key in _CART_TOTAL_FIELDS.items():
            value = (order.get("totals") or {}).get(raw_key)
            if value is not None:
                totals[key] = round((totals[key] or 0) + value, 2)
        for entry in order.get("entries", []) or []:
            offer = entry.get("offer") or {}
            product = offer.get("product") or {}
            prices = entry.get("prices") or {}
            total_sale_price = prices.get("totalSalePrice")
            deal_badge = (offer.get("badges") or {}).get("dealBadge") or {}
            deal_text = deal_badge.get("text") or offer.get("promotionLabel")
            code = offer.get("id") or product.get("id")
            entries.append(
                {
                    "code": code,
                    "name": product.get("name"),
                    "brand": product.get("brand"),
                    "package_size": product.get("sizeLabel"),
                    "quantity": entry.get("quantity"),
                    "total_price": total_sale_price if total_sale_price is not None else prices.get("totalRegularPrice"),
                    "regular_price": prices.get("totalRegularPrice") if deal_text else None,
                    "unit_price": _unit_price(prices, "price"),
                    "deal_text": deal_text,
                    "estimated": bool(code and code.endswith("_KG")),
                    "photo_markdown": _markdown_image(product.get("name"), product.get("primaryImage")),
                }
            )
    fulfillment = (orders[0].get("fulfillment") or {}) if orders else {}
    window = (fulfillment.get(fulfillment.get("type") or "") or {}).get("timeWindow") or {}
    return {
        "cart_id": cart.get("id"),
        "status": cart.get("status"),
        "store_id": _cart_bound_store(cart),
        "fulfillment_method": _cart_mode(cart),
        "slot": (
            {
                "start": window["startTime"],
                "end": window.get("endTime"),
                "hold_expires_at": window.get("slotExpiryDateTime"),
                "hold_active": _hold_active(window.get("slotExpiryDateTime")),
            }
            if window.get("startTime")
            else None
        ),
        "min_cart_value": cart.get("minCartValue"),
        "modified_time": cart.get("modifiedTime"),
        "item_count": len(entries),
        "units": sum(e["quantity"] or 0 for e in entries),
        "items": entries,
        "totals": totals,
    }


# Output key -> raw `orders[].totals` key (confirmed live on a real cart).
_CART_TOTAL_FIELDS = {
    "subtotal": "subTotal",
    "tax": "totalTax",
    "delivery_fee": "totalDeliveryFee",
    "service_fee": "totalServiceFee",
    "tip": "totalDeliveryTip",
    "discounts": "totalDiscounts",
    "total": "totalPrice",
}


def _hold_active(expires_at: Optional[str]) -> Optional[bool]:
    """Whether a slot hold is still live. An expired hold stays on the cart
    (seen live: the next morning, still listed), so it can't be trusted by
    presence alone."""
    try:
        return datetime.fromisoformat(expires_at.replace("Z", "+00:00")) > datetime.now(timezone.utc) if expires_at else None
    except ValueError:
        return None


def _cart_mode(cart: dict) -> Optional[str]:
    """"delivery" or "pickup", from `orders[0].fulfillment.type` -- "courier"
    for delivery (confirmed live); pickup's exact value is unconfirmed, so
    anything mentioning pickup counts."""
    orders = cart.get("orders") or []
    ftype = (((orders[0].get("fulfillment") or {}).get("type") if orders else None) or "").lower()
    if ftype in ("courier", "delivery"):
        return "delivery"
    return "pickup" if "pickup" in ftype else None


def _simplify_order_summary(order: dict) -> dict:
    """A list row. `total_at_placement` stays at the placed amount: seen
    unchanged after the order's own detail total dropped at fulfilment.
    Fee fields are omitted -- null on every row of a 300-order history."""
    return {
        "order_id": order.get("id"),
        "placed": order.get("placed"),
        "store": order.get("store"),
        "order_type": order.get("orderType"),
        "fulfillment_type": order.get("fulfillmentType"),
        "total_at_placement": order.get("total"),
    }


def _adjustment_kind(product: dict) -> Optional[str]:
    """Driver tips and loyalty stamps arrive as order lines with product
    codes but no photo (confirmed live); they aren't groceries."""
    name = (product.get("productName") or "").upper()
    if "DRIVER TIP" in name:
        return "tip"
    if "STAMP" in name and not product.get("primaryImage"):
        return "stamps"
    return None


def _order_line_code(product: dict) -> Optional[str]:
    """`product.id` is the suffixed code cart tools need (`20852143_KG`),
    confirmed live; `articleNumber` is the bare one they silently ignore."""
    return product.get("id") or product.get("articleNumber")


def _simplify_order_detail(detail: dict) -> dict:
    """A single order, trimmed (the raw response is ~85% embedded product
    and store objects).

    Confirmed live on real orders, so don't expect more than this:
    `status` and every line's availability are null upstream, so there's
    no progress tracking and no reason for a missing line (out of stock vs
    removed at review). For an in-flight order PC Express can return the
    totals with no lines at all (`lines_available: false`). Totals can
    change after fulfilment (weighed items, removed lines). Tips and stamp
    lines go in `adjustments`, not `items`. `points_value` assumes PC
    Optimum's 1,000 points = $1.
    """
    od = detail.get("orderDetails") or {}
    booking = od.get("booking") or {}
    pickup_location = booking.get("pickupLocation") or {}
    items: list[dict] = []
    adjustments: list[dict] = []
    for e in od.get("entries", []) or []:
        product = e.get("product") or {}
        line = {
            "code": _order_line_code(product),
            "name": product.get("productName"),
            "brand": product.get("brand"),
            "quantity": e.get("quantity"),
            "unit_price": e.get("unitPrice"),
            "total_price": e.get("totalPrice"),
        }
        kind = _adjustment_kind(product)
        if kind:
            adjustments.append({"kind": kind, **line})
            continue
        if e.get("weight"):
            line["weight_kg"] = e["weight"]
        items.append(line)
    try:
        points_redeemed = float(detail.get("pointsRedeemed") or 0)
    except (TypeError, ValueError):
        points_redeemed = None
    result = {
        "order_number": od.get("orderNumber"),
        "order_type": od.get("orderType"),
        "status": od.get("status") or detail.get("statusDisplay") or od.get("deliveryStatus"),
        "store_id": pickup_location.get("storeId"),
        "store_name": pickup_location.get("name") or od.get("bannerName"),
        "fulfillment_type": pickup_location.get("pickupType"),
        "slot_start": booking.get("pickupStartDate"),
        "slot_end": booking.get("pickupEndDate"),
        "products_total": round(sum(i["total_price"] or 0 for i in items), 2),
        "sub_total": od.get("subTotal"),
        "tip": round(sum(a["total_price"] or 0 for a in adjustments if a["kind"] == "tip"), 2),
        "delivery_fee": booking.get("deliveryFee"),
        "service_fee": booking.get("serviceFee"),
        "tax": od.get("totalTax"),
        "discounts": od.get("totalDiscounts"),
        "points_redeemed": points_redeemed,
        "points_value": round(points_redeemed / 1000, 2) if points_redeemed else 0.0,
        "points_earned": detail.get("pointsEarned"),
        "total_price": od.get("totalPriceWithTax") or od.get("totalPrice"),
        "item_count": len(items),
        "units": sum(i["quantity"] or 0 for i in items),
        "items": items,
        "adjustments": adjustments,
    }
    if not items and not adjustments:
        result["lines_available"] = False
    return result


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
        destructive_hint=True,  # re-prices or drops items in a cart other people may share
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def switch_cart_store(store_id: str, postal_code: str, confirm: bool = False) -> dict:
    """Re-bind this banner's cart to a different store -- the fix for a
    `cart_store_mismatch` error. Also makes `store_id` the active store.

    The cart is one per banner and shared by everyone using this account
    (e.g. two households at different stores), so this changes their cart
    too: items carry over, re-priced at the new store (or dropped if it
    doesn't stock them). If the cart has items, this refuses without
    `confirm=True` and names the store it's bound to -- check with the user
    first. The response lists `previous_store_id`, `items_repriced` and
    `items_dropped`.

    `postal_code` must be a delivery address the store serves; the store's
    own postal code works. (Shape from a real app request capture -- see
    docs/RESEARCH.md "Cart is bound to a single store".)
    """
    session = _load_session()
    api = _get_api(session.banner)
    try:
        # Always fresh: a stale cached cart_id makes set_cart_fulfillment
        # fail with a 503/422 rather than a 404 the healing helpers catch.
        _rediscover_cart(api, session)
        before = _simplify_cart(api.get_cart(session.cart_id))
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    if before["store_id"] == store_id:
        session.store_id = store_id
        _save_session(session)
        return {**before, "message": f"The cart is already bound to store {store_id}; nothing changed."}
    if before["item_count"] and not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                f"This banner's cart is bound to store {before['store_id']} and holds {before['item_count']} "
                f"items, possibly someone else's on this account. Switching re-prices them at store {store_id} "
                "for everyone. Confirm with the user, then call again with confirm=True."
            ),
            "previous_store_id": before["store_id"],
            "items": [{"code": i["code"], "name": i["name"]} for i in before["items"]],
        }
    try:
        serviceability = api.get_delivery_serviceability(postal_code)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    location_id = _find_fulfillment_location_id(serviceability, session.banner, store_id)
    if not location_id:
        return {
            "error": "store_not_serviceable",
            "message": (
                f"No delivery fulfillment location for store {store_id!r} on banner {session.banner!r} from "
                f"postal code {postal_code!r} -- check the store id and that it delivers to that postal code."
            ),
        }
    try:
        raw = api.set_cart_fulfillment(session.cart_id, location_id, postal_code)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)

    session.store_id = store_id
    _save_session(session)
    result = _simplify_cart(raw.get("cart", raw))
    after = {i["code"]: i["total_price"] for i in result["items"]}
    result["previous_store_id"] = before["store_id"]
    result["switched_to_store"] = store_id
    result["items_repriced"] = [i["code"] for i in before["items"] if i["code"] in after and after[i["code"]] != i["total_price"]]
    result["items_dropped"] = [i["code"] for i in before["items"] if i["code"] not in after]
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


_OFF_CACHE: dict[str, Optional[dict]] = {}


def _enrich_with_nutrition(results: list[dict], client: httpx.Client) -> dict[str, Any]:
    """Attach Open Food Facts data (see `_simplify_nutrition`) to each
    result with a `barcode`, in place. Lookups are cached per barcode for
    the life of the process and paced ~1s apart (OFF allows 15/min/IP).
    Stops early, without raising, if rate-limited anyway. `with_data`/
    `with_ingredients` count useful answers, not requests -- most OFF
    records found for these products have no ingredient list.
    """
    with_data = with_ingredients = 0
    rate_limited = False
    made_a_request = False
    for product in results:
        barcode = product.get("barcode")
        if not barcode:
            continue
        if barcode not in _OFF_CACHE:
            if made_a_request:
                time.sleep(1)
            made_a_request = True
            try:
                _OFF_CACHE[barcode] = nutrition_client.fetch_nutrition(barcode, client=client)
            except nutrition_client.OpenFoodFactsRateLimited:
                rate_limited = True
                break
        off_product = _OFF_CACHE[barcode]
        if off_product is None:
            product["nutrition"] = {"found": False}
            continue
        product["nutrition"] = _simplify_nutrition(off_product)
        with_data += 1
        with_ingredients += bool(product["nutrition"].get("ingredients_text"))
    return {"with_data": with_data, "with_ingredients": with_ingredients, "rate_limited": rate_limited}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Products", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )
)
def search_products(query: str, size: int = 20, offset: int = 0, include_nutrition: bool = False) -> dict:
    """Search the product catalog at the active store.

    If you haven't already called get_purchase_history this session,
    call it first -- once is enough (don't call it before every search),
    but if you're ever unsure whether you already have, call it again
    rather than skip it; it's cheap, and assuming you already know what
    this household buys when you don't is the worse mistake. Use that
    context when choosing a query or ranking/recommending results.

    Returns at most `size` results, in PC Express's own ranking. `code` is
    what the cart tools take; `sku` (bare article number) and `barcode`
    (UPC) are for cross-referencing. Each result's `photo_markdown` is a
    ready-to-paste `![name](url)` -- include it in your reply so the photo
    renders. `offset` pages for real; `total_results` is an estimate.
    Ranking is noisy for vague queries: if the obvious product isn't there,
    try a more specific query or the next page.

    No filter/sort parameters exist -- every attempt to make the API's
    advertised facets work failed (docs/RESEARCH.md "Search filters/sort");
    filter the results yourself.

    PC Express has no nutrition or ingredient data. `include_nutrition=True`
    attaches Open Food Facts data per result: at most 15 results (OFF
    allows 15 lookups/min/IP), ~1s per uncached lookup, and coverage is
    patchy (`nutrition_enrichment.with_data`/`with_ingredients` say how
    patchy; weighed items never have a barcode). Leave it off unless the
    request needs nutrition.
    """
    session = _load_session()
    api = _get_api(session.banner)
    if not _ensure_active_store(api, session):
        return _no_active_store()

    size, size_capped = _cap_size_for_nutrition(size, include_nutrition)
    try:
        raw = api.search_products(query, session.store_id, cart_id=session.cart_id, size=size, from_=offset)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    # PC Express often returns more rows than `size` (seen: 3 -> 11, 5 ->
    # 13), which inflated payloads and nutrition lookups. Its first rows are
    # its own ranking, so cut there. image_urls is dropped here (the
    # widget keeps it): photo_markdown already carries the first photo.
    results = [_simplify_product(p) for p in (raw.get("results") or [])[:size]]
    for r in results:
        r.pop("image_urls")
    total_results = (raw.get("pagination") or {}).get("totalResults")

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
            "size_capped": size_capped,
            **summary,
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
    """An Open Food Facts product, trimmed to what helps "should I buy
    this". `nutriscore_grade` (a-e) and `nova_group` (1-4, processing) are
    the headline signals; deliberately no invented 0-100 Yuka-style score
    (that's Yuka's proprietary weighting, not OFF data). `nutrient_levels`
    is OFF's own traffic-light rating; `additives` has no risk levels (OFF
    doesn't provide them). Values are as contributed to OFF, rounded:
    `per_100g_consistent: false` marks records whose energy doesn't match
    their macros, so the macros probably aren't really per 100 g.
    `ingredients_language` says what language the ingredient list is in.
    """
    nutriments = product.get("nutriments") or {}
    per_100g = {key: _round2(nutriments.get(raw)) for key, raw in _PER_100G_FIELDS.items()}
    additive_codes = product.get("additives_tags") or []
    # OFF tags both a parent and its variant (en:e262 + en:e262ii); keep the specific one.
    additive_codes = [c for c in additive_codes if not any(o != c and o.startswith(c) for o in additive_codes)]
    ingredients_en = product.get("ingredients_text_en")
    return {
        "product_name": product.get("product_name"),
        "brands": product.get("brands"),
        "quantity": product.get("quantity"),
        "nutriscore_grade": product.get("nutriscore_grade"),
        "nova_group": product.get("nova_group"),
        "ecoscore_grade": product.get("ecoscore_grade"),
        "ingredients_text": ingredients_en or product.get("ingredients_text"),
        "ingredients_language": "en" if ingredients_en else product.get("ingredients_lc"),
        "allergens": product.get("allergens"),
        "allergen_status": _allergen_status(product.get("allergens_tags") or [], product.get("traces_tags") or []),
        "dietary_flags": _dietary_flags(product.get("ingredients_analysis_tags") or []),
        "nutrient_levels": {k.replace("-", "_"): v for k, v in (product.get("nutrient_levels") or {}).items()},
        "additives": {"count": len(additive_codes), "codes": additive_codes},
        "per_100g": per_100g,
        "per_100g_consistent": _energy_matches_macros(per_100g),
    }


_PER_100G_FIELDS = {
    "energy_kcal": "energy-kcal_100g",
    "protein_g": "proteins_100g",
    "fat_g": "fat_100g",
    "saturated_fat_g": "saturated-fat_100g",
    "carbohydrates_g": "carbohydrates_100g",
    "sugars_g": "sugars_100g",
    "fiber_g": "fiber_100g",
    "salt_g": "salt_100g",
}


def _round2(value: Any) -> Any:
    return round(value, 2) if isinstance(value, (int, float)) else value


def _energy_matches_macros(per_100g: dict) -> Optional[bool]:
    """False when stated energy is >25% off 4p+4c+9f -- OFF contributors
    sometimes enter per-serving macros beside per-100g energy (seen: a
    cracker at 467 kcal/100g with fat_100g 6, i.e. per 30g serving). None
    if anything needed is missing."""
    values = [per_100g.get(k) for k in ("energy_kcal", "protein_g", "carbohydrates_g", "fat_g")]
    if any(not isinstance(v, (int, float)) for v in values) or not values[0]:
        return None
    energy, protein, carbs, fat = values
    return abs(4 * protein + 4 * carbs + 9 * fat - energy) <= 0.25 * energy


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
    """View the current cart: items, the store it's bound to, fulfillment
    method, booked slot (if any) and a totals breakdown.

    `status: "NO_CART"` means this banner has no open cart -- normal right
    after checkout, not an error. Each item carries `photo_markdown` --
    paste it directly into your reply so the cart actually looks like a cart.
    """
    session = _load_session()
    api = _get_api(session.banner)
    try:
        raw = _get_cart_healing(api, session)
    except NoCartError as exc:
        return {"status": "NO_CART", "item_count": 0, "items": [], "message": str(exc)}
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    return _simplify_cart(raw)


class CartItem(TypedDict):
    product_code: str
    quantity: NotRequired[int]
    fulfillment_method: NotRequired[Literal["pickup", "delivery"]]


class QuantityUpdate(TypedDict):
    product_code: str
    quantity: int


def _resolve_codes(
    api: PCExpressAPI, store_id: Optional[str], codes: list[str], cart_codes: list[str]
) -> tuple[dict[str, str], list[dict]]:
    """Map requested codes to the suffixed form cart writes need
    (`20028593001` -> `20028593001_EA`). PC Express silently ignores a bare
    code (confirmed live), so a bare code is matched against the cart
    first, then looked up in the catalog. Returns ({requested: resolved},
    rejected)."""
    in_cart = {_base_code(c): c for c in cart_codes}
    resolved: dict[str, str] = {}
    to_lookup: list[str] = []
    for code in codes:
        if "_" in code:
            resolved[code] = code
        elif code in in_cart:
            resolved[code] = in_cart[code]
        else:
            to_lookup.append(code)
    rejected: list[dict] = []
    if to_lookup:
        found, not_found = _lookup_products_by_code(api, store_id, to_lookup) if store_id else ([], to_lookup)
        by_base = {_base_code(p["code"]): p["code"] for p in found}
        for code in to_lookup:
            if code in by_base:
                resolved[code] = by_base[code]
        rejected = [
            {"code": c, "reason": "unknown_code", "detail": "No product with this code at the active store -- use a code from search_products."}
            for c in not_found
        ]
    return resolved, rejected


def _apply_cart_entries(
    api: PCExpressAPI, session: session_state.SessionState, entries: dict[str, dict[str, Any]], rejected: list[dict]
) -> dict:
    """Send `entries`, then report per code whether the cart actually
    changed: PC Express returns a normal cart for codes it ignores (bare or
    wrong-suffix codes, unknown products), so the only proof of a write is
    re-reading the quantity. Errors if nothing applied."""
    if not entries:
        return {"error": "nothing_applied", "message": "None of the product codes could be used -- see `rejected`.", "rejected": rejected}
    try:
        raw = _update_cart_healing(api, session, entries)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    result = _simplify_cart(raw)
    quantities = {i["code"]: i["quantity"] or 0 for i in result["items"]}
    applied: list[str] = []
    for code, entry in entries.items():
        got = quantities.get(code, 0)
        if got == entry["quantity"]:
            applied.append(code)
            continue
        rejection = {
            "code": code,
            "reason": "not_applied",
            "detail": f"cart quantity is {got}, expected {entry['quantity']} -- wrong suffix (_EA/_KG/_C01...) or out of stock.",
        }
        if entry["quantity"] and session.store_id:
            try:
                found, _ = _lookup_products_by_code(api, session.store_id, [_base_code(code)])
            except (PcidAuthError, PcxApiError):
                found = []
            if found and found[0]["code"] != code:
                rejection["suggested_code"] = found[0]["code"]
        rejected.append(rejection)
    result["applied"] = applied
    result["rejected"] = rejected
    if not applied:
        result["error"] = "nothing_applied"
        result["message"] = "The cart did not change -- see `rejected`."
    return result


def _load_cart_for_write(api: PCExpressAPI, session: session_state.SessionState) -> dict:
    """Current cart, simplified -- every write reads it first for quantities,
    mode and the bound store. Raises like _get_cart_healing."""
    return _simplify_cart(_get_cart_healing(api, session))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add to Cart",
        read_only_hint=False,
        destructive_hint=False,
        # Increments: calling it twice adds twice.
        idempotent_hint=False,
        open_world_hint=False,
    )
)
def add_to_cart(items: list[CartItem]) -> dict:
    """Add products to the cart. `quantity` (default 1) is added on top of
    whatever is already in the cart; use update_quantity to set an exact
    quantity.

    If you haven't already called get_purchase_history this session,
    call it first -- once is enough (don't call it before every add),
    but if you're ever unsure whether you already have, call it again
    rather than skip it; it's cheap, and assuming you already know what
    this household buys when you don't is the worse mistake. This
    matters most when choosing or confirming what to add on someone
    else's behalf.

    Bare codes from purchase history or orders (no `_EA`/`_KG` suffix) are
    resolved automatically. `fulfillment_method` defaults to the cart's
    current one. Check `rejected` in the response: codes PC Express
    ignored are listed there, and the call errors if nothing was added.

    The returned cart's items carry `photo_markdown` -- include it in your
    reply when confirming what was added.
    """
    if not items:
        return {"error": "invalid_items", "message": "items must be a non-empty list."}
    for item in items:
        if not item.get("product_code"):
            return {"error": "invalid_items", "message": "Each item needs a product_code."}
        if item.get("quantity", 1) <= 0:
            return {
                "error": "invalid_quantity",
                "message": f"quantity for {item['product_code']!r} must be >= 1 (use remove_from_cart to remove).",
            }
    session = _load_session()
    api = _get_api(session.banner)
    try:
        cart = _load_cart_for_write(api, session)
        store_id = _ensure_active_store(api, session, cart["store_id"])
        if not store_id:
            return _no_active_store()
        resolved, rejected = _resolve_codes(api, store_id, [i["product_code"] for i in items], [c["code"] for c in cart["items"]])
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    current = {i["code"]: i["quantity"] or 0 for i in cart["items"]}
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        code = resolved.get(item["product_code"])
        if not code:
            continue
        quantity = (entries[code]["quantity"] if code in entries else current.get(code, 0)) + item.get("quantity", 1)
        entries[code] = {
            "quantity": int(quantity) if float(quantity).is_integer() else quantity,
            "fulfillmentMethod": item.get("fulfillment_method") or cart["fulfillment_method"] or "pickup",
            "sellerId": store_id,
        }
    return _apply_cart_entries(api, session, entries, rejected)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Remove from Cart", read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    )
)
def remove_from_cart(product_codes: list[str]) -> dict:
    """Remove one or more products from the cart entirely, in a single call.

    Bare codes match the cart's suffixed ones. Codes not in the cart come
    back in `rejected`. The returned cart's remaining items carry
    `photo_markdown` -- include it when confirming the cart's new contents.
    """
    if not product_codes:
        return {"error": "invalid_items", "message": "product_codes must be a non-empty list."}
    return update_quantity([{"product_code": c, "quantity": 0} for c in product_codes])


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Cart Quantity",
        read_only_hint=False,
        destructive_hint=True,  # can reduce/zero out quantity, i.e. lose cart data
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def update_quantity(items: list[QuantityUpdate]) -> dict:
    """Set exact cart quantities for one or more products in a single call
    (quantity=0 removes that item).

    Bare codes are resolved like add_to_cart's; check `rejected`. The
    returned cart's items carry `photo_markdown` -- include it when
    confirming the cart's new contents.
    """
    if not items:
        return {"error": "invalid_items", "message": "items must be a non-empty list."}
    for item in items:
        if not item.get("product_code") or item.get("quantity") is None:
            return {"error": "invalid_items", "message": "Each item needs product_code and quantity."}
        if item["quantity"] < 0:
            return {"error": "invalid_quantity", "message": f"quantity for {item['product_code']!r} must be >= 0."}
    session = _load_session()
    api = _get_api(session.banner)
    try:
        cart = _load_cart_for_write(api, session)
        in_cart = {_base_code(c["code"]): c["code"] for c in cart["items"]}
        adds = [i["product_code"] for i in items if i["quantity"] > 0]
        store_id = _ensure_active_store(api, session, cart["store_id"]) if adds else session.store_id
        if adds and not store_id:
            return _no_active_store()
        resolved, rejected = _resolve_codes(api, store_id, adds, list(in_cart.values()))
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        requested = item["product_code"]
        if item["quantity"] > 0:
            code, seller_id = resolved.get(requested), store_id
        else:
            # Confirmed live: PC Express rejects even a removal with
            # SELLER_ID_MISMATCH unless sellerId is the cart's real binding.
            code, seller_id = in_cart.get(_base_code(requested)), cart["store_id"] or session.store_id
            if code is None or ("_" in requested and code != requested):
                rejected.append({"code": requested, "reason": "not_in_cart"})
                continue
        if not code:
            continue
        entry: dict[str, Any] = {"quantity": item["quantity"], "fulfillmentMethod": cart["fulfillment_method"] or "pickup"}
        if seller_id:
            entry["sellerId"] = seller_id
        entries[code] = entry
    return _apply_cart_entries(api, session, entries, rejected)


# --- fulfillment slots -----------------------------------------------------------


class NoDeliveryTarget(Exception):
    """The cart isn't a delivery cart with a location and address set."""


def _cart_delivery_target(cart: dict) -> tuple[Optional[str], Optional[str]]:
    """(fulfillmentLocationId, delivery postal code) of a *delivery* cart,
    from `orders[0].fulfillment.courier`; (None, None) for anything else --
    a pickup cart can carry a stale courier block."""
    if _cart_mode(cart) != "delivery":
        return None, None
    courier = (cart["orders"][0].get("fulfillment") or {}).get("courier") or {}
    return courier.get("fulfillmentLocationId"), (courier.get("deliveryAddress") or {}).get("postalCode")


def _load_slots(api: PCExpressAPI, session: session_state.SessionState) -> tuple[dict, str, str, list[dict]]:
    """(raw cart, location id, postal code, raw timeslots). Raises
    PcxApiError (incl. NoCartError) like the cart helpers, or
    NoDeliveryTarget."""
    cart = _get_cart_healing(api, session)
    location_id, postal_code = _cart_delivery_target(cart)
    if not location_id or not postal_code:
        raise NoDeliveryTarget(
            "Only delivery slots are supported, and this cart isn't set up for delivery (no delivery "
            "location and address). Pickup slots have to be picked in the PC Express app."
        )
    raw = api.get_delivery_slots(session.cart_id, location_id, postal_code)
    return cart, location_id, postal_code, (raw.get(location_id) or {}).get("timeslots") or []


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Available Delivery Slots",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def get_available_slots(date: Optional[str] = None, days: int = 2) -> dict:
    """Available delivery slots for the cart's store and delivery address,
    with this account's own fees (e.g. $0 with a delivery pass).

    Returns the first `days` dates that have availability, or just `date`
    (YYYY-MM-DD) if given; `dates_available` lists every bookable date
    (~2 weeks out). Times are the store's local time. `booked` is the
    slot on the cart, if any -- check `hold_active`: an expired hold stays
    listed. Book one with book_delivery_slot.
    """
    session = _load_session()
    api = _get_api(session.banner)
    try:
        cart, location_id, _, timeslots = _load_slots(api, session)
    except NoDeliveryTarget as exc:
        return {"error": "slots_unavailable", "reason": str(exc)}
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    by_date: dict[str, list[dict]] = {}
    for s in timeslots:
        if s.get("available"):
            by_date.setdefault(s["date"], []).append(
                {"start": s.get("startTime"), "end": s.get("endTime"), "fee": s.get("charge"), "type": (s.get("slotType") or "").removeprefix("DELIVERY_")}
            )
    dates = sorted(by_date)
    return {
        "location_id": location_id,
        "booked": _simplify_cart(cart)["slot"],
        "dates_available": dates,
        "slots": {d: by_date.get(d, []) for d in ([date] if date else dates[: max(days, 1)])},
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Book Delivery Slot",
        read_only_hint=False,
        destructive_hint=True,  # releases any slot already held, which someone else may take
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def book_delivery_slot(date: str, start_time: str) -> dict:
    """Hold a delivery slot on the cart: `date` (YYYY-MM-DD) and
    `start_time` (HH:MM, store local) from get_available_slots.

    This is a hold, not an order: it expires (about an hour after booking,
    seen live -- `hold_expires_at`, UTC) unless checkout is completed in the
    PC Express app or website. It replaces any slot already held on this
    banner's cart, which other people on the account may share
    (`previous` shows what was there). Confirm the slot with the user
    first. `warnings` carries cart problems the checkout service reports,
    e.g. a low-stock item.
    """
    session = _load_session()
    api = _get_api(session.banner)
    if not api.banner_info.get("checkout_lob"):
        return {
            "error": "booking_unsupported",
            "message": f"Slot booking isn't verified for banner {session.banner!r} yet -- book in the PC Express app.",
        }
    try:
        cart, location_id, postal_code, timeslots = _load_slots(api, session)
    except NoDeliveryTarget as exc:
        return {"error": "slots_unavailable", "reason": str(exc)}
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    matches = [s for s in timeslots if s.get("date") == date and s.get("startTime") == start_time]
    slot = next((s for s in matches if s.get("available")), matches[0] if matches else None)
    if not slot or not slot.get("available"):
        return {
            "error": "slot_unavailable" if slot else "slot_not_found",
            "message": f"No bookable slot at {date} {start_time} -- pick one from get_available_slots.",
        }
    try:
        raw = api.book_delivery_slot(session.cart_id, date, start_time, slot["endTime"], location_id, postal_code)
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    store_carts = ((raw.get("cart") or {}).get("cart_data") or {}).get("store_carts") or [{}]
    held = ((store_carts[0].get("fulfillment") or {}).get("delivery") or {}).get("time_slot") or {}
    if not held.get("start_time"):
        # A 200 can carry only errors (e.g. the slot filled meanwhile).
        return {"error": "not_booked", "message": "The checkout service didn't hold the slot.", "warnings": _checkout_warnings(raw)}
    return {
        "booked": {"date": date, "start": start_time, "end": slot["endTime"], "fee": slot.get("charge"), "hold_expires_at": held.get("expiry_time")},
        "previous": _simplify_cart(cart)["slot"],
        "warnings": _checkout_warnings(raw),
    }


def _checkout_warnings(raw: dict) -> list[dict]:
    """The checkout service's `errors` (e.g. LOW_STOCK) as short warnings."""
    warnings = []
    for e in raw.get("errors") or []:
        detail = e.get("error_detail") or e.get("details") or {}
        warnings.append({"code": detail.get("error_code") or detail.get("code") or e.get("code"), "message": detail.get("message") or e.get("message")})
    return warnings


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

    No payment-submission API is used here, on purpose. This checks the cart
    and returns the checkout page's own summary (`checkout`: real subtotal,
    tax, fees, tip, total and the held slot, with any `warnings` such as a
    low-stock item) plus `checkout_url` for the user to pay. Requires
    confirm=True so it never fires as a side effect.

    Before presenting `checkout_url`, show a full visual receipt: every
    item's `photo_markdown` from `cart_summary.items`, quantity and price,
    and the `checkout` total. If `cart_summary.slot` is missing or its
    `hold_active` is false, book one first with book_delivery_slot.
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

    result: dict[str, Any] = {
        "status": "ready_for_manual_checkout",
        "message": (
            "This tool does not submit payment. Show the user a full visual receipt (each item's "
            "photo_markdown, quantity, price, and the checkout total) before the checkout link."
        ),
        "checkout_url": api.checkout_page_url(),
        "cart_summary": cart,
    }
    try:
        result["checkout"] = _simplify_checkout(api.get_checkout(session.cart_id))
    except (PcidAuthError, PcxApiError) as exc:
        result["checkout"] = {"error": "checkout_summary_unavailable", "message": str(exc)}
    return result


def _simplify_checkout(raw: dict) -> dict:
    """The checkout page's summary, in dollars (the service uses cents).
    Personal details it carries (address, phone, email, card) are dropped,
    and so is its slot: it's in UTC, while cart_summary.slot has the same
    slot in store-local time."""
    data = (raw.get("checkout") or {}).get("checkout_data") or {}
    charges = data.get("charges") or {}

    def dollars(cents: Any) -> Optional[float]:
        return round(cents / 100, 2) if isinstance(cents, (int, float)) else None

    fees: dict[str, float] = {}
    for f in charges.get("fulfillment_fee_components") or []:
        key = (f.get("fee_component_type") or "OTHER_FEE").lower()
        fees[key] = round(fees.get(key, 0) + (dollars(f.get("calculated_amount_in_cents")) or 0), 2)
    return {
        "fulfillment_type": (data.get("fulfillment") or {}).get("fulfillment_type"),
        "subtotal": dollars(charges.get("subtotal")),
        "fees": fees,
        "tip": dollars(charges.get("tip")),
        "tax": dollars(charges.get("total_tax")),
        "discount": dollars(charges.get("total_discount")),
        "total": dollars(charges.get("total")),
        "max_redeemable_points": charges.get("max_redeemable_points"),
        "warnings": _checkout_warnings(raw),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Order Status", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def get_order_status(order_id: Optional[str] = None, limit: int = 10) -> dict:
    """List recent orders (newest first, `limit` of them), or one order's
    detail by its order number.

    A just-placed order can be missing from the list for hours (seen: 5+
    hours while it was being shopped) -- look it up by order number
    instead. Its detail may have no lines until fulfilment, and totals can
    change after (weighed items, removed lines); `status` is never
    populated by PC Express. Line `code`s work directly with the cart tools.
    """
    if order_id is not None:
        order_id = order_id.strip()
        if not order_id.isdigit():
            if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", order_id):
                return {"error": "looks_like_cart_id", "message": "That's a cart id, not an order number -- order numbers are digits only."}
            return {"error": "invalid_order_id", "message": "Order numbers are digits only."}
    session = _load_session()
    api = _get_api(session.banner)
    try:
        if order_id:
            return _simplify_order_detail(api.get_historical_order(order_id))
        raw = api.get_historical_orders()
    except PcxApiError as exc:
        if order_id and exc.status_code == 500:
            # PC Express answers an unknown order id with HTTP 500, not 404.
            return {"error": "order_not_found", "message": f"No order {order_id} on this account."}
        return _tool_error(exc)
    except PcidAuthError as exc:
        return _tool_error(exc)

    orders = [_simplify_order_summary(o) for o in (raw.get("orderHistory") or [])[:limit]]
    return {
        "total_online_orders": raw.get("onlineOrdersCount"),
        "total_offline_orders": raw.get("offlineOrdersCount"),
        "returned": len(orders),
        "orders": orders,
    }


# order id -> (store id, product lines). Only settled orders: a recent one
# keeps changing after placement (lines dropped, totals updated).
_ORDER_LINES_CACHE: dict[str, tuple[Optional[str], list[dict]]] = {}
_ORDER_SETTLED_AFTER = timedelta(days=7)


def _order_store_and_lines(api: PCExpressAPI, summary: dict) -> Optional[tuple[Optional[str], list[dict]]]:
    """(real store id, product lines) for one order, or None if its detail
    fetch fails. The store id comes from the detail, not the summary's
    store name, which has legacy variants for the same store on a real
    account ("1024-Oakville", "North Oakville")."""
    order_id = summary["id"]
    if order_id in _ORDER_LINES_CACHE:
        return _ORDER_LINES_CACHE[order_id]
    try:
        detail = api.get_historical_order(order_id)
    except (PcidAuthError, PcxApiError):
        return None
    od = detail.get("orderDetails") or {}
    pickup = (od.get("booking") or {}).get("pickupLocation") or {}
    lines = []
    for e in od.get("entries", []) or []:
        product = e.get("product") or {}
        code = _order_line_code(product)
        if code and not _adjustment_kind(product):
            lines.append(
                {
                    "code": code,
                    "name": product.get("productName"),
                    "brand": product.get("brand"),
                    "quantity": e.get("quantity") or 0,
                    "weight": e.get("weight") or 0,
                }
            )
    result = (pickup.get("storeId") or pickup.get("id"), lines)
    settled_before = (datetime.now(timezone.utc) - _ORDER_SETTLED_AFTER).strftime("%Y-%m-%dT%H:%M:%S")
    if (summary.get("placed") or "9") < settled_before:
        _ORDER_LINES_CACHE[order_id] = result
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Purchase History", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def get_purchase_history(limit: int = 20, max_orders_scanned: int = 60, top_n: int = 60) -> dict:
    """Products this household has actually bought at the active store,
    most-frequently-bought first.

    Call this once, early in a session, before searching or adding items
    -- not before every subsequent tool call -- and reuse it for the rest
    of the session. A product bought repeatedly is a safer, better-catered
    default than a similar-looking one never bought. It shows what was
    *bought*, not what was liked: still ask when unsure, especially for
    anything non-staple or when buying on someone else's behalf.

    Scans up to `max_orders_scanned` recent orders (newest first) until
    `limit` orders from the active store are found; orders from other
    stores on the account are excluded by real store id. Returns the
    `top_n` most-bought rows (`truncated` says if more exist). `code`s work
    directly with the cart tools. Weighed items report `total_weight_kg`
    instead of a quantity. Tips and stamps are excluded.
    `fulfillment_types` counts how matched orders were fulfilled -- the
    household's usual delivery/pickup choice.
    """
    session = _load_session()
    api = _get_api(session.banner)
    if not _ensure_active_store(api, session):
        return _no_active_store()
    try:
        raw = api.get_historical_orders()
    except (PcidAuthError, PcxApiError) as exc:
        return _tool_error(exc)
    summaries = sorted(
        (o for o in raw.get("orderHistory") or [] if o.get("id")), key=lambda o: o.get("placed") or "", reverse=True
    )[:max_orders_scanned]

    items_by_code: dict[str, dict] = {}
    fulfillment_types: dict[str, int] = {}
    matched = scanned = 0
    # Detail fetches are the slow part (~1s each; a 93-order scan took 77s
    # sequentially), so fetch in parallel, never more than still needed.
    with ThreadPoolExecutor(max_workers=8) as pool:
        while scanned < len(summaries) and matched < limit:
            batch = summaries[scanned : scanned + min(8, limit - matched)]
            for summary, fetched in zip(batch, pool.map(lambda s: _order_store_and_lines(api, s), batch)):
                scanned += 1
                if not fetched or fetched[0] != session.store_id:
                    continue
                matched += 1
                kind = summary.get("fulfillmentType") or "UNKNOWN"
                fulfillment_types[kind] = fulfillment_types.get(kind, 0) + 1
                for line in fetched[1]:
                    entry = items_by_code.setdefault(
                        line["code"],
                        {
                            "code": line["code"],
                            "name": line["name"],
                            "brand": line["brand"],
                            "times_purchased": 0,
                            "last_purchased": (summary.get("placed") or "")[:10],
                        },
                    )
                    entry["times_purchased"] += 1
                    if line["weight"]:
                        entry["total_weight_kg"] = round(entry.get("total_weight_kg", 0) + line["weight"], 3)
                    else:
                        entry["total_quantity"] = entry.get("total_quantity", 0) + line["quantity"]

    items = sorted(items_by_code.values(), key=lambda i: i["times_purchased"], reverse=True)
    return {
        "store_id": session.store_id,
        "orders_scanned": scanned,
        "orders_matched": matched,
        "fulfillment_types": fulfillment_types,
        "distinct_items": len(items),
        "truncated": len(items) > top_n,
        "items": items[:top_n],
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
