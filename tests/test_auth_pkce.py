"""Unit tests for pure logic that doesn't require network access or the
`mcp`/`httpx` packages -- run with `pytest` after `pip install -e .[dev]`.
"""

import base64
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pc_express_mcp.auth import extract_pcid_redirect, generate_pkce_pair, generate_state  # noqa: E402
from pc_express_mcp.config import banner_info  # noqa: E402


def test_pkce_pair_is_valid_s256_challenge():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    # No padding characters -- required for the S256 code_challenge format.
    assert "=" not in challenge


def test_pkce_pairs_are_random():
    v1, _ = generate_pkce_pair()
    v2, _ = generate_pkce_pair()
    assert v1 != v2


def test_state_is_url_safe_and_random():
    s1 = generate_state()
    s2 = generate_state()
    assert s1 != s2
    assert len(s1) > 16


def test_banner_info_known_banner():
    info = banner_info("superstore")
    # Site-Banner/baseSiteId headers use the raw banner key itself (see
    # config.py's BANNERS comment) -- banner_info() only carries the domain.
    assert info["domain"] == "www.realcanadiansuperstore.ca"


def test_banner_info_unknown_banner_raises():
    try:
        banner_info("not-a-real-banner")
    except ValueError as exc:
        assert "not-a-real-banner" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_extract_pcid_redirect_bare_code():
    code, state, error = extract_pcid_redirect("  abc123  ")
    assert code == "abc123"
    assert state is None
    assert error is None


def test_extract_pcid_redirect_failed_app_scheme_url():
    url = "com.loblaw.pcx://pcx-android/login/appredirect?code=XYZ&state=STATE1&lastOp=login"
    code, state, error = extract_pcid_redirect(url)
    assert code == "XYZ"
    assert state == "STATE1"
    assert error is None


def test_extract_pcid_redirect_interstitial_url_with_nested_code():
    # A real, reported-by-a-user shape: accounts.pcid.ca's interstitial page
    # before the failed com.loblaw.pcx:// navigation. Its redirectURL value
    # isn't URL-escaped, so `code` ends up nested inside it while `state`
    # leaks out as a top-level param -- this is the exact bug this parser
    # was written to fix (naive top-level-only parsing finds state but not
    # code here).
    url = (
        "https://accounts.pcid.ca/login/success?redirectURL=com.loblaw.pcx://"
        "pcx-android/login/appredirect?code=REALCODE&state=REALSTATE&lastOp=login&casl=&hidePageView=true"
    )
    code, state, error = extract_pcid_redirect(url)
    assert code == "REALCODE"
    assert state == "REALSTATE"
    assert error is None


def test_extract_pcid_redirect_error_response():
    url = "com.loblaw.pcx://pcx-android/login/appredirect?error=access_denied&error_description=User+cancelled"
    code, state, error = extract_pcid_redirect(url)
    assert code is None
    assert "access_denied" in error
