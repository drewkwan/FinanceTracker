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
- `meals`: one row per logged food/drink item or entry (not one row per meal
  slot -- matches how this is actually used: logged as things are eaten
  throughout the day, not decomposed into breakfast/lunch/dinner buckets).
  Calories are stored as a `calories_low`/`calories_high`/`calories_estimate`
  range rather than a single false-precise number, matching the estimate
  style already validated by hand in ChatGPT. `water_ml` is a separate axis
  (hydration, not calories) and is null on entries that aren't plain water.
- `workouts`: one row per logged session. `notes` stays free text rather than
  forcing IPPT times or tennis sets into rigid columns before it's clear
  what's worth tracking structurally.
- `vitals`: one row per daily check-in (weight, sleep, knee pain, or any
  subset -- all nullable, since not every check-in reports everything).
  Separate from `workouts` because it's reported on its own cadence, not
  tied to a specific session.

Rollover math (matches the spec exactly):
  Day 1: target=$100, spend $30 -> leftover = $100 - $30 = $70 -> balance += 70
  Day 2: available = target($100) + balance($70) = $170
"""

import json
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                meal_type TEXT,
                items TEXT,
                calories_low REAL,
                calories_high REAL,
                calories_estimate REAL,
                water_ml REAL,
                meal_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                activity TEXT,
                duration_min REAL,
                distance_km REAL,
                notes TEXT,
                workout_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vitals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                weight_kg REAL,
                sleep_hours REAL,
                knee_pain REAL,
                notes TEXT,
                vitals_date TEXT NOT NULL,
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


def get_month_to_date_total(chat_id):
    """Sum of non-claimable spend (base currency) from the 1st of the
    current month through today (inclusive). Computed fresh from the raw
    expense rows every call -- no stored running total, so this needed no
    schema change and naturally resets to zero on the 1st of every month
    with zero migration risk, alongside (not replacing) the existing
    rolling `balance` on the user row."""
    today = date.fromisoformat(today_str())
    month_start = today.replace(day=1)
    tomorrow = today + timedelta(days=1)
    totals = get_category_totals(chat_id, month_start.isoformat(), tomorrow.isoformat())
    return {
        "total": round(sum(r["total"] for r in totals), 2),
        "month_start": month_start.isoformat(),
        "today": today.isoformat(),
        "days_elapsed": (today - month_start).days + 1,
    }


# ---------- meals ----------
# Each row is one logged item/entry, not one row per meal slot -- see the
# module docstring. calories_estimate is the number everything else (running
# totals, /summary-style rollups) sums; low/high are kept for display only.

def _meal_row(row):
    d = dict(row)
    try:
        d["items"] = json.loads(d["items"]) if d["items"] else []
    except (TypeError, json.JSONDecodeError):
        d["items"] = []
    return d


def add_meal(chat_id, meal_type, items, calories_low, calories_high, calories_estimate, water_ml=None):
    """items: list of strings. Never raises on bad estimate math -- a null
    calories_estimate is stored as-is rather than blocking the log (mirrors
    fx.py's "never block on an estimate/lookup failure" discipline)."""
    get_or_create_user(chat_id)
    items_json = json.dumps(items or [])
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meals (chat_id, meal_type, items, calories_low, calories_high, "
            "calories_estimate, water_ml, meal_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, meal_type, items_json, calories_low, calories_high, calories_estimate,
             water_ml, today_str()),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_recent_meals(chat_id, limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [_meal_row(r) for r in rows]


def get_meal(chat_id, meal_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meals WHERE id = ? AND chat_id = ?", (meal_id, chat_id)
        ).fetchone()
        return _meal_row(row) if row else None


def get_daily_meal_totals(chat_id, day_str):
    """Running totals for one day -- the reply pattern this mirrors (from the
    ChatGPT thread this replaces) always shows a running calorie/water total
    alongside each new item, not just the item just logged."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(calories_estimate), 0) AS calories, "
            "COALESCE(SUM(water_ml), 0) AS water_ml FROM meals "
            "WHERE chat_id = ? AND meal_date = ?",
            (chat_id, day_str),
        ).fetchone()
        return {"calories": row["calories"], "water_ml": row["water_ml"]}


def edit_meal_date(chat_id, meal_id, new_date_str):
    """Moves a meal to a different meal_date. Unlike expenses, meals don't
    feed a rolling balance, so this is a plain field update -- no balance
    adjustment needed. Dates after today are clamped to today."""
    row = get_meal(chat_id, meal_id)
    if row is None:
        return None
    today = today_str()
    if new_date_str > today:
        new_date_str = today
    with get_conn() as conn:
        conn.execute(
            "UPDATE meals SET meal_date = ? WHERE id = ? AND chat_id = ?",
            (new_date_str, meal_id, chat_id),
        )
    return get_meal(chat_id, meal_id)


def delete_meal(chat_id, meal_id):
    row = get_meal(chat_id, meal_id)
    if row is None:
        return None
    with get_conn() as conn:
        conn.execute("DELETE FROM meals WHERE id = ? AND chat_id = ?", (meal_id, chat_id))
    return row


def delete_most_recent_meal(chat_id):
    recent = get_recent_meals(chat_id, limit=1)
    if not recent:
        return None
    return delete_meal(chat_id, recent[0]["id"])


def restore_deleted_meal(chat_id, row):
    """Re-inserts a previously deleted meal row exactly as it was. Used for
    one-step 'undo' after a natural-language correction deletes the wrong
    entry -- gets a fresh row id, every other field preserved."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meals (chat_id, meal_type, items, calories_low, calories_high, "
            "calories_estimate, water_ml, meal_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, row["meal_type"], json.dumps(row["items"]), row["calories_low"],
             row["calories_high"], row["calories_estimate"], row["water_ml"], row["meal_date"]),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return get_meal(chat_id, new_id)


# ---------- workouts ----------

def add_workout(chat_id, activity, duration_min=None, distance_km=None, notes=None):
    get_or_create_user(chat_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO workouts (chat_id, activity, duration_min, distance_km, notes, workout_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, activity, duration_min, distance_km, notes, today_str()),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_recent_workouts(chat_id, limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM workouts WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_workout(chat_id, workout_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM workouts WHERE id = ? AND chat_id = ?", (workout_id, chat_id)
        ).fetchone()
        return dict(row) if row else None


def edit_workout_date(chat_id, workout_id, new_date_str):
    row = get_workout(chat_id, workout_id)
    if row is None:
        return None
    today = today_str()
    if new_date_str > today:
        new_date_str = today
    with get_conn() as conn:
        conn.execute(
            "UPDATE workouts SET workout_date = ? WHERE id = ? AND chat_id = ?",
            (new_date_str, workout_id, chat_id),
        )
    return get_workout(chat_id, workout_id)


def delete_workout(chat_id, workout_id):
    row = get_workout(chat_id, workout_id)
    if row is None:
        return None
    with get_conn() as conn:
        conn.execute("DELETE FROM workouts WHERE id = ? AND chat_id = ?", (workout_id, chat_id))
    return row


def delete_most_recent_workout(chat_id):
    recent = get_recent_workouts(chat_id, limit=1)
    if not recent:
        return None
    return delete_workout(chat_id, recent[0]["id"])


def restore_deleted_workout(chat_id, row):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO workouts (chat_id, activity, duration_min, distance_km, notes, workout_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, row["activity"], row["duration_min"], row["distance_km"], row["notes"],
             row["workout_date"]),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return get_workout(chat_id, new_id)


# ---------- vitals ----------
# One row per daily check-in. All fields nullable -- a check-in commonly
# reports only some of weight/sleep/knee, plus a free-text note.

def add_vitals(chat_id, weight_kg=None, sleep_hours=None, knee_pain=None, notes=None):
    get_or_create_user(chat_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO vitals (chat_id, weight_kg, sleep_hours, knee_pain, notes, vitals_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, weight_kg, sleep_hours, knee_pain, notes, today_str()),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_recent_vitals(chat_id, limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM vitals WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_vitals(chat_id, vitals_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM vitals WHERE id = ? AND chat_id = ?", (vitals_id, chat_id)
        ).fetchone()
        return dict(row) if row else None


def edit_vitals_date(chat_id, vitals_id, new_date_str):
    row = get_vitals(chat_id, vitals_id)
    if row is None:
        return None
    today = today_str()
    if new_date_str > today:
        new_date_str = today
    with get_conn() as conn:
        conn.execute(
            "UPDATE vitals SET vitals_date = ? WHERE id = ? AND chat_id = ?",
            (new_date_str, vitals_id, chat_id),
        )
    return get_vitals(chat_id, vitals_id)


def delete_vitals(chat_id, vitals_id):
    row = get_vitals(chat_id, vitals_id)
    if row is None:
        return None
    with get_conn() as conn:
        conn.execute("DELETE FROM vitals WHERE id = ? AND chat_id = ?", (vitals_id, chat_id))
    return row


def delete_most_recent_vitals(chat_id):
    recent = get_recent_vitals(chat_id, limit=1)
    if not recent:
        return None
    return delete_vitals(chat_id, recent[0]["id"])


def restore_deleted_vitals(chat_id, row):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO vitals (chat_id, weight_kg, sleep_hours, knee_pain, notes, vitals_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, row["weight_kg"], row["sleep_hours"], row["knee_pain"], row["notes"],
             row["vitals_date"]),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return get_vitals(chat_id, new_id)
