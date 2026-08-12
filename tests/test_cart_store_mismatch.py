"""Unit tests for the SELLER_ID_MISMATCH handling added to _tool_error and
set_active_store's proactive cart_note -- see docs/RESEARCH.md "Cart is
bound to a single store" for the real user-reported bug and investigation
this fixes: an account has exactly one active cart at a time, bound to
whichever store it was last used at (relevant for accounts shared across
locations, e.g. family members ordering from different stores). The real
fix (switch_cart_store) is tested separately in
test_cart_store_switch.py -- these tests just cover the error message
pointing at it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp.api_client import PcxApiError  # noqa: E402
from pc_express_mcp.auth import PcidAuthError  # noqa: E402
from pc_express_mcp.server import _cart_store_mismatch_note, _tool_error  # noqa: E402
from tests.test_api_client import REAL_SELLER_MISMATCH_BODY  # noqa: E402


def test_tool_error_gives_precise_actionable_message_for_seller_mismatch():
    exc = PcxApiError("test", status_code=400, body=REAL_SELLER_MISMATCH_BODY)
    result = _tool_error(exc)
    assert result["error"] == "cart_store_mismatch"
    assert result["cart_bound_to_store"] == "2841"
    assert result["requested_store"] == "1024"
    # The message must actually name both stores and say what to do --
    # not just "cart error, try the app" (the vague version this replaces).
    assert "2841" in result["message"]
    assert "1024" in result["message"]
    assert "switch_cart_store" in result["message"]


def test_tool_error_falls_back_to_generic_api_error_for_other_pcx_errors():
    exc = PcxApiError("not found", status_code=404, body='{"errors":[{"message":"Not found"}]}')
    result = _tool_error(exc)
    assert result["error"] == "api_error"
    assert result["status_code"] == 404


def test_tool_error_still_handles_auth_errors():
    result = _tool_error(PcidAuthError("expired"))
    assert result["error"] == "auth_required"


def test_tool_error_still_handles_unexpected_exceptions():
    result = _tool_error(ValueError("boom"))
    assert result["error"] == "unexpected"


def test_cart_store_mismatch_note_warns_when_stores_differ():
    note = _cart_store_mismatch_note("2841", "1024")
    assert note is not None
    assert "2841" in note
    assert "1024" in note
    assert "switch_cart_store" in note


def test_cart_store_mismatch_note_silent_when_stores_match():
    assert _cart_store_mismatch_note("1024", "1024") is None


def test_cart_store_mismatch_note_silent_when_cart_store_unknown():
    assert _cart_store_mismatch_note(None, "1024") is None
    assert _cart_store_mismatch_note("", "1024") is None
