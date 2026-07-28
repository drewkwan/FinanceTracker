"""
SQLite persistence layer for the expense bot.

Design (see README for the full explanation):

- `users`: one row per Telegram chat. Tracks the current daily target and a
  running `balance` -- the cumulative rollover of unspent (or overspent) money
  from previous days, plus streak counters and alert throttling.
- `expenses`: every logged expense. `amount`/`currency` are what you actually
  paid; `amount_base` is that converted into BASE_CURRENCY and is what all
  budget math uses, so mixed-currency spending still adds up correctly.
  `is_claimable` separates "claim this back from someone" spending from your
  personal daily allowance. `is_claimed` marks claimables that have been
  reimbursed and cleared.

Rollover math (matches the spec exactly):
  Day 1: target=$100, spend $30 -> leftover = $100 - $30 = $70 -> balance += 70
  Day 2: available = target($100) + balance($70) = $170
"""

import sqlite3
from datetime import date, timedelta
from contextlib import contextmanager

import config
import fx


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(conn, table, column, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                daily_target REAL NOT NULL DEFAULT 0,
                balance REAL NOT NULL DEFAULT 0,
                last_rollover_date TEXT NOT NULL,
                last_alert_date TEXT,
                current_streak INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT '{config.BASE_CURRENCY}',
                amount_base REAL NOT NULL,
                description TEXT,
                category TEXT,
                is_claimable INTEGER NOT NULL DEFAULT 0,
                is_claimed INTEGER NOT NULL DEFAULT 0,
                expense_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Forward-compatible migration in case this is an existing db from
        # before currency/streak/alert support was added.
        _add_column_if_missing(conn, "users", "last_alert_date", "last_alert_date TEXT")
        _add_column_if_missing(conn, "users", "current_streak", "current_streak INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "users", "best_streak", "best_streak INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "expenses", "currency", f"currency TEXT NOT NULL DEFAULT '{config.BASE_CURRENCY}'")
        _add_column_if_missing(conn, "expenses", "amount_base", "amount_base REAL")
        # Backfill amount_base for any pre-existing rows (assume same as amount if it was null).
        conn.execute("UPDATE expenses SET amount_base = amount WHERE amount_base IS NULL")


def _now_local_date():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(config.BOT_TIMEZONE)).date()


def today_str():
    return _now_local_date().isoformat()


def get_or_create_user(chat_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (chat_id, daily_target, balance, last_rollover_date) VALUES (?, ?, 0, ?)",
                (chat_id, config.DEFAULT_DAILY_TARGET, today_str()),
            )
            row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row)


def set_daily_target(chat_id, amount):
    get_or_create_user(chat_id)
    with get_conn() as conn:
        conn.execute("UPDATE users SET daily_target = ? WHERE chat_id = ?", (amount, chat_id))


def _spent_on(conn, chat_id, day_str):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_base), 0) AS total FROM expenses "
        "WHERE chat_id = ? AND expense_date = ? AND is_claimable = 0",
        (chat_id, day_str),
    ).fetchone()
    return row["total"]


def ensure_rollover(chat_id):
    """
    Catches the user's balance (and streak) up to today. Safe to call before
    every command. If the bot was offline for multiple days, rolls forward
    one day at a time using whatever the current daily_target is (a
    reasonable approximation for missed days).

    Returns {"rollovers": [(date, leftover), ...], "new_best_streak": int|None}
    """
    user = get_or_create_user(chat_id)
    last = date.fromisoformat(user["last_rollover_date"])
    today = date.fromisoformat(today_str())
    result = {"rollovers": [], "new_best_streak": None}
    if today <= last:
        return result

    with get_conn() as conn:
        balance = user["balance"]
        target = user["daily_target"]
        streak = user["current_streak"]
        best = user["best_streak"]
        d = last
        while d < today:
            spent = _spent_on(conn, chat_id, d.isoformat())
            leftover = target - spent
            balance += leftover
            result["rollovers"].append((d.isoformat(), leftover))

            if spent <= target:
                streak += 1
                if streak > best:
                    best = streak
                    result["new_best_streak"] = best
            else:
                streak = 0

            d += timedelta(days=1)

        conn.execute(
            "UPDATE users SET balance = ?, last_rollover_date = ?, current_streak = ?, best_streak = ? "
            "WHERE chat_id = ?",
            (balance, today.isoformat(), streak, best, chat_id),
        )
    return result


def get_status(chat_id):
    ensure_rollover(chat_id)
    user = get_or_create_user(chat_id)
    today = today_str()
    with get_conn() as conn:
        spent_today = _spent_on(conn, chat_id, today)
        pending_claimable = conn.execute(
            "SELECT COALESCE(SUM(amount_base), 0) AS total FROM expenses "
            "WHERE chat_id = ? AND is_claimable = 1 AND is_claimed = 0",
            (chat_id,),
        ).fetchone()["total"]
    available_today = user["daily_target"] + user["balance"] - spent_today
    return {
        "daily_target": user["daily_target"],
        "balance": user["balance"],
        "spent_today": spent_today,
        "available_today": available_today,
        "pending_claimable": pending_claimable,
        "current_streak": user["current_streak"],
        "best_streak": user["best_streak"],
    }


