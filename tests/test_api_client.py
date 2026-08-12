"""Unit tests for api_client.py's PcxApiError.pcx_error -- pure logic, no
network. See that property's docstring for the real double-encoded error
shape this parses (confirmed live against a real SELLER_ID_MISMATCH
response while investigating a real user-reported cart bug).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp.api_client import PcxApiError  # noqa: E402

# Real body, captured live (see docs/RESEARCH.md "Cart is bound to a
# single store") -- not hand-constructed, to make sure the parser handles
# the actual escaping pcx-bff produces, not an idealized version of it.
REAL_SELLER_MISMATCH_BODY = (
    '{"errors":[{"message":"{\\"error_response\\":{\\"message\\":\\"The seller_id provided in the request '
    'does not match the seller_id associated with the cart\'s seller_cart\\",\\"details\\":{\\"expected\\":'
    '\\"2841\\",\\"provided\\":\\"1024\\"},\\"error_code\\":\\"SELLER_ID_MISMATCH\\"}}","subjectType":'
    '"PLATFORM_CART","type":null,"reason":null,"service":null,"call":null,"code":null}]}'
)


def test_pcx_error_parses_real_seller_mismatch_body():
    exc = PcxApiError("test", status_code=400, body=REAL_SELLER_MISMATCH_BODY)
    result = exc.pcx_error
    assert result["error_code"] == "SELLER_ID_MISMATCH"
    assert result["details"] == {"expected": "2841", "provided": "1024"}


def test_pcx_error_returns_none_for_plain_error_body():
    exc = PcxApiError("test", status_code=404, body='{"errors":[{"message":"Not found"}]}')
    assert exc.pcx_error is None


def test_pcx_error_returns_none_for_non_json_body():
    exc = PcxApiError("test", status_code=500, body="<html>not json</html>")
    assert exc.pcx_error is None


def test_pcx_error_returns_none_for_empty_body():
    exc = PcxApiError("test", status_code=500, body="")
    assert exc.pcx_error is None


def test_pcx_error_returns_none_when_errors_list_missing():
    exc = PcxApiError("test", status_code=500, body="{}")
    assert exc.pcx_error is None
