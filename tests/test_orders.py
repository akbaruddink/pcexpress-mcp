"""get_order_status input handling."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp import server, session_state  # noqa: E402
from pc_express_mcp.api_client import PcxApiError  # noqa: E402


class _FakeApi:
    def get_historical_order(self, order_id):
        # Real behaviour: PC Express answers an unknown order id with HTTP 500.
        raise PcxApiError("HTTP 500", status_code=500, body="")


def _patch(monkeypatch):
    monkeypatch.setattr(server, "_load_session", lambda: session_state.SessionState(store_id="1024"))
    monkeypatch.setattr(server, "_get_api", lambda banner: _FakeApi())


def test_order_status_recognizes_a_cart_id(monkeypatch):
    _patch(monkeypatch)
    assert server.get_order_status(order_id="a60c4f9b-c32c-4802-ba1d-42aff06bd157")["error"] == "looks_like_cart_id"
    assert server.get_order_status(order_id="CA-12")["error"] == "invalid_order_id"


def test_order_status_maps_upstream_500_to_not_found(monkeypatch):
    _patch(monkeypatch)
    result = server.get_order_status(order_id="0000000000")
    assert result["error"] == "order_not_found"
    assert "http" not in result["message"].lower()
