"""
Pure period-boundary math for /summary's trend analytics.

Deliberately has zero dependencies on python-telegram-bot, anthropic, or the
database -- just dates in, a plain dict out. Kept separate from bot.py so it
can be unit tested directly (see tests/test_trends.py) without needing a
Telegram token, an Anthropic key, or any mocking of the framework layer.

This is also where a real off-by-one bug lived once: an 8-day "current"
window was being compared against a 7-day "previous" window. The tests for
this module exist specifically to catch a regression of that class of bug.
"""

from datetime import date, timedelta


def period_bounds(period_arg: str, today: date) -> dict:
    """Computes the current/previous comparison windows for a given period
    argument ("today", "week", "month", or anything else -- which defaults
    to "week") anchored on `today`.

    Returns a dict with: period, length, today, tomorrow, current_start,
    current_end, prev_start, prev_end. The current and previous windows are
    always exactly `length` days each, adjacent, and non-overlapping
    (current_end/current_start are exclusive upper bounds, so they compose
    directly with half-open DB range queries).
    """
    period_arg = (period_arg or "week").lower()
    tomorrow = today + timedelta(days=1)

    if period_arg == "today":
        period, length = "today", 1
    elif period_arg == "month":
        period, length = "month", 30
    else:
        period, length = "week", 7

    current_start = today - timedelta(days=length - 1)
    current_end = tomorrow
    prev_start = current_start - timedelta(days=length)
    prev_end = current_start

    return {
        "period": period,
        "length": length,
        "today": today,
        "tomorrow": tomorrow,
        "current_start": current_start,
        "current_end": current_end,
        "prev_start": prev_start,
        "prev_end": prev_end,
    }
