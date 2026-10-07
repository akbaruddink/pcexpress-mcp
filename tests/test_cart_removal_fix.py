"""get_time_slots could return HTTP 200 with an HTML "Site Under
Maintenance" page instead of JSON, crashing with a raw json.JSONDecodeError
instead of a normal tool error. (The removal sellerId fix this file also
covered is now tested in test_bulk_cart_ops.py.)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp.api_client import PCExpressAPI, PcxApiError  # noqa: E402


def test_get_time_slots_raises_clean_error_on_non_json_response(monkeypatch):
    api = PCExpressAPI(token_manager=None, banner="superstore")

    class _HtmlResp:
        status_code = 200
        text = "<!DOCTYPE html><title>Site Under Maintenance</title>"

        def json(self):
            raise json.JSONDecodeError("Expecting value", self.text, 0)

    monkeypatch.setattr(api, "_request", lambda method, url, **kw: _HtmlResp())

    with pytest.raises(PcxApiError) as exc_info:
        api.get_time_slots("1024")
    assert exc_info.value.status_code == 200
    assert "non-JSON" in str(exc_info.value)


def test_get_time_slots_still_returns_real_json_normally(monkeypatch):
    api = PCExpressAPI(token_manager=None, banner="superstore")

    class _JsonResp:
        status_code = 200
        text = '{"timeSlots": []}'

        def json(self):
            return {"timeSlots": []}

    monkeypatch.setattr(api, "_request", lambda method, url, **kw: _JsonResp())
    assert api.get_time_slots("1024") == {"timeSlots": []}
