"""
Core money-math tests: rollover, streaks, budget alerts, currency conversion,
and balance-safe undo/edit/delete.
"""

from datetime import date, timedelta

import db
from conftest import CHAT


def _insert_on(chat_id, day, amount, currency="SGD", category="Food", is_claimable=False):
    """Backdoor insert for a specific expense_date, bypassing add_expense's
    'always today' behavior so we can simulate past days."""
    amount_base = amount if currency == "SGD" else amount  # tests that need real conversion override fx
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, currency, amount_base, description, category, "
            "is_claimable, expense_date) VALUES (?, ?, ?, ?, 'x', ?, ?, ?)",
            (chat_id, amount, currency, amount_base, category, int(is_claimable), day.isoformat()),
        )


# ---------- daily target / user bootstrap ----------

def test_new_user_gets_default_target():
    user = db.get_or_create_user(CHAT)
    assert user["daily_target"] == 100.0
    assert user["balance"] == 0.0
    assert user["current_streak"] == 0


def test_set_daily_target_updates_existing_user():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 250)
    assert db.get_or_create_user(CHAT)["daily_target"] == 250


# ---------- rollover ----------

def test_rollover_credits_leftover_to_balance():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    yesterday = date.today() - timedelta(days=1)
    _insert_on(CHAT, yesterday, 30)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_rollover_date = ? WHERE chat_id = ?",
            (yesterday.isoformat(), CHAT),
        )
    result = db.ensure_rollover(CHAT)
    assert len(result["rollovers"]) == 1
    assert result["rollovers"][0] == (yesterday.isoformat(), 70.0)  # 100 target - 30 spent
    assert db.get_or_create_user(CHAT)["balance"] == 70.0


def test_rollover_overspend_reduces_balance():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    yesterday = date.today() - timedelta(days=1)
    _insert_on(CHAT, yesterday, 150)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_rollover_date = ? WHERE chat_id = ?",
            (yesterday.isoformat(), CHAT),
        )
    db.ensure_rollover(CHAT)
    assert db.get_or_create_user(CHAT)["balance"] == -50.0  # 100 - 150


def test_rollover_handles_multi_day_offline_gap():
    """If the bot was offline for 3 days, ensure_rollover walks forward one
    day at a time using the *current* daily_target for each missed day."""
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    three_days_ago = date.today() - timedelta(days=3)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_rollover_date = ? WHERE chat_id = ?",
            (three_days_ago.isoformat(), CHAT),
        )
    # spend on two of the three missed days, nothing on the third
    _insert_on(CHAT, three_days_ago, 40)
    _insert_on(CHAT, three_days_ago + timedelta(days=1), 120)  # overspend day
    result = db.ensure_rollover(CHAT)
    assert len(result["rollovers"]) == 3
    # 60 (under) + -20 (over) + 100 (nothing spent) = 140
    assert db.get_or_create_user(CHAT)["balance"] == 140.0


def test_ensure_rollover_is_noop_same_day():
    db.get_or_create_user(CHAT)
    result = db.ensure_rollover(CHAT)
    assert result["rollovers"] == []


# ---------- streaks ----------

def test_streak_increments_on_days_within_target():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    three_days_ago = date.today() - timedelta(days=3)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_rollover_date = ? WHERE chat_id = ?",
            (three_days_ago.isoformat(), CHAT),
        )
    for i in range(3):
        _insert_on(CHAT, three_days_ago + timedelta(days=i), 50)  # always under target
    db.ensure_rollover(CHAT)
    user = db.get_or_create_user(CHAT)
    assert user["current_streak"] == 3
    assert user["best_streak"] == 3


def test_streak_resets_to_zero_on_overspend_day():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    two_days_ago = date.today() - timedelta(days=2)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_rollover_date = ?, current_streak = 5, best_streak = 5 WHERE chat_id = ?",
            (two_days_ago.isoformat(), CHAT),
        )
    _insert_on(CHAT, two_days_ago, 50)       # under target: streak -> 6
    _insert_on(CHAT, two_days_ago + timedelta(days=1), 150)  # over target: streak -> 0
    db.ensure_rollover(CHAT)
    user = db.get_or_create_user(CHAT)
    assert user["current_streak"] == 0
    assert user["best_streak"] == 6  # best is preserved even after the reset


