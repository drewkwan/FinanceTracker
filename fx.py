"""
Currency conversion for multi-currency logging.

Uses Frankfurter (https://www.frankfurter.app), a free no-API-key exchange
rate service backed by European Central Bank reference rates. Rates update
once a day, so an in-memory per-day cache avoids hammering it.
"""

import json
import logging
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

_rate_cache = {}  # (from_ccy, to_ccy, iso_date) -> rate


def _now_local_date() -> date:
    """Local calendar date in config.BOT_TIMEZONE -- deliberately NOT
    date.today() (the server's/OS date, UTC on Railway). That was a real
    bug: right after local midnight in Asia/Singapore (UTC+8) but before
    UTC midnight, date.today() still returns YESTERDAY's date for up to 8
    hours, so a rate fetched in that window got cached under the wrong day
    -- silently disagreeing with every other "what day is it" computation
    in the app (db.today_str() is the canonical one; see its docstring).
    A twin of db._now_local_date, not imported from there: db.py already
    imports fx.py (for currency conversion), so fx.py importing db.py back
    would be a circular import. Kept in sync by hand -- change one, change
    the other. Exposed as its own function (like db._now_local_date) so
    tests can monkeypatch it directly instead of needing to fake the
    system clock."""
    return datetime.now(ZoneInfo(config.BOT_TIMEZONE)).date()


def get_rate(from_ccy: str, to_ccy: str) -> float:
    from_ccy = from_ccy.upper()
    to_ccy = to_ccy.upper()
    if from_ccy == to_ccy:
        return 1.0

    key = (from_ccy, to_ccy, _now_local_date().isoformat())
    if key in _rate_cache:
        return _rate_cache[key]

    url = f"https://api.frankfurter.app/latest?from={from_ccy}&to={to_ccy}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    rate = data["rates"][to_ccy]
    _rate_cache[key] = rate
    return rate


def to_base(amount: float, currency: str | None) -> float:
    """Converts `amount` in `currency` into config.BASE_CURRENCY. Falls back
    to a 1:1 conversion (with a logged warning) if the rate lookup fails, so
    a network hiccup never blocks logging an expense."""
    currency = (currency or config.BASE_CURRENCY).upper()
    if currency == config.BASE_CURRENCY:
        return amount
    try:
        rate = get_rate(currency, config.BASE_CURRENCY)
        return amount * rate
    except Exception:
        logger.exception("FX lookup failed for %s -> %s, using 1:1 as a fallback", currency, config.BASE_CURRENCY)
        return amount


def normalize_currency(raw: str | None) -> str | None:
    """Returns a valid known currency code from user input, or None if it
    doesn't match anything we recognize."""
    if not raw:
        return None
    raw = raw.strip().upper()
    return raw if raw in config.KNOWN_CURRENCIES else None
