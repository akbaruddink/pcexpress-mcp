"""Unit tests for nutrition_client.py's barcode reconstruction -- pure logic,
no network. See that module's docstring for the real bug this works around:
PC Express's `upcs` field strips both the leading `0` and the trailing check
digit from a real EAN-13, confirmed by diffing against Open Food Facts' real
code for the same product.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from pc_express_mcp import nutrition_client  # noqa: E402
from pc_express_mcp.nutrition_client import normalize_barcode  # noqa: E402


def test_normalize_11_digit_reconstructs_real_ean13():
    # Real case, confirmed live: PC Express's "06563313212" is Open Food
    # Facts' real "0065633132122" with the leading 0 and trailing check
    # digit both stripped.
    assert normalize_barcode("06563313212") == "0065633132122"


def test_normalize_11_digit_second_real_example():
    assert normalize_barcode("06680000015") == "0066800000152"


def test_normalize_strips_non_digit_characters():
    assert normalize_barcode("0668-0000-015") == "0066800000152"


def test_normalize_12_digit_prepends_zero():
    assert normalize_barcode("066800000152") == "0066800000152"


def test_normalize_13_digit_passes_through():
    assert normalize_barcode("0066800000152") == "0066800000152"


def test_normalize_8_digit_ean8_passes_through():
    assert normalize_barcode("12345678") == "12345678"


def test_normalize_rejects_7_digit_plu_code():
    # Real shape, confirmed live: 7-digit codes are internal PLU codes for
    # items sold by weight (fresh meat/deli) -- they never had a real
    # manufacturer barcode, so there's nothing to reconstruct.
    assert normalize_barcode("2375590") is None


def test_normalize_rejects_garbage():
    assert normalize_barcode("not-a-barcode") is None
    assert normalize_barcode("") is None


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_nutrition_returns_product_on_real_shape_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/product/0066800000152.json"
        assert "fields" in request.url.params
        assert request.headers["user-agent"]  # OFF requires a real User-Agent
        return httpx.Response(200, json={"status": 1, "product": {"product_name": "2% Milk", "nutriscore_grade": "b"}})

    result = nutrition_client.fetch_nutrition("06680000015", client=_mock_client(handler))
    assert result == {"product_name": "2% Milk", "nutriscore_grade": "b"}


def test_fetch_nutrition_returns_none_when_status_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "status_verbose": "product not found"})

    assert nutrition_client.fetch_nutrition("06680000015", client=_mock_client(handler)) is None


def test_fetch_nutrition_raises_distinct_exception_on_rate_limit():
    # Rate-limited (429) must NOT be conflated with a plain not-found --
    # a caller telling the user "no data for this product" when the real
    # reason is "we got rate-limited" would be actively misleading.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(nutrition_client.OpenFoodFactsRateLimited):
        nutrition_client.fetch_nutrition("06680000015", client=_mock_client(handler))


def test_fetch_nutrition_returns_none_for_unreconstructable_barcode():
    # A 7-digit PLU code never reaches the network at all.
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("should not make a request for an unreconstructable barcode")

    assert nutrition_client.fetch_nutrition("2375590", client=_mock_client(handler)) is None


def test_fetch_nutrition_returns_none_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    assert nutrition_client.fetch_nutrition("06680000015", client=_mock_client(handler)) is None