def maybe_alert(chat_id):
    """Returns True (and marks it sent) the first time today that spending
    crosses config.BUDGET_ALERT_THRESHOLD of today's available budget."""
    status = get_status(chat_id)
    today = today_str()
    with get_conn() as conn:
        user = conn.execute("SELECT last_alert_date FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        if user["last_alert_date"] == today:
            return False
        available_total = status["daily_target"] + status["balance"]
        if available_total <= 0:
            return False
        ratio = status["spent_today"] / available_total
        if ratio >= config.BUDGET_ALERT_THRESHOLD:
            conn.execute("UPDATE users SET last_alert_date = ? WHERE chat_id = ?", (today, chat_id))
            return True
    return False


def add_expense(chat_id, amount, currency, description, category, is_claimable=False):
    ensure_rollover(chat_id)
    get_or_create_user(chat_id)
    currency = (currency or config.BASE_CURRENCY).upper()
    amount_base = fx.to_base(amount, currency)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, currency, amount_base, description, category, "
            "is_claimable, expense_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, amount, currency, amount_base, description, category, int(is_claimable), today_str()),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def clear_claimables(chat_id):
    """Marks all pending claimables as claimed. Returns (count, total_base)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_base), 0) AS total FROM expenses "
            "WHERE chat_id = ? AND is_claimable = 1 AND is_claimed = 0",
            (chat_id,),
        ).fetchone()
        conn.execute(
            "UPDATE expenses SET is_claimed = 1 WHERE chat_id = ? AND is_claimable = 1 AND is_claimed = 0",
            (chat_id,),
        )
        return rows["n"], rows["total"]


def get_pending_claimables(chat_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, amount, currency, description, category, expense_date FROM expenses "
            "WHERE chat_id = ? AND is_claimable = 1 AND is_claimed = 0 ORDER BY id",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_expenses(chat_id, limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, amount, currency, description, category, is_claimable, is_claimed, expense_date "
            "FROM expenses WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_expense(chat_id, expense_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM expenses WHERE id = ? AND chat_id = ?", (expense_id, chat_id)
        ).fetchone()
        return dict(row) if row else None


def _adjust_balance_for_past_day(conn, chat_id, delta_base):
    """delta_base > 0 means less was spent than before (refund to balance)."""
    if delta_base == 0:
        return
    conn.execute("UPDATE users SET balance = balance + ? WHERE chat_id = ?", (delta_base, chat_id))


def delete_expense(chat_id, expense_id):
    """Deletes an expense. If it was a past (already rolled-over) non-claimable
    day, credits the balance back so history stays consistent. Returns the
    deleted row as a dict, or None if it didn't exist / wasn't this chat's."""
    ensure_rollover(chat_id)
    row = get_expense(chat_id, expense_id)
    if row is None:
        return None
    with get_conn() as conn:
        if not row["is_claimable"] and row["expense_date"] < today_str():
            _adjust_balance_for_past_day(conn, chat_id, row["amount_base"])
        conn.execute("DELETE FROM expenses WHERE id = ? AND chat_id = ?", (expense_id, chat_id))
    return row


def delete_most_recent(chat_id):
    """Undo: deletes the single most recent expense for this chat."""
    recent = get_recent_expenses(chat_id, limit=1)
    if not recent:
        return None
    return delete_expense(chat_id, recent[0]["id"])


def restore_deleted_expense(chat_id, row):
    """Re-inserts a previously deleted expense row exactly as it was (same
    amount/currency/amount_base/description/category/claimable-ness/date),
    applying the exact opposite balance adjustment delete_expense would have
    made. Used to support one-step 'undo' after a natural-language
    correction deletes the wrong entry -- gets a fresh row id, since SQLite
    won't recycle the old one, but every other field is preserved."""
    ensure_rollover(chat_id)
    with get_conn() as conn:
        if not row["is_claimable"] and row["expense_date"] < today_str():
            _adjust_balance_for_past_day(conn, chat_id, -row["amount_base"])
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, currency, amount_base, description, category, "
            "is_claimable, is_claimed, expense_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, row["amount"], row["currency"], row["amount_base"], row["description"],
             row["category"], row["is_claimable"], row.get("is_claimed", 0), row["expense_date"]),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return get_expense(chat_id, new_id)