# ---------- budget alert ----------

def test_alert_fires_once_after_crossing_threshold():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    _insert_on(CHAT, date.today(), 95)  # 95% of 100 target, threshold is 90%
    assert db.maybe_alert(CHAT) is True
    assert db.maybe_alert(CHAT) is False  # already fired today, no repeat


def test_alert_does_not_fire_below_threshold():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    _insert_on(CHAT, date.today(), 50)
    assert db.maybe_alert(CHAT) is False


# ---------- currency conversion ----------

def test_add_expense_converts_foreign_currency(monkeypatch):
    import fx
    monkeypatch.setattr(fx, "get_rate", lambda f, t: 1.35)  # 1 USD = 1.35 SGD
    db.get_or_create_user(CHAT)
    expense_id = db.add_expense(CHAT, 20, "USD", "taxi", "Transport")
    row = db.get_expense(CHAT, expense_id)
    assert row["amount"] == 20
    assert row["currency"] == "USD"
    assert row["amount_base"] == 27.0  # 20 * 1.35


def test_add_expense_same_currency_skips_conversion():
    db.get_or_create_user(CHAT)
    expense_id = db.add_expense(CHAT, 20, "SGD", "lunch", "Food")
    row = db.get_expense(CHAT, expense_id)
    assert row["amount_base"] == 20  # no fx.get_rate call needed (would fail via conftest guard)


# ---------- balance-safe undo / edit / delete ----------

def test_delete_past_day_expense_credits_balance_back():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    yesterday = date.today() - timedelta(days=1)
    _insert_on(CHAT, yesterday, 30)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_rollover_date = ? WHERE chat_id = ?",
            (yesterday.isoformat(), CHAT),
        )
    db.ensure_rollover(CHAT)  # balance becomes 70
    expense = db.get_recent_expenses(CHAT, limit=1)[0]
    db.delete_expense(CHAT, expense["id"])
    # deleting a past expense refunds its amount_base back into balance
    assert db.get_or_create_user(CHAT)["balance"] == 100.0  # 70 + 30


def test_delete_today_expense_does_not_touch_balance():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    expense_id = db.add_expense(CHAT, 30, "SGD", "lunch", "Food")
    db.delete_expense(CHAT, expense_id)
    assert db.get_or_create_user(CHAT)["balance"] == 0.0  # unaffected; today's spend is live, not rolled


def test_edit_past_day_expense_adjusts_balance_by_delta():
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    yesterday = date.today() - timedelta(days=1)
    _insert_on(CHAT, yesterday, 30)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_rollover_date = ? WHERE chat_id = ?",
            (yesterday.isoformat(), CHAT),
        )
    db.ensure_rollover(CHAT)  # balance = 70
    expense = db.get_recent_expenses(CHAT, limit=1)[0]
    db.edit_expense(CHAT, expense["id"], new_amount=50)  # was 30, now 50 -> spent 20 more
    assert db.get_or_create_user(CHAT)["balance"] == 50.0  # 70 - 20


def test_undo_removes_most_recent_only():
    db.get_or_create_user(CHAT)
    db.add_expense(CHAT, 10, "SGD", "coffee", "Food")
    second_id = db.add_expense(CHAT, 20, "SGD", "lunch", "Food")
    removed = db.delete_most_recent(CHAT)
    assert removed["id"] == second_id
    remaining = db.get_recent_expenses(CHAT)
    assert len(remaining) == 1
    assert remaining[0]["description"] == "coffee"


# ---------- claimables ----------

def test_claimables_are_separate_from_balance():
    db.get_or_create_user(CHAT)
    db.add_expense(CHAT, 300, "SGD", "team dinner", "Food", is_claimable=True)
    status = db.get_status(CHAT)
    assert status["pending_claimable"] == 300
    assert status["balance"] == 0.0  # claimables never touch the daily allowance


def test_clear_claimables_marks_all_as_claimed():
    db.get_or_create_user(CHAT)
    db.add_expense(CHAT, 100, "SGD", "flight", "Travel", is_claimable=True)
    db.add_expense(CHAT, 50, "SGD", "cab", "Transport", is_claimable=True)
    count, total = db.clear_claimables(CHAT)
    assert count == 2
    assert total == 150
    assert db.get_pending_claimables(CHAT) == []
