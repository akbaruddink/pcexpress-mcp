"""Unit tests for interactive_product_search and its app-only fetch tool --
the MCP Apps widget feature added after a user asked for interactive,
visual search results instead of plain text.

See docs/RESEARCH.md "Interactive product search widget" for how this was
built (an actual client-side widget, bundled with esbuild from
web/product-search-widget/, verified against real ext-apps documentation
and a working reference implementation rather than guessed), verified
live end-to-end against a real account before shipping, and later
reworked from a cached-result-ref design to a re-run-the-search-live
design after a real user report showed the cache going stale across
server restarts.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp.api_client import PcxApiError  # noqa: E402
from pc_express_mcp.auth import PcidAuthError  # noqa: E402
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


class _FailingApi:
    def search_products(self, term, store_id, cart_id=None, size=25, from_=0):
        raise PcxApiError("boom", status_code=500, body="")


def _patch(monkeypatch, store_id="1024", api=None):
    session = session_state.SessionState(store_id=store_id, cart_id="cart-1")
    monkeypatch.setattr(server, "_load_session", lambda: session)
    monkeypatch.setattr(server, "_get_api", lambda banner: api or _FakeApi())


def test_launcher_returns_full_results_when_client_lacks_apps_support(monkeypatch):
    _patch(monkeypatch)
    result = server.interactive_product_search(query="cheese", size=5, ctx=None)
    assert "store_id" not in result  # that's the Apps-branch reference shape, not this one
    assert result["count"] == 1
    assert result["results"][0]["photo_markdown"] == "![Test Cheese](https://digital.loblaws.ca/PCX/20700462_EA/en/1/test.png)"


def test_launcher_returns_a_small_reproducible_reference_when_client_supports_apps(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(server, "client_supports_apps", lambda ctx: True)
    result = server.interactive_product_search(query="cheese", size=5, ctx=object())
    assert "results" not in result
    note = result.pop("note")
    assert result == {"query": "cheese", "size": 5, "store_id": "1024", "banner": "superstore", "count": 1}
    # The small reference is the *correct* outcome, not a rendering
    # failure -- a real user report showed a model misreading this
    # terse shape as "the widget didn't render" and stating that as
    # fact, which it can't actually observe. The note exists so the
    # model doesn't have to guess.
    assert "not a sign" in note or "correct behavior" in note
    assert "widget" in note.lower()

    # The widget fetches the real data itself via the app-only tool,
    # re-running the search live from these exact params -- not reading
    # a cache back by an opaque id.
    fetched = server._interactive_search_results(query="cheese", size=5, store_id="1024", banner="superstore")
    assert fetched["results"][0]["name"] == "Test Cheese"


def test_launcher_requires_active_store(monkeypatch):
    _patch(monkeypatch, store_id=None)
    result = server.interactive_product_search(query="cheese")
    assert result["error"] == "no_active_store"


def test_interactive_search_results_re_runs_the_search_live(monkeypatch):
    monkeypatch.setattr(server, "_get_api", lambda banner: _FakeApi())
    result = server._interactive_search_results(query="cheese", size=5, store_id="1024", banner="superstore")
    assert result["results"][0]["name"] == "Test Cheese"
    # No cache, so nothing to expire -- calling it again with the exact
    # same params (simulating a widget re-fetch after a server restart)
    # works identically, not "expired."
    result_again = server._interactive_search_results(query="cheese", size=5, store_id="1024", banner="superstore")
    assert result_again == result


def test_interactive_search_results_surfaces_a_real_api_failure(monkeypatch):
    monkeypatch.setattr(server, "_get_api", lambda banner: _FailingApi())
    result = server._interactive_search_results(query="cheese", size=5, store_id="1024", banner="superstore")
    assert result["results"] == []
    assert result["error"] == "api_error"


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
