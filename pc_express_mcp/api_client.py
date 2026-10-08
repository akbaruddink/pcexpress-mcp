"""Thin HTTP client for the pcx-bff API and the website's checkout service.

Most of this (headers, customer/cart/orders/search/add-to-cart endpoints)
was cross-checked against the prior-art project's working Python source,
not just its docs -- see config.py's module docstring. A handful of
methods (list_carts, cart_heartbeat, type_ahead, get_pickup_location) are
only documented as "verified" in that project's API_REFERENCE.md; see their
docstrings. Slots, booking and the checkout summary come from the
website's own checkout service (one-checkout.<banner domain>), captured
from a real web checkout session -- see get_delivery_slots.

Every request goes through `_request`, which attaches the standard headers,
retries exactly once on HTTP 401 after forcing a token refresh, and raises
`PcxApiError` (with the response body attached) on any other failure so
callers get something more useful than a bare httpx exception.

Loblaw's carts also expire server-side; `get_cart`/`update_cart_entries`
calls can start 404ing on a previously-valid cart_id. Callers (server.py)
handle re-discovering the cart via get_profile() and retrying once -- this
module just makes sure a 404 surfaces as a normal PcxApiError with
status_code=404 so that's possible.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from . import config
from .auth import PcidAuthError, TokenManager


class PcxApiError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    @property
    def pcx_error(self) -> Optional[dict]:
        """Some pcx-bff errors (confirmed live: cart update's SELLER_ID_MISMATCH)
        wrap a real, structured error as a JSON *string* double-encoded
        inside `errors[0].message`, not a plain nested object:

            {"errors": [{"message": "{\\"error_response\\": {\\"error_code\\":
            \\"SELLER_ID_MISMATCH\\", \\"details\\": {\\"expected\\": \\"2841\\",
            \\"provided\\": \\"1024\\"}, ...}}"}]}

        Returns the inner `error_response` dict (with `error_code` and
        `details`) if the body matches that shape, else None -- most
        pcx-bff error bodies don't, so this is best-effort, not assumed
        present. Not specific to carts: this looks like a general pcx-bff
        convention, just only confirmed against this one real error so far.
        """
        try:
            outer = json.loads(self.body)
            inner = json.loads(outer["errors"][0]["message"])
            error_response = inner.get("error_response")
            return error_response if isinstance(error_response, dict) else None
        except (ValueError, KeyError, TypeError, IndexError):
            return None


def _build_pcx_headers(token: str, banner: str) -> dict[str, str]:
    domain = config.banner_info(banner)["domain"]
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en",
        "Authorization": f"Bearer {token}",
        "Business-User-Agent": "PCXWEB",
        "Origin": f"https://{domain}",
        "Referer": f"https://{domain}/",
        # Both of these are the raw banner key itself (e.g. "loblaws"),
        # not a separate mapped value -- see config.py's BANNERS comment.
        "Site-Banner": banner,
        "baseSiteId": banner,
        "x-apikey": config.PCX_APIKEY,
        "x-application-type": "Web",
        "x-loblaw-tenant-id": "ONLINE_GROCERIES",
        "is-helios-account": "true",
    }


def fetch_customer_id(client: httpx.Client, access_token: str, banner: str) -> Optional[str]:
    """Look up the profile.id (PC Express login email, verified live against
    a real account -- NOT customerId, a separate UUID also present in the
    response) for a raw access token that hasn't been through
    TokenManager/PCExpressAPI at all.

    Used by oauth_server.py's account-verification flow: it needs to know
    *whose* PC Express account a fresh login belongs to before deciding
    whether to trust it, using a token that must NOT be persisted or
    treated as "the" session until that check passes -- so this
    deliberately bypasses the normal TokenManager-backed request path,
    which would conflate the two.
    """
    headers = _build_pcx_headers(access_token, banner)
    resp = client.get(f"{config.PCX_BFF_BASE}/ecommerce/v2/{banner}/customers", headers=headers)
    if resp.status_code != 200:
        return None
    return resp.json().get("id")


# One process-wide, thread-safe connection pool. server.py builds a fresh
# PCExpressAPI per tool call; giving each its own pool meant a new TLS
# handshake every call and sockets left for GC. Each instance still gets its
# own Client, so cookies never cross tenants.
_TRANSPORT = httpx.HTTPTransport()


class PCExpressAPI:
    def __init__(self, token_manager: TokenManager, banner: str):
        self.token_manager = token_manager
        self.banner = banner
        self.banner_info = config.banner_info(banner)
        self._client = httpx.Client(timeout=30.0, transport=_TRANSPORT)

    # -- low-level request plumbing -----------------------------------

    def _auth_headers(self) -> dict[str, str]:
        token = self.token_manager.get_access_token(self._client)
        return _build_pcx_headers(token, self.banner)

    def _checkout_auth_headers(self) -> dict[str, str]:
        """The website's checkout service (one-checkout.<banner domain>)
        takes the same PC ID token, but as an `authToken` cookie: a bearer
        header alone 401s on checkout and booking, and on the slot list it
        loses the account's own fees ($5.99 instead of $0). Confirmed live."""
        token = self.token_manager.get_access_token(self._client)
        cookie = f"authToken={token}"
        if self.banner_info.get("checkout_lob"):
            cookie += f"; lob={self.banner_info['checkout_lob']}"
        return {"Cookie": cookie, "Content-Type": "application/json", "Accept": "application/json"}

    def _checkout_url(self, path: str) -> str:
        host = self.banner_info.get("checkout_host")
        if not host:
            raise PcxApiError(f"No checkout service is known for banner {self.banner!r}.")
        return f"https://{host}/api/{path}"

    def checkout_page_url(self) -> str:
        """Where the user finishes checkout (and pays) in a browser."""
        host = self.banner_info.get("checkout_host")
        return f"https://{host}/en/pre-checkout" if host else f"https://{self.banner_info['domain']}/"

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
        retried: bool = False,
        checkout: bool = False,
    ) -> httpx.Response:
        headers = self._checkout_auth_headers() if checkout else self._auth_headers()
        try:
            resp = self._client.request(method, url, headers=headers, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise PcxApiError(f"{method} {url} -> {type(exc).__name__}: {exc}") from exc

        # The checkout service also answers 401 for a missing banner cookie,
        # and a forced refresh in HTTP mode burns PC ID's single-use refresh
        # token (see auth.EphemeralTokenManager) -- so only retry pcx-bff.
        if resp.status_code == 401 and not retried and not checkout:
            self.token_manager.force_refresh(self._client)
            return self._request(method, url, json_body=json_body, params=params, retried=True)

        if resp.status_code >= 400:
            raise PcxApiError(
                f"{method} {url} -> HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:1000],
            )
        return resp

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """_request, parsed. A non-JSON body (seen: HTTP 200 with an HTML
        maintenance or bot-check page) becomes a PcxApiError, not a crash."""
        resp = self._request(method, url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise PcxApiError(
                f"{method} {url} -> HTTP {resp.status_code} but non-JSON body", status_code=resp.status_code, body=resp.text[:1000]
            ) from exc

    # -- profile / cart discovery ---------------------------------------

    def get_profile(self) -> dict:
        url = f"{config.PCX_BFF_BASE}/ecommerce/v2/{self.banner}/customers"
        return self._request_json("GET", url)

    def get_customer_promotions(self) -> dict:
        """PC Optimum loyalty stamp-card program status -- real, working
        endpoint, found while looking for a broader "browse/clip available
        personalized offers" endpoint that turned out not to exist (a
        dozen plausible URL patterns tried against pcx-bff all 404'd; see
        docs/RESEARCH.md "Loyalty offers"). Confirmed shape only for the
        INACTIVE case (`{"stampCards": {"isActive": false, ...}}` -- this
        account's stamp card program isn't currently running); field names
        when a card is actually active (`balance`, `rewards`) are
        inferred, not verified against a populated example.
        """
        url = f"{config.PCX_BFF_BASE}/ecommerce/v2/{self.banner}/customers/promotions"
        return self._request_json("GET", url)

    # -- products ---------------------------------------------------------

    def search_products(
        self,
        term: str,
        store_id: str,
        cart_id: Optional[str] = None,
        size: int = 25,
        from_: int = 0,
    ) -> dict:
        """`from_` is a real, working offset -- verified live: `from_=5`
        returns genuinely different results than `from_=0`, not the same
        page again. The response's own `pagination.pageNumber` field is
        misleadingly named -- it echoes back the raw `from_` offset you
        sent, not a page *index* (`from_=5, size=5` comes back as
        `pageNumber: 5`, not `pageNumber: 1`). `pagination.totalResults`
        fluctuates a few percent between otherwise-identical calls seconds
        apart (observed: 136, then 128, then 127, then 141 for the same
        "milk"/store_id) -- this API appears to run ML-personalized/
        variant search (see the response's own `searchVariation`/
        `modelVersion` fields), so treat `totalResults` as an estimate for
        "roughly how many more are there," not an exact count. The actual
        number of items returned can also run slightly over the requested
        `size` (observed: asked for 5, got 7; asked for 20, got 22) --
        likely injected sponsored results, not confirmed.
        """
        url = f"{config.PCX_BFF_BASE}/products/search"
        body: dict[str, Any] = {
            "lang": "en",
            "term": term,
            "storeId": store_id,
            "banner": self.banner,
            "pagination": {"from": from_, "size": size},
        }
        if cart_id:
            body["cartId"] = cart_id
        return self._request_json("POST", url, json_body=body)

    def type_ahead(self, term: str, store_id: str) -> Any:
        """Search-suggestion endpoint. Documented as working but not wired to any tool here."""
        url = f"{config.PCX_BFF_BASE}/products/type-ahead"
        body = {
            "lang": "en",
            "term": term,
            "storeId": store_id,
            "banner": self.banner,
        }
        return self._request_json("POST", url, json_body=body)

    def get_product(self, product_code: str) -> dict:
        """Confirmed broken, not just unverified: every plausible URL/query-param
        combination tried against this endpoint (bare code, code without the
        `_EA` suffix, `storeId`/`banner`/`lang`/`cartId` query params in various
        combinations, a `/details` suffix, a POST variant) returned HTTP 400 or
        404 against a real account with a real, valid session. Not wired to any
        tool -- `search_products`' results already carry everything this
        client can actually get about a product (description, images, price,
        barcode, etc. -- see `_simplify_product` in server.py); this method is
        kept only as a documented dead end so nobody re-derives the same
        400s from scratch. If someone finds the real shape (likely needs a
        legitimate PC Express web/app session's Network tab, not blind
        guessing), the fix belongs here.
        """
        url = f"{config.PCX_BFF_BASE}/products/{product_code}"
        return self._request_json("GET", url)

    # -- cart ---------------------------------------------------------------

    def list_carts(self, customer_id: str) -> dict:
        """The banner-scoped cart-discovery path -- server.py's
        `_rediscover_cart` uses this, not `get_profile()['cartId']`.

        An earlier version of this client used `profile.cartId` instead
        (simpler, and what the prior-art project's own code does) --
        confirmed live to be wrong: `profile.cartId` returns the *same*
        cart id regardless of which banner's headers request it (observed
        always the superstore cart, even queried with nofrills headers),
        while this endpoint correctly returns each banner's own,
        genuinely different cart. See docs/RESEARCH.md "Cart discovery
        must be banner-scoped".
        """
        url = f"{config.PCX_BFF_BASE}/customers/{customer_id}/carts"
        return self._request_json("GET", url, params={"banner": self.banner})

    def get_cart(self, cart_id: str, with_inventory: bool = True) -> dict:
        url = f"{config.PCX_BFF_BASE}/carts/{cart_id}"
        params = {"inventory": "true"} if with_inventory else None
        return self._request_json("GET", url, params=params)

    def cart_heartbeat(self, cart_id: str) -> dict:
        url = f"{config.PCX_BFF_BASE}/carts/{cart_id}/heartbeat"
        return self._request_json("GET", url)

    def update_cart_entries(
        self,
        cart_id: str,
        entries: dict[str, dict[str, Any]],
    ) -> httpx.Response:
        """entries maps productCode -> {"quantity": N, "fulfillmentMethod": "pickup"|"delivery", "sellerId": store_id}.

        Set quantity=0 to remove an item, per the documented behaviour.
        """
        url = f"{config.PCX_BFF_BASE}/carts/{cart_id}"
        return self._request("POST", url, json_body={"entries": entries}, params={"inventory": "true"})

    # -- stores / fulfillment ------------------------------------------------

    def get_delivery_serviceability(self, postal_code: str) -> dict:
        """Maps a postal code to real courier fulfillment-location ids per
        banner, e.g. `{"banners": [{"name": "superstore", "pickupLocations":
        [{"id": "1024PCXD", ...}, ...]}, ...]}`. A different host/path than
        the rest of this client (`config.PCX_DELIVERY_SERVICEABILITY_URL`,
        not under pcx-bff) -- confirmed live to work with no Authorization
        header at all in a real user-supplied capture. Called here through
        the normal authenticated `_request` anyway for simplicity; the
        extra header is harmless, not required. See `switch_cart_store` in
        server.py, the only caller.
        """
        body = {"deliveryAddress": {"postalCode": postal_code}, "isB2b": False}
        return self._request_json("POST", config.PCX_DELIVERY_SERVICEABILITY_URL, json_body=body)

    def set_cart_fulfillment(
        self,
        cart_id: str,
        fulfillment_location_id: str,
        postal_code: str,
        dry_run: bool = False,
    ) -> dict:
        """Re-binds an existing cart to a different store/seller by setting
        its courier fulfillment -- confirmed live to be the real, working
        fix for SELLER_ID_MISMATCH (see docs/RESEARCH.md "Cart is bound to
        a single store"), overturning an earlier conclusion there that no
        such fix existed. That earlier attempt combined a fulfillment
        override with `entries` in the same call, under a different field
        name ("fulfillment"); the shape that actually works is a *separate*
        call with only a `courier` key -- no `entries` at all:

            {"courier": {"deliveryAddress": {"postalCode": ...},
             "fulfillmentLocationId": "1024PCXD"}, "fulfillmentType": "COURIER"}

        `dry_run=True` hits `/carts/{id}/dry-run` -- confirmed live to
        preview the switch (returns what the cart would look like
        afterwards) without persisting it; re-fetching the cart afterwards
        showed no change. The default (False) hits `/carts/{id}` directly
        and does persist -- confirmed live, followed by a fresh GET
        showing the new store. Existing cart entries carry over to the new
        store with a fresh `creationTime` (re-priced/re-validated against
        the new store's catalog, confirmed live), not dropped.
        """
        url = f"{config.PCX_BFF_BASE}/carts/{cart_id}" + ("/dry-run" if dry_run else "")
        body = {
            "courier": {
                "deliveryAddress": {"postalCode": postal_code},
                "fulfillmentLocationId": fulfillment_location_id,
            },
            "fulfillmentType": "COURIER",
        }
        return self._request_json("POST", url, json_body=body)

    def get_pickup_location(self, store_id: str) -> dict:
        url = f"{config.PCX_BFF_BASE}/pickup-locations/{store_id}"
        return self._request_json("GET", url, params={"bannerId": self.banner})

    # -- checkout service (slots, booking, checkout summary) ---------------
    # Shapes from a real web checkout session (user-supplied captures),
    # replayed live from this server before being wired in.

    def get_delivery_slots(self, cart_id: str, location_id: str, postal_code: str) -> dict:
        """{location_id: {"timeslots": [{date, startTime, endTime, available,
        charge, slotType, ...}]}} -- local store times, ~2 weeks out."""
        params = {"locationIds": location_id, "banner": self.banner, "postalCode": postal_code}
        return self._request_json("POST", self._checkout_url("timeslots"), params=params, json_body={"cartId": cart_id}, checkout=True)

    def book_delivery_slot(self, cart_id: str, date: str, start: str, end: str, location_id: str, postal_code: str) -> dict:
        """Hold a slot on the cart. The web client sends the store's *local*
        wall time with a literal "Z" (08:30 local -> "...T08:30:00.000Z"),
        and the service books 08:30 local -- confirmed live, so this
        replicates it rather than converting to real UTC."""
        body = {
            "deliveryTimeslot": {
                "startTime": f"{date}T{start}:00.000Z",
                "endTime": f"{date}T{end}:00.000Z",
                "locationId": location_id,
                "postalCode": postal_code,
            }
        }
        return self._request_json("PATCH", self._checkout_url(f"carts/{cart_id}/fulfillment"), json_body=body, checkout=True)

    def get_checkout(self, cart_id: str) -> dict:
        """The checkout page's own summary: real tax, fees, tip, booked slot."""
        return self._request_json("GET", self._checkout_url(f"checkout/{cart_id}"), params={"refresh": "true"}, checkout=True)

    # -- orders ---------------------------------------------------------------

    def get_historical_orders(self) -> dict:
        url = f"{config.PCX_BFF_BASE}/ecommerce/v2/{self.banner}/customers/historical-orders"
        return self._request_json("GET", url)

    def get_historical_order(self, order_id: str) -> dict:
        url = f"{config.PCX_BFF_BASE}/ecommerce/v2/{self.banner}/customers/historical-orders/{order_id}"
        return self._request_json("GET", url)
