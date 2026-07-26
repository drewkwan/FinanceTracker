"""
Trend-analytics tests: the period-boundary math (trends.period_bounds) and
the day-of-week / budget-adherence aggregation used by /summary.

trends.period_bounds is where a real off-by-one bug lived (an 8-day
"current" window compared against a 7-day "previous" window) -- these tests
exist specifically to catch a regression of that class of bug, not just to
check happy-path totals. It's a pure function with no framework
dependencies, so these tests don't need python-telegram-bot or anthropic
installed at all.
"""

from datetime import date, timedelta

import db
from conftest import CHAT

from trends import period_bounds as _period_bounds


def _insert(chat_id, day, amount, category="Food"):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, currency, amount_base, description, category, "
            "is_claimable, expense_date) VALUES (?, ?, 'SGD', ?, 'x', ?, 0, ?)",
            (chat_id, amount, amount, category, day.isoformat()),
        )


# ---------- _period_bounds: pure boundary-math regression tests ----------

def test_week_bounds_are_equal_length_and_adjacent():
    today = date(2026, 7, 26)  # arbitrary fixed anchor, a Sunday
    b = _period_bounds("week", today)
    assert b["length"] == 7
    assert b["current_start"] == today - timedelta(days=6)
    assert b["current_end"] == today + timedelta(days=1)
    # previous period must be exactly 7 days too, ending exactly where current starts
    assert (b["prev_end"] - b["prev_start"]).days == 7
    assert (b["current_end"] - b["current_start"]).days == 7
    assert b["prev_end"] == b["current_start"]  # adjacent, no gap
    assert b["prev_start"] < b["current_start"]  # no overlap


def test_month_bounds_are_equal_length_and_adjacent():
    today = date(2026, 7, 26)
    b = _period_bounds("month", today)
    assert b["length"] == 30
    assert (b["current_end"] - b["current_start"]).days == 30
    assert (b["prev_end"] - b["prev_start"]).days == 30
    assert b["prev_end"] == b["current_start"]


def test_today_bounds_are_single_day():
    today = date(2026, 7, 26)
    b = _period_bounds("today", today)
    assert b["length"] == 1
    assert b["current_start"] == today
    assert (b["current_end"] - b["current_start"]).days == 1


def test_unknown_period_defaults_to_week():
    today = date(2026, 7, 26)
    b = _period_bounds("nonsense", today)
    assert b["period"] == "week"
    assert b["length"] == 7


def test_period_arg_is_case_insensitive():
    today = date(2026, 7, 26)
    assert _period_bounds("MONTH", today)["period"] == "month"
    assert _period_bounds("Today", today)["period"] == "today"


# ---------- period-over-period totals, using db against real bounds ----------

def test_current_and_previous_week_totals_match_expected_split():
    db.get_or_create_user(CHAT)
    today = date.today()
    b = _period_bounds("week", today)
    for i in range(7):
        _insert(CHAT, today - timedelta(days=i), 20)  # this week: 7 * 20 = 140
    for i in range(7, 14):
        _insert(CHAT, today - timedelta(days=i), 10)  # last week: 7 * 10 = 70

    current = db.get_category_totals(CHAT, b["current_start"].isoformat(), b["current_end"].isoformat())
    previous = db.get_category_totals(CHAT, b["prev_start"].isoformat(), b["prev_end"].isoformat())

    assert sum(r["total"] for r in current) == 140
    assert sum(r["total"] for r in previous) == 70


# ---------- day-of-week averages ----------

def test_day_of_week_average_flags_the_spike_day():
    db.get_or_create_user(CHAT)
    today = date.today()
    lookback_start = today - timedelta(days=55)  # 56 days inclusive of today = 8 weeks
    d = lookback_start
    spike_dow = 5  # Saturday
    while d <= today:
        amount = 50 if d.weekday() == spike_dow else 10
        _insert(CHAT, d, amount)
        d += timedelta(days=1)

    daily = db.get_daily_totals(CHAT, lookback_start.isoformat(), (today + timedelta(days=1)).isoformat())
    dow_totals = [0.0] * 7
    dow_counts = [0] * 7
    d = lookback_start
    while d <= today:
        dow = d.weekday()
        dow_totals[dow] += daily.get(d.isoformat(), 0.0)
        dow_counts[dow] += 1
        d += timedelta(days=1)
    averages = {i: dow_totals[i] / dow_counts[i] for i in range(7) if dow_counts[i] > 0}

    assert averages[spike_dow] == 50
    assert all(v == 10 for k, v in averages.items() if k != spike_dow)


# ---------- budget adherence ----------

def test_days_under_target_counts_correctly():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    today = date.today()
    b = _period_bounds("week", today)
    for i in range(7):
        day = b["current_start"] + timedelta(days=i)
        amount = 200 if i == 3 else 50  # one overspend day in the middle
        _insert(CHAT, day, amount)

    period_daily = db.get_daily_totals(CHAT, b["current_start"].isoformat(), b["current_end"].isoformat())
    user = db.get_or_create_user(CHAT)
    days_under = sum(
        1 for i in range(b["length"])
        if period_daily.get((b["current_start"] + timedelta(days=i)).isoformat(), 0.0) <= user["daily_target"]
    )
    assert days_under == 6  # 7 days minus the one overspend day
