"""
Tests for fx.py's per-day rate cache and its own BOT_TIMEZONE-aware clock.

fx.get_rate is monkeypatched to raise by conftest's autouse
_no_real_network fixture (see conftest.py), so these tests call the real
implementation directly via a reference captured at import time, before
any per-test monkeypatching happens -- the only way to exercise get_rate's
own caching logic without hitting the network guard meant for every OTHER
test in the suite.
"""

import json
from datetime import date

import fx

# Captured now, at module import/collection time -- before any test's
# autouse fixtures run and replace the module attribute fx.get_rate with
# conftest's network-guard stub. This is the real, un-patched function.
_REAL_GET_RATE = fx.get_rate


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_get_rate_cache_key_uses_bot_timezone_date_not_server_clock(monkeypatch):
    """Regression test for a real bug: the per-day rate cache used to key
    off date.today() (the server's/OS date -- UTC on Railway), which lags
    Asia/Singapore's actual calendar date by up to 8 hours after local
    midnight -- a rate fetched in that window got cached under the wrong
    day, silently disagreeing with every other "what day is it"
    computation in the app (db.today_str(), the canonical one). get_rate
    must key off fx._now_local_date() -- a BOT_TIMEZONE-aware,
    independently monkeypatchable twin of db._now_local_date -- not the
    raw system clock, which is what this test drives directly."""
    fx._rate_cache.clear()
    calls = []

    def fake_urlopen_v1(url, timeout=8):
        calls.append(url)
        return _FakeResponse({"rates": {"SGD": 1.35}})

    monkeypatch.setattr(fx.urllib.request, "urlopen", fake_urlopen_v1)
    monkeypatch.setattr(fx, "_now_local_date", lambda: date(2026, 9, 15))

    assert _REAL_GET_RATE("USD", "SGD") == 1.35
    assert len(calls) == 1

    # Same local day (per _now_local_date) -- served from cache, no second
    # network call, even though we haven't touched urlopen's fake return.
    assert _REAL_GET_RATE("USD", "SGD") == 1.35
    assert len(calls) == 1

    # A new local day per _now_local_date -- nothing about the real system
    # clock changed, but this must still re-fetch rather than silently
    # reusing yesterday's cached rate.
    def fake_urlopen_v2(url, timeout=8):
        calls.append(url)
        return _FakeResponse({"rates": {"SGD": 1.40}})

    monkeypatch.setattr(fx.urllib.request, "urlopen", fake_urlopen_v2)
    monkeypatch.setattr(fx, "_now_local_date", lambda: date(2026, 9, 16))

    assert _REAL_GET_RATE("USD", "SGD") == 1.40
    assert len(calls) == 2
