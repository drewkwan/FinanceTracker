"""
Shared pytest fixtures.

Each test gets its own throwaway SQLite file and a UTC "today" (rather than
BOT_TIMEZONE's Asia/Singapore default), so results are deterministic no
matter what time or timezone the test runner is in. fx.get_rate is monkeypatched
by default so tests never hit the real network -- individual tests override it
when they specifically want to exercise currency conversion.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import db
import fx


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "BOT_TIMEZONE", "UTC")
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
