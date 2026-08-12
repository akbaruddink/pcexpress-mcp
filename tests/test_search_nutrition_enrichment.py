"""Unit tests for search_products' include_nutrition option: the size cap
tied to Open Food Facts' documented rate limit, and the per-result
enrichment loop's pacing/rate-limit/not-found handling. See that tool's
docstring in server.py and docs/RESEARCH.md "Nutrition enrichment (Open
Food Facts)" for why these specific limits exist.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from pc_express_mcp import server  # noqa: E402
from pc_express_mcp.server import (  # noqa: E402
    MAX_SIZE_WITH_NUTRITION,
    _cap_results_for_nutrition,
    _cap_size_for_nutrition,
    _enrich_with_nutrition,
)


def test_cap_size_leaves_requests_alone_when_nutrition_disabled():
    assert _cap_size_for_nutrition(50, False) == (50, False)


def test_cap_size_leaves_small_requests_alone_when_nutrition_enabled():
    assert _cap_size_for_nutrition(10, True) == (10, False)


def test_cap_size_caps_at_max_when_nutrition_enabled():
    assert _cap_size_for_nutrition(50, True) == (MAX_SIZE_WITH_NUTRITION, True)


def test_cap_size_exactly_at_max_is_not_flagged_as_capped():
    assert _cap_size_for_nutrition(MAX_SIZE_WITH_NUTRITION, True) == (MAX_SIZE_WITH_NUTRITION, False)


def test_cap_results_leaves_results_alone_when_nutrition_disabled():
    results = list(range(50))
    capped, was_capped = _cap_results_for_nutrition(results, False)
    assert capped == results
    assert was_capped is False


def test_cap_results_truncates_to_exactly_max_when_over():
    # Mirrors the exact real scenario this exists for: PC Express returned
    # more results than requested (confirmed live: asked for 5, got 7) --
    # capping the *request* size alone isn't enough, the actual results
    # list needs its own truncation to reliably cap nutrition lookups.
    results = list(range(20))
    capped, was_capped = _cap_results_for_nutrition(results, True)
    assert capped == list(range(MAX_SIZE_WITH_NUTRITION))
    assert was_capped is True


def test_cap_results_at_exactly_max_is_not_flagged_as_capped():
    results = list(range(MAX_SIZE_WITH_NUTRITION))
    capped, was_capped = _cap_results_for_nutrition(results, True)
    assert capped == results
    assert was_capped is False


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _dont_actually_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(server.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def test_enrich_skips_items_without_barcode(monkeypatch):
    _dont_actually_sleep(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("should never be called -- no results have a barcode")

    results = [{"code": "1", "barcode": None}, {"code": "2"}]
    summary = _enrich_with_nutrition(results, _mock_client(handler))
    assert summary == {"enriched_count": 0, "rate_limited": False}
    assert "nutrition" not in results[0]
    assert "nutrition" not in results[1]


def test_enrich_attaches_nutrition_and_marks_not_found(monkeypatch):
    sleeps = _dont_actually_sleep(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"status": 1, "product": {"product_name": "Milk", "nutriscore_grade": "b"}})
        return httpx.Response(200, json={"status": 0})

    results = [{"code": "1", "barcode": "06680000015"}, {"code": "2", "barcode": "06563313212"}]
    summary = _enrich_with_nutrition(results, _mock_client(handler))

    assert summary == {"enriched_count": 1, "rate_limited": False}
    assert results[0]["nutrition"]["product_name"] == "Milk"
    assert results[1]["nutrition"] == {"found": False}
    # Paced once between the two real lookups, not before the first.
    assert sleeps == [1]


def test_enrich_does_not_sleep_before_first_lookup_even_with_leading_skips(monkeypatch):
    sleeps = _dont_actually_sleep(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 1, "product": {}})

    results = [{"code": "1"}, {"code": "2", "barcode": "06680000015"}]  # first has no barcode
    _enrich_with_nutrition(results, _mock_client(handler))
    assert sleeps == []  # only one real lookup happened -- no pacing needed


def test_enrich_stops_on_rate_limit_leaving_remaining_results_unenriched(monkeypatch):
    _dont_actually_sleep(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"status": 1, "product": {"product_name": "Milk"}})
        return httpx.Response(429)

    results = [
        {"code": "1", "barcode": "06680000015"},
        {"code": "2", "barcode": "06563313212"},
        {"code": "3", "barcode": "06680000004"},
    ]
    summary = _enrich_with_nutrition(results, _mock_client(handler))

    assert summary == {"enriched_count": 1, "rate_limited": True}
    assert results[0]["nutrition"]["product_name"] == "Milk"
    assert "nutrition" not in results[2]  # loop stopped before reaching this one
    assert calls["n"] == 2  # did not keep trying after the 429
