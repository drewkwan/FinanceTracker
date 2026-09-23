"""
Shared pytest fixtures.

Each test gets its own throwaway SQLite file. Test bodies build dates with
plain date.today() (whatever timezone the test runner's OS is in), so
db._now_local_date is monkeypatched to match that exactly -- otherwise db.py's
real BOT_TIMEZONE-aware clock (Asia/Singapore by default) would disagree with
date.today() by a day for part of every day, on any machine not set to that
same timezone. fx.get_rate is monkeypatched by default so tests never hit the
real network -- individual tests override it when they specifically want to
exercise currency conversion.
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ai
import config
import db
import fx


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "_now_local_date", lambda: date.today())
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def _fail_if_called(from_ccy, to_ccy):
        raise AssertionError(
            f"fx.get_rate({from_ccy!r}, {to_ccy!r}) was called without a test override -- "
            "network calls must not happen in tests."
        )
    monkeypatch.setattr(fx, "get_rate", _fail_if_called)
    yield


@pytest.fixture(autouse=True)
def _passthrough_narration(monkeypatch):
    """replies._reply now runs EVERY reply through ai.narrate_reply by
    default (see replies.py's module docstring) -- without this fixture,
    every single test that sends a reply on the free-text/command surface
    (hundreds of them, across nearly every test file) would attempt a real
    network call to the Anthropic API, the exact thing _no_real_network
    above exists to prevent for fx.get_rate. Default every test to a pure
    passthrough (the deterministic text, unchanged) so the large existing
    body of tests asserting exact confirmation wording keeps working
    without each one having to know or care that narration exists. A test
    that actually wants to exercise the narration mechanism itself
    overrides this within its own body (monkeypatch stacking: the later
    setattr wins) -- see tests/test_replies.py."""
    monkeypatch.setattr(ai, "narrate_reply", lambda text, *args, **kwargs: text)
    yield


CHAT = 12345
