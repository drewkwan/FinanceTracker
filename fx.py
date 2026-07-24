"""
Currency conversion for multi-currency logging.

Uses Frankfurter (https://www.frankfurter.app), a free no-API-key exchange
rate service backed by European Central Bank reference rates. Rates update
once a day, so an in-memory per-day cache avoids hammering it.
"""

import json
import logging
import urllib.request
from datetime import date

import config

logger = logging.getLogger(__name__)

_rate_cache = {}  # (from_ccy, to_ccy, iso_date) -> rate


def get_rate(from_ccy: str, to_ccy: str) -> float:
    from_ccy = from_ccy.upper()
    to_ccy = to_ccy.upper()
    if from_ccy == to_ccy:
        return 1.0

    key = (from_ccy, to_ccy, date.today().isoformat())
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
