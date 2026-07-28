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


CHAT = 12345
