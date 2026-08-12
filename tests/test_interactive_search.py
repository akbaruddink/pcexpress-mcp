"""Unit tests for interactive_product_search and its supporting cache/
app-only fetch tool -- the MCP Apps widget feature added after a user
asked for interactive, visual search results instead of plain text.

See docs/RESEARCH.md "Interactive product search widget" for how this was
built (an actual client-side widget, bundled with esbuild from
web/product-search-widget/, verified against real ext-apps documentation
and a working reference implementation rather than guessed) and verified
live end-to-end against a real account before shipping.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402

REAL_RAW_SEARCH_RESULT = {
    "results": [
        {
            "code": "20700462_EA",
            "articleNumber": "20700462",
            "name": "Test Cheese",
            "imageAssets": [{"mediumUrl": "https://digital.loblaws.ca/PCX/20700462_EA/en/1/test.png"}],
            "prices": {"price": {"value": 5.99}},
        }
    ]
}


class _FakeApi:
    def search_products(self, term, store_id, cart_id=None, size=25, from_=0):
        return REAL_RAW_SEARCH_RESULT


def _patch(monkeypatch, store_id="1024"):
    session = session_state.SessionState(store_id=store_id, cart_id="cart-1")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: _FakeApi())


def test_launcher_returns_full_results_when_client_lacks_apps_support(monkeypatch):
    _patch(monkeypatch)
    result = server.interactive_product_search(query="cheese", size=5, ctx=None)
    assert "result_ref" not in result
    assert result["count"] == 1
    assert result["results"][0]["photo_markdown"] == "![Test Cheese](https://digital.loblaws.ca/PCX/20700462_EA/en/1/test.png)"


def test_launcher_returns_only_a_reference_when_client_supports_apps(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(server, "client_supports_apps", lambda ctx: True)
    result = server.interactive_product_search(query="cheese", size=5, ctx=object())
    assert "results" not in result
    assert result["query"] == "cheese"
    assert result["count"] == 1
    assert result["result_ref"]

    # The widget fetches the real data itself via the app-only tool.
    fetched = server._interactive_search_results(result_ref=result["result_ref"])
    assert fetched["results"][0]["name"] == "Test Cheese"


def test_launcher_requires_active_store(monkeypatch):
    _patch(monkeypatch, store_id=None)
    result = server.interactive_product_search(query="cheese")
    assert result["error"] == "no_active_store"


def test_interactive_search_results_returns_expired_for_unknown_ref():
    result = server._interactive_search_results(result_ref="not-a-real-ref")
    assert result["error"] == "expired"
    assert result["results"] == []


def test_cache_eviction_is_bounded(monkeypatch):
    monkeypatch.setattr(server, "_search_results_cache", server.OrderedDict())
    monkeypatch.setattr(server, "_SEARCH_RESULTS_CACHE_MAX", 3)
    refs = [server._cache_interactive_search_results([{"code": f"P{i}"}]) for i in range(5)]
    assert len(server._search_results_cache) == 3
    # The two oldest were evicted; the three newest remain.
    assert server._interactive_search_results(result_ref=refs[0])["error"] == "expired"
    assert server._interactive_search_results(result_ref=refs[1])["error"] == "expired"
    assert server._interactive_search_results(result_ref=refs[-1])["results"][0]["code"] == "P4"


def test_widget_resource_is_registered_with_correct_mime_and_csp():
    resources = server.apps.resources()
    matches = [r for r in resources if r.resource.uri == server._INTERACTIVE_SEARCH_RESOURCE_URI]
    assert len(matches) == 1
    resource = matches[0].resource
    assert resource.mime_type == "text/html;profile=mcp-app"
    assert resource.meta["ui"]["csp"]["resourceDomains"] == ["https://digital.loblaws.ca"]
    assert "<title>PC Express Product Search</title>" in resource.text


def test_launcher_tool_is_bound_to_the_widget_resource():
    tools = {t.fn.__name__: (t, uri) for t, uri in server.apps._tools}
    launcher_binding, launcher_uri = tools["interactive_product_search"]
    assert launcher_uri == server._INTERACTIVE_SEARCH_RESOURCE_URI
    assert launcher_binding.meta["ui"]["resourceUri"] == server._INTERACTIVE_SEARCH_RESOURCE_URI

    fetch_binding, fetch_uri = tools["_interactive_search_results"]
    assert fetch_uri == server._INTERACTIVE_SEARCH_RESOURCE_URI
    assert fetch_binding.meta["ui"]["visibility"] == ["app"]