def edit_expense(chat_id, expense_id, new_amount=None, new_currency=None,
                  new_description=None, new_category=None):
    """Updates an expense in place. Only touches fields that are passed in.
    Recomputes amount_base if amount or currency changed, and (for
    non-claimable expenses on already-rolled-over days) adjusts the running
    balance by the difference so history stays consistent. Returns the
    updated row, or None if it didn't exist / wasn't this chat's."""
    ensure_rollover(chat_id)
    row = get_expense(chat_id, expense_id)
    if row is None:
        return None

    final_amount = new_amount if new_amount is not None else row["amount"]
    final_currency = (new_currency or row["currency"]).upper()
    recompute = new_amount is not None or new_currency is not None
    new_amount_base = fx.to_base(final_amount, final_currency) if recompute else row["amount_base"]

    final_description = new_description if new_description is not None else row["description"]
    final_category = new_category if new_category is not None else row["category"]

    with get_conn() as conn:
        if not row["is_claimable"] and row["expense_date"] < today_str():
            delta = row["amount_base"] - new_amount_base
            _adjust_balance_for_past_day(conn, chat_id, delta)
        conn.execute(
            "UPDATE expenses SET amount = ?, currency = ?, amount_base = ?, description = ?, category = ? "
            "WHERE id = ? AND chat_id = ?",
            (final_amount, final_currency, new_amount_base, final_description, final_category,
             expense_id, chat_id),
        )
    return get_expense(chat_id, expense_id)


def edit_expense_date(chat_id, expense_id, new_date_str):
    """Moves a non-claimable expense to a different expense_date, adjusting
    the running balance so history stays consistent. new_date_str is an ISO
    date string; dates after today are clamped to today (this app doesn't
    support logging into the future).

    The balance math depends on whether each side of the move is "today"
    (live -- spent_today is computed fresh on every read, nothing stored) or
    an already-rolled-over past day (baked into the single cumulative
    `balance` number at rollover time):

      - past day -> past day: the amount is removed from one already-rolled
        day's spend and added to another already-rolled day's spend. Since
        both use the same (current) daily_target approximation and both feed
        the same cumulative `balance`, the two adjustments cancel out
        exactly -- net zero change to balance.
      - today (live) -> past day: the amount leaves today's live spend
        (automatic once expense_date changes) and now retroactively counts
        against a day whose rollover already happened, so that day's
        leftover -- and therefore balance -- decreases by amount_base.
      - past day -> today (live): the reverse -- the amount is removed from
        an already-rolled day (balance increases by amount_base) and now
        counts against today's live, not-yet-rolled spend instead.
      - same date (no-op): nothing to do.

    Claimable expenses never touch balance, so their date can move freely
    with no adjustment. Returns the updated row, or None if the expense
    doesn't exist / isn't this chat's.
    """
    ensure_rollover(chat_id)
    row = get_expense(chat_id, expense_id)
    if row is None:
        return None

    today = today_str()
    if new_date_str > today:
        new_date_str = today
    old_date_str = row["expense_date"]

    if new_date_str == old_date_str:
        return row

    with get_conn() as conn:
        if not row["is_claimable"]:
            old_is_past = old_date_str < today
            new_is_past = new_date_str < today
            if old_is_past and not new_is_past:
                # leaving an already-rolled day -> that day's leftover goes up
                _adjust_balance_for_past_day(conn, chat_id, row["amount_base"])
            elif not old_is_past and new_is_past:
                # entering an already-rolled day -> that day's leftover goes down
                _adjust_balance_for_past_day(conn, chat_id, -row["amount_base"])
            # past -> past nets to zero (see docstring); no adjustment needed
        conn.execute(
            "UPDATE expenses SET expense_date = ? WHERE id = ? AND chat_id = ?",
            (new_date_str, expense_id, chat_id),
        )
    return get_expense(chat_id, expense_id)


def get_all_chat_ids():
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id FROM users").fetchall()
        return [r["chat_id"] for r in rows]


def get_category_totals(chat_id, start_date, end_date):
    """Category breakdown (in base currency) of non-claimable spending in
    [start_date, end_date) -- both ISO date strings, end_date exclusive."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COALESCE(SUM(amount_base), 0) AS total, COUNT(*) AS n FROM expenses "
            "WHERE chat_id = ? AND is_claimable = 0 AND expense_date >= ? AND expense_date < ? "
            "GROUP BY category ORDER BY total DESC",
            (chat_id, start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_totals(chat_id, start_date, end_date):
    """Per-day non-claimable totals (base currency) in [start_date, end_date)
    -- both ISO date strings, end_date exclusive. Returns {date_str: total},
    omitting days with no spend (callers should default missing days to 0)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT expense_date, COALESCE(SUM(amount_base), 0) AS total FROM expenses "
            "WHERE chat_id = ? AND is_claimable = 0 AND expense_date >= ? AND expense_date < ? "
            "GROUP BY expense_date",
            (chat_id, start_date, end_date),
        ).fetchall()
        return {r["expense_date"]: r["total"] for r in rows}
